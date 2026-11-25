import logging
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import accumulate
from statistics import median
from typing import Final

from pydantic import BaseModel

from infonium.document import (
    CHINESE_MARKS,
    FormulaMark,
    LinkMark,
    Mark,
    Style,
    StyleMark,
)
from infonium.glossary import TERMS_KEPT_IN_ENGLISH, Term, occurring_terms
from infonium.language_model import LanguageModel

# A passage is written into a request with its marks as tags around the words they mark, each
# tag named for what its mark is and numbered, so that the model can carry it over to the
# words that translate them and the reply tells which mark it is. The names are those of
# HTML, which the model knows, so that it treats the words inside as HTML does.
LINK_TAG = "a"
FORMULA_TAG = "f"
STYLE_TAGS = {
    Style.STRONG: "b",
    Style.EMPHASIS: "i",
    Style.UNDERLINE: "u",
    Style.CODE: "code",
    Style.SUBSCRIPT: "sub",
    Style.SUPERSCRIPT: "sup",
}
TAG_NAMES = sorted([LINK_TAG, FORMULA_TAG, *STYLE_TAGS.values()], key=len, reverse=True)
NAMES = "|".join(TAG_NAMES)
# The brackets a model was seen to write a tag with: those of HTML, written plainly or as
# entities, and the full-width and Chinese ones, also mixed.
OPENS = r"(?:[<\N{FULLWIDTH LESS-THAN SIGN}《〈]|&lt;)"
CLOSES = r"(?:[>\N{FULLWIDTH GREATER-THAN SIGN}》〉]|&gt;)"
# What may be a tag as the model writes it: in any case, with spaces, with or without a
# number, with attributes, or closing itself. It is a tag only where its name and number are
# one of the passage's own keys.
TAG = re.compile(
    rf"{OPENS}\s*(/?)\s*({NAMES})(?![a-z])\s*(\d*)(?:\s[^<>]{{0,80}}?)?\s*/?\s*{CLOSES}",
    re.IGNORECASE,
)
# A key as a word of a text, in the text as Unicode's compatibility form writes it, where a
# full-width letter or digit is a plain one: written close, as "b1", "/b1" or "b-1", or with
# spaces where something other than a letter or a digit stands before it, as a bracket does.
# It is looked for alike in a passage's own text, whose keys it holds are not given to its
# tags, and in a reply, where it is left of a tag.
KEY_LEFT = (
    r"(?<![0-9a-z])/?{name}[-_]?0*{number}(?![0-9])"
    r"|(?<=[^\w\s])\s*/?\s*{name}\s*[-_]?\s*0*{number}(?![0-9])"
)
# The brackets of HTML, which the source does not hold where a tag was cut.
LONE_BRACKETS = ("<", ">", "&lt;", "&gt;")
COMPATIBILITY_FORM: Final = "NFKC"
DIGITS = "0123456789"
CLOSING_SLASH = "/"
OPENING = "<{key}>"
CLOSING = f"<{CLOSING_SLASH}{{key}}>"
# Whether each tag of a pair closes, in the order a usable pair stands.
OPENED_THEN_CLOSED = [False, True]
# An answer is out of proportion with its passage when the share of its length in its
# source's falls below or rises above these parts of the request's median share, among the
# passages long enough to be judged; two answers alike to passages that differ are as wrong.
# Only the segments of a block cut into several, a recording's, are judged so: only there
# can an answer slip to its neighbour, and a heading or a table's cell may be far shorter or
# longer than its source and still be sound.
# Measured on three candidates' translations of sixteen documents, the requests gathered as
# the engine gathers them: sound answers lie between 0.40 and 3.27 of their request's median,
# and gpt-6.1-terra's run of answers shifted by one passage in a talk fell to 0.30, 0.25 and
# 0.11 and rose to 8.35. Its repeated answers were to sources alike by 0.11 and 0.21; the
# sound translations' repeats were to sources alike by 0.94, or of fewer than 20 characters.
JUDGED_CHARACTERS = 20
LEAST_PROPORTION = 0.35
MOST_PROPORTION = 5.0
ALIKE_SOURCES = 0.8
NO_WORDS = "the reply has no words for passage {number}"
REMNANT_LEFT = "the reply to passage {number} holds what is left of a tag: {remnant!r}"
LOST_FORMULA = (
    "the reply to passage {number} does not hold the formula {formula!r} alone in its tags"
)
WRONG_BLOCK = "the reply's answers to block {block} are not those of its passages"
OUT_OF_PROPORTION = (
    "the answer to passage {number} is {share:.2f} times as long, against its source, as the "
    "request's answers are"
)
SAME_ANSWER = "passages {numbers} have unlike sources and the same answer"
INSTRUCTIONS = """\
You translate passages of a document into {language}, for a reader who reads the \
document in that language.

The passages are grouped by the block of the document they belong to: each group starts \
with a line "Block" and the block's number. The passages are numbered across the groups; \
each starts on a line of its own with its number in square brackets. Translate every \
passage, and only the passages: the text before and after them is there so that you know \
what you are translating. Reply for each block with one translation for each of its \
passages, under the same block and the same number, and with nothing of another passage.

- Translate faithfully and completely into natural {language}. Do not summarise, explain, \
or add anything that the passage does not say.
- A passage may be a heading, a cell of a table, a caption, or a sentence that the next \
passage goes on with. Translate what the passage says, and nothing of the passages around \
it.
- Keep each name of a person, a product, a place or a work, and each technical term, the \
same in every passage. Where the request lists terms, translate a listed term as it is \
listed wherever a passage uses it in that sense; words that only look like a listed term \
are translated for what they say.
- {terms_kept_in_english} A term kept in English stands alone, but at its first use in \
the whole document its {language} word follows it, after a space, in round brackets of \
Latin script, as in "Fresnel lens (菲涅尔透镜)" in Chinese, where {language} has a word \
for it. The request names under a heading of its own the passage in which the document \
first uses a listed term: give the word there, at the term's first use in that passage, \
and nowhere else. A term kept in English that the request does not list is first used \
where these passages first use it, unless the text before them does.
- Punctuate the translation as {language} is written: in Chinese, with the marks \
{chinese_marks} and never a comma or a colon of Latin script.
- Keep the value of every number, and write numbers and units the way {language} writes \
them.
- Some words are enclosed in tags such as <b1>...</b1>, <a2>...</a2> or <code3>...</code3>. \
Put each pair of tags, with the same name and number, around the words of your translation \
that translate the words it encloses. Add no tags of your own.
- A tag named f, such as <f4>...</f4>, encloses a formula written in LaTeX. Copy it with \
its tags, character for character.
- Copy the words inside code tags as they are written.
"""
TITLE = "Title: {title}"
TERMS = "Terms:"
TERM = "{source} = {translation}"
FIRST_USES = "First use in the document of a term kept as it is written:"
FIRST_USE = "{source}: passage [{number}]"
BEFORE = "Text before the passages:\n{text}"
PASSAGES = "Passages to translate:"
BLOCK = "Block {number}:"
PASSAGE = "[{number}] {markup}"
AFTER = "Text after the passages:\n{text}"
PARTS_BETWEEN = "\n\n"

logger = logging.getLogger(__name__)


class Wording(BaseModel):
    # A passage of a source, with its marks, counted in its text.
    text: str
    marks: list[Mark]


class Rendered(BaseModel):
    # A passage in the reader's language, with the marks of its source that the model kept,
    # counted in the translated text.
    text: str
    marks: list[Mark]


class PassageBlock(BaseModel):
    # The passages of one block, and whether they are segments of a block cut into several,
    # whose answers can slip from one to the next.
    wordings: list[Wording]
    in_segments: bool


class FirstUse(BaseModel):
    # A term the list keeps as it is written, by its source, and the passage, by its number,
    # in which the document first uses it.
    source: str
    passage: int


class Passages(BaseModel):
    # Passages to translate, by the blocks they belong to, numbered from 1 across the blocks
    # in the order they are given; the terms of their document, and the passages among them
    # in which the document first uses a term kept as it is written; and the text of the
    # document around them.
    title: str
    terms: list[Term]
    first_uses: list[FirstUse]
    before: str
    blocks: list[PassageBlock]
    after: str


class PassageReply(BaseModel):
    number: int
    translation: str


class BlockReply(BaseModel):
    block: int
    translations: list[PassageReply]


class Reply(BaseModel):
    blocks: list[BlockReply]


class Renditions(BaseModel):
    # What came back for a request: each passage that came back usable, by its place among
    # the passages asked for; why the others could not be used; and what the request took of
    # the model.
    rendered: dict[int, Rendered]
    unusable: list[str]
    input_tokens: int
    output_tokens: int


class UnusableReply(Exception):
    pass


def tag_name(mark: Mark) -> str:
    match mark:
        case LinkMark():
            return LINK_TAG
        case FormulaMark():
            return FORMULA_TAG
        case StyleMark(style=style):
            return STYLE_TAGS[style]


def compatible(text: str) -> str:
    # A text as Unicode's compatibility form writes it, where a full-width letter or digit is
    # a plain one, in one case.
    return unicodedata.normalize(COMPATIBILITY_FORM, text).casefold()


def key_found(key: str, text: str) -> list[str]:
    # Where a key stands in a text made compatible, in any form the reading takes for it.
    name, number = key.rstrip(DIGITS), int(key[len(key.rstrip(DIGITS)) :])
    found = re.finditer(KEY_LEFT.format(name=re.escape(name), number=number), text)
    return [place.group().strip() for place in found]


def keys_of(wording: Wording) -> list[str]:
    # The keys of a passage's own tags, one for each mark: its tag's name and a number, which
    # counts on past any key that the passage's own text holds in any form the reading takes
    # for a key. So whatever of a key stands in a reply is left of a tag, and nothing of the
    # source can pass for one.
    held = compatible(wording.text)
    keys: list[str] = []
    number = 1
    for mark in wording.marks:
        while key_found(f"{tag_name(mark)}{number}", held):
            number += 1
        keys.append(f"{tag_name(mark)}{number}")
        number += 1
    return keys


def tag_key(tag: re.Match[str]) -> str:
    return f"{tag.group(2).lower()}{int(tag.group(3))}" if tag.group(3) else ""


def markup(wording: Wording) -> str:
    # The text with a tag at each end of each mark. Where tags meet, the closing ones come
    # first, and of the ones that open together the one that closes last opens first.
    tags: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for key, mark in zip(keys_of(wording), wording.marks, strict=True):
        tags[mark.end].append((0, -mark.start, CLOSING.format(key=key)))
        tags[mark.start].append((1, -mark.end, OPENING.format(key=key)))
    pieces: list[str] = []
    for at in range(len(wording.text) + 1):
        pieces.extend(tag for _, _, tag in sorted(tags[at]))
        pieces.append(wording.text[at : at + 1])
    return "".join(pieces)


def wordings_of(passages: Passages) -> list[Wording]:
    return [wording for block in passages.blocks for wording in block.wordings]


def prompt(passages: Passages) -> str:
    # The request carries, of the document's terms, those its passages use.
    terms = occurring_terms(passages.terms, [wording.text for wording in wordings_of(passages)])
    firsts = [0, *accumulate(len(block.wordings) for block in passages.blocks)]
    numbered = [
        line
        for block_number, (first, block) in enumerate(
            zip(firsts[:-1], passages.blocks, strict=True), start=1
        )
        for line in [
            BLOCK.format(number=block_number),
            *(
                PASSAGE.format(number=number, markup=markup(wording))
                for number, wording in enumerate(block.wordings, start=first + 1)
            ),
        ]
    ]
    parts = [
        TITLE.format(title=passages.title),
        *(
            [
                "\n".join(
                    [
                        TERMS,
                        *(
                            TERM.format(source=term.source, translation=term.translation)
                            for term in terms
                        ),
                    ]
                )
            ]
            if terms
            else []
        ),
        *(
            [
                "\n".join(
                    [
                        FIRST_USES,
                        *(
                            FIRST_USE.format(source=use.source, number=use.passage)
                            for use in passages.first_uses
                        ),
                    ]
                )
            ]
            if passages.first_uses
            else []
        ),
        *([BEFORE.format(text=passages.before)] if passages.before else []),
        "\n".join([PASSAGES, *numbered]),
        *([AFTER.format(text=passages.after)] if passages.after else []),
    ]
    return PARTS_BETWEEN.join(parts)


def trimmed(start: int, end: int, text: str) -> tuple[int, int]:
    # A range without the spaces at its ends.
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def kept_mark(mark: Mark, start: int, end: int) -> Mark:
    return mark.model_copy(update={"start": start, "end": end})


def tag_pairs(reply: str, keys: list[str]) -> dict[str, list[re.Match[str]]]:
    # The passage's own tags in a reply that pair, by their key. Only a key's tag that stands
    # once opening and then once closing is a pair; anything else in brackets stays in the
    # text, as a title such as 《F1》 does.
    found: dict[str, list[re.Match[str]]] = defaultdict(list)
    for tag in TAG.finditer(reply):
        if tag_key(tag) in keys:
            found[tag_key(tag)].append(tag)
    return {
        key: tags
        for key, tags in found.items()
        if [tag.group(1) == CLOSING_SLASH for tag in tags] == OPENED_THEN_CLOSED
    }


def with_formulas(wording: Wording, number: int, reply: str) -> str:
    # The reply with each formula's own text between the formula's tags, whatever the reply
    # held there: a formula is not translated, and a model alters its LaTeX in copying it,
    # as in writing its backslashes. A formula whose tags the reply lost, or between whose
    # tags it holds a letter the formula does not hold, as words of the sentence put inside
    # them, makes the reply of no use.
    pairs = tag_pairs(reply, keys_of(wording))
    formulas = [
        (pairs.get(key, []), wording.text[mark.start : mark.end])
        for key, mark in zip(keys_of(wording), wording.marks, strict=True)
        if isinstance(mark, FormulaMark)
    ]
    for tags, formula in formulas:
        held = "" if not tags else reply[tags[0].end() : tags[1].start()]
        if not tags or set(filter(str.isalpha, held)) - set(formula):
            raise UnusableReply(LOST_FORMULA.format(number=number, formula=formula))
    for tags, formula in sorted(formulas, key=lambda pair: -pair[0][0].start()):
        reply = f"{reply[: tags[0].end()]}{formula}{reply[tags[1].start() :]}"
    return reply


def tag_ranges(reply: str, keys: list[str]) -> tuple[str, dict[str, tuple[int, int]]]:
    # The reply without the passage's own tags that pair, and without its spaces at either
    # end, and the range each such pair encloses in what is left, by its key.
    pairs = tag_pairs(reply, keys)
    taken = sorted((tag for tags in pairs.values() for tag in tags), key=lambda tag: tag.start())
    pieces: list[str] = []
    ends: dict[str, list[int]] = defaultdict(list)
    last = 0
    for tag in taken:
        pieces.append(reply[last : tag.start()])
        ends[tag_key(tag)].append(sum(map(len, pieces)))
        last = tag.end()
    pieces.append(reply[last:])
    untagged = "".join(pieces)
    lead = len(untagged) - len(untagged.lstrip())
    text = untagged.strip()
    return text, {
        key: (min(max(opened - lead, 0), len(text)), min(max(closed - lead, 0), len(text)))
        for key, (opened, closed) in ends.items()
    }


def remnant(text: str, source: str, keys: list[str]) -> str:
    # What is left of the passage's own tags in a reply's text once its pairs are taken out,
    # or nothing: one of its keys, or the closing form of one, standing as a word, which the
    # source cannot hold, since a passage's keys are none its text holds; or a bracket of
    # HTML that the source does not hold.
    reply = compatible(text)
    left = [found for key in keys for found in key_found(key, reply)]
    if left:
        return left[0]
    held = compatible(source)
    return "".join(
        bracket for bracket in LONE_BRACKETS if reply.count(bracket) > held.count(bracket)
    )


def rendered(wording: Wording, number: int, reply: str) -> Rendered:
    # The reply to one passage, without its tags, and with each mark of the source whose
    # tags it keeps, in the order they stand in it, each formula's own text between its
    # tags. A mark whose tags the reply lost or garbled is left out; a reply that holds what
    # is left of a tag, or lost a formula's tags, is of no use.
    keys = keys_of(wording)
    text, ranges = tag_ranges(with_formulas(wording, number, reply), keys)
    if not text:
        raise UnusableReply(NO_WORDS.format(number=number))
    left = remnant(text, wording.text, keys)
    if left:
        raise UnusableReply(REMNANT_LEFT.format(number=number, remnant=left))
    marks: list[Mark] = []
    for key, mark in zip(keys, wording.marks, strict=True):
        start, end = trimmed(*ranges.get(key, (0, 0)), text)
        if start < end:
            marks.append(kept_mark(mark, start, end))
    return Rendered(text=text, marks=sorted(marks, key=lambda mark: (mark.start, mark.end)))


def block_answers(passages: Passages, reply: Reply) -> tuple[dict[int, str], list[str]]:
    # Each passage's answer, by its place among the passages, where the reply gives for its
    # block exactly the answers of that block's passages, once each; and why the answers to
    # the other blocks cannot be used.
    given: dict[int, list[BlockReply]] = defaultdict(list)
    for block_reply in reply.blocks:
        given[block_reply.block].append(block_reply)
    answers: dict[int, str] = {}
    unusable: list[str] = []
    firsts = [0, *accumulate(len(block.wordings) for block in passages.blocks)]
    for block_number, (first, block) in enumerate(
        zip(firsts[:-1], passages.blocks, strict=True), start=1
    ):
        asked = list(range(first + 1, first + len(block.wordings) + 1))
        match given[block_number]:
            case [BlockReply(translations=translations)] if (
                sorted(answer.number for answer in translations) == asked
            ):
                answers.update({answer.number - 1: answer.translation for answer in translations})
            case _:
                unusable.append(WRONG_BLOCK.format(block=block_number))
    return answers, unusable


def out_of_proportion(passages: Passages, kept: dict[int, Rendered]) -> dict[int, str]:
    # The answers out of proportion with their passages, or alike for passages that differ,
    # among the segments of blocks cut into several that are long enough to be judged, with
    # why.
    segments = {
        at: wording
        for at, (wording, in_segments) in enumerate(
            (wording, block.in_segments) for block in passages.blocks for wording in block.wordings
        )
        if in_segments
    }
    judged = {
        at: (segments[at].text, done.text)
        for at, done in kept.items()
        if at in segments and len(segments[at].text) >= JUDGED_CHARACTERS
    }
    if not judged:
        return {}
    shares = {at: len(answer) / len(source) for at, (source, answer) in judged.items()}
    usual = median(shares.values())
    refused = {
        at: OUT_OF_PROPORTION.format(number=at + 1, share=share / usual)
        for at, share in shares.items()
        if not LEAST_PROPORTION <= share / usual <= MOST_PROPORTION
    }
    by_answer: dict[str, list[int]] = defaultdict(list)
    for at, (_, answer) in judged.items():
        by_answer[answer].append(at)
    for alike in by_answer.values():
        sources = [judged[at][0].casefold() for at in alike]
        unlike = any(
            SequenceMatcher(None, one, other).ratio() < ALIKE_SOURCES
            for index, one in enumerate(sources)
            for other in sources[index + 1 :]
        )
        if unlike:
            numbers = [at + 1 for at in alike]
            refused.update({at: SAME_ANSWER.format(numbers=numbers) for at in alike})
    return refused


def keeps_marks(wording: Wording, done: Rendered) -> bool:
    # Whether a passage came back with more than half of its marks, or with all of them
    # where it had none or one.
    lost = len(wording.marks) - len(done.marks)
    return lost == 0 or 2 * lost < len(wording.marks)


def renditions(model: LanguageModel, language: str, passages: Passages) -> Renditions:
    # One request for the passages. A passage whose block the reply answers otherwise than
    # with exactly its passages' answers, or that is answered unusably or out of proportion,
    # is left out of what comes back, with why.
    completion = model.structured(
        instructions=INSTRUCTIONS.format(
            language=language,
            terms_kept_in_english=TERMS_KEPT_IN_ENGLISH.format(language=language),
            chinese_marks=CHINESE_MARKS,
        ),
        prompt=prompt(passages),
        output=Reply,
    )
    wordings = wordings_of(passages)
    answers, unusable = block_answers(passages, completion.output)
    kept: dict[int, Rendered] = {}
    for at, answer in sorted(answers.items()):
        try:
            kept[at] = rendered(wordings[at], at + 1, answer)
        except UnusableReply as refused:
            unusable.append(str(refused))
    refused_answers = out_of_proportion(passages, kept)
    unusable.extend(refused_answers.values())
    for reason in unusable:
        logger.warning("a passage is asked again: %s", reason)
    return Renditions(
        rendered={at: done for at, done in kept.items() if at not in refused_answers},
        unusable=unusable,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )
