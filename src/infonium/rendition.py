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
    FormulaMark,
    LinkMark,
    Mark,
    Style,
    StyleMark,
    is_unspaced,
)
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
# A tag as the model may write it: in any case, with spaces, with or without a number, with
# attributes, or closing itself.
TAG = re.compile(
    rf"{OPENS}\s*(/?)\s*({NAMES})(?![a-z])\s*(\d*)(?:\s[^<>]{{0,80}}?)?\s*/?\s*{CLOSES}",
    re.IGNORECASE,
)
# What is left of a tag that was not read as one: one of the tag names with a number beside
# a bracket of any kind, in the text as Unicode's compatibility form writes it, where a
# full-width letter, digit or bracket is a plain one. A bracket of HTML that the source does
# not hold is left of a tag that was cut.
ANY_OPENING = r"(?:[<《〈\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}«\[]|&(?:amp;)*lt;)"
ANY_CLOSING = r"(?:[>》〉\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}»\]]|&(?:amp;)*gt;)"
REMNANT = re.compile(
    rf"{ANY_OPENING}\s*/?\s*(?:{NAMES})\s*[-_]?\s*\d"
    rf"|(?<![a-z])(?:{NAMES})\s*[-_]?\s*\d+\s*/?\s*{ANY_CLOSING}",
    re.IGNORECASE,
)
LONE_BRACKETS = "<>"
COMPATIBILITY_FORM: Final = "NFKC"
CLOSING_SLASH = "/"
OPENING = "<{name}{number}>"
CLOSING = f"<{CLOSING_SLASH}{{name}}{{number}}>"
# Whether each tag of a pair closes, in the order a usable pair stands.
OPENED_THEN_CLOSED = [False, True]
# How a term lists several forms of itself, and the part of a form that may be left out.
TERM_FORMS = re.compile(r"\s*/\s*")
OPTIONAL_PART = re.compile(r"\([^)]*\)")
# A word of a term's form is found in a passage where a word of the passage begins as it
# begins: a word of two letters or fewer only whole, one of up to this many letters in full,
# and a longer one without its last letters, as many as an ending changes, keeping as many.
# Measured on the measurement's Finnish and Spanish sources and their terms: Phobos finds
# Phoboksen, Lappeenranta Lappeenrannan, Haukivesi Haukiveden, Mars Marsin.
WHOLE_WORD_LETTERS = 2
STEM_LETTERS = 5
ENDING_LETTERS = 2
WORD = re.compile(r"[^\W\d_]+")
# An answer is out of proportion with its passage when the share of its length in its
# source's falls below or rises above these parts of the request's median share, among the
# passages long enough to be judged; two answers alike to passages that differ are as wrong.
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
LOST_FORMULA = "the reply to passage {number} does not hold the formula {formula!r} in its tags"
BARE_FORMULA = "passage {number} is sent without tags, so its formulas cannot come back in them"
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


class Term(BaseModel):
    # A name or a term that recurs in a document, and the one translation it is given in all
    # of it.
    source: str
    translation: str


class Passages(BaseModel):
    # Passages to translate, by the blocks they belong to, numbered from 1 across the blocks
    # in the order they are given; the terms of their document; and the text of the document
    # around them.
    title: str
    terms: list[Term]
    before: str
    blocks: list[list[Wording]]
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


def is_bare(wording: Wording) -> bool:
    # A text that holds what reads as one of the tags is sent without tags and read back
    # as it comes, since a tag in the reply could then be either.
    return TAG.search(wording.text) is not None


def markup(wording: Wording) -> str:
    # The text with a tag at each end of each mark. Where tags meet, the closing ones come
    # first, and of the ones that open together the one that closes last opens first.
    if is_bare(wording):
        return wording.text
    tags: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for number, mark in enumerate(wording.marks, start=1):
        name = tag_name(mark)
        tags[mark.end].append((0, -mark.start, CLOSING.format(name=name, number=number)))
        tags[mark.start].append((1, -mark.end, OPENING.format(name=name, number=number)))
    pieces: list[str] = []
    for at in range(len(wording.text) + 1):
        pieces.extend(tag for _, _, tag in sorted(tags[at]))
        pieces.append(wording.text[at : at + 1])
    return "".join(pieces)


def wordings_of(passages: Passages) -> list[Wording]:
    return [wording for block in passages.blocks for wording in block]


def term_forms(term: Term) -> list[str]:
    forms = [OPTIONAL_PART.sub("", form).strip() for form in TERM_FORMS.split(term.source)]
    return [form.casefold() for form in forms if form]


def word_found(word: str, words: set[str]) -> bool:
    # Whether a word of a term's form begins one of a passage's words, as far as an ending
    # leaves it.
    if len(word) <= WHOLE_WORD_LETTERS:
        return word in words
    stem = word[: max(min(len(word), STEM_LETTERS), len(word) - ENDING_LETTERS)]
    return any(said.startswith(stem) for said in words)


def form_found(form: str, said: str, words: set[str]) -> bool:
    # A form written without spaces between its words is looked for as it is written.
    if any(map(is_unspaced, form)):
        return form in said
    form_words = WORD.findall(form)
    return bool(form_words) and all(word_found(word, words) for word in form_words)


def occurring_terms(terms: list[Term], wordings: list[Wording]) -> list[Term]:
    # The terms one of whose forms a passage uses, in whatever form its words take.
    said = "\n".join(wording.text for wording in wordings).casefold()
    words = set(WORD.findall(said))
    return [
        term for term in terms if any(form_found(form, said, words) for form in term_forms(term))
    ]


def prompt(passages: Passages) -> str:
    # The request carries, of the document's terms, those its passages use.
    terms = occurring_terms(passages.terms, wordings_of(passages))
    firsts = [0, *accumulate(len(block) for block in passages.blocks)]
    numbered = [
        line
        for block_number, (first, block) in enumerate(
            zip(firsts[:-1], passages.blocks, strict=True), start=1
        )
        for line in [
            BLOCK.format(number=block_number),
            *(
                PASSAGE.format(number=number, markup=markup(wording))
                for number, wording in enumerate(block, start=first + 1)
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


def tag_ranges(reply: str) -> tuple[str, dict[str, tuple[int, int]]]:
    # The reply without anything that reads as a tag and without its spaces at either end,
    # and the range each pair of numbered tags encloses in what is left, by the tag's name
    # and number. A tag that stands other than once opening and then once closing, or has no
    # number, encloses nothing; it is taken out all the same.
    untagged = TAG.sub("", reply)
    lead = len(untagged) - len(untagged.lstrip())
    text = untagged.strip()
    ends: dict[str, list[tuple[bool, int]]] = defaultdict(list)
    removed = 0
    for tag in TAG.finditer(reply):
        at = min(max(tag.start() - removed - lead, 0), len(text))
        if tag.group(3):
            key = f"{tag.group(2).lower()}{int(tag.group(3))}"
            ends[key].append((tag.group(1) == CLOSING_SLASH, at))
        removed += len(tag.group())
    paired = {
        key: tag_ends
        for key, tag_ends in ends.items()
        if [closes for closes, _ in tag_ends] == OPENED_THEN_CLOSED
    }
    return text, {key: (tag_ends[0][1], tag_ends[1][1]) for key, tag_ends in paired.items()}


def remnant(text: str, source: str) -> str:
    # What is left of a tag in a reply's text once the tags read are taken out, or nothing.
    compatible = unicodedata.normalize(COMPATIBILITY_FORM, text)
    found = REMNANT.search(compatible)
    if found is not None:
        return found.group()
    compatible_source = unicodedata.normalize(COMPATIBILITY_FORM, source)
    return "".join(
        bracket
        for bracket in LONE_BRACKETS
        if compatible.count(bracket) > compatible_source.count(bracket)
    )


def rendered(wording: Wording, number: int, reply: str) -> Rendered:
    # The reply to one passage, without its tags, and with each mark of the source whose
    # tags it keeps, in the order they stand in it. A passage sent without tags is read back
    # as it is. A mark whose tags the reply lost or garbled is left out; a reply that holds
    # what is left of a tag, or does not hold a formula inside its own tags with exactly its
    # text, is of no use.
    text, ranges = (reply.strip(), {}) if is_bare(wording) else tag_ranges(reply)
    if not text:
        raise UnusableReply(NO_WORDS.format(number=number))
    left = "" if is_bare(wording) else remnant(text, wording.text)
    if left:
        raise UnusableReply(REMNANT_LEFT.format(number=number, remnant=left))
    marks: list[Mark] = []
    for mark_number, mark in enumerate(wording.marks, start=1):
        start, end = trimmed(*ranges.get(f"{tag_name(mark)}{mark_number}", (0, 0)), text)
        formula = wording.text[mark.start : mark.end]
        if isinstance(mark, FormulaMark) and is_bare(wording):
            raise UnusableReply(BARE_FORMULA.format(number=number))
        if isinstance(mark, FormulaMark) and text[start:end] != formula:
            raise UnusableReply(LOST_FORMULA.format(number=number, formula=formula))
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
    firsts = [0, *accumulate(len(block) for block in passages.blocks)]
    for block_number, (first, block) in enumerate(
        zip(firsts[:-1], passages.blocks, strict=True), start=1
    ):
        asked = list(range(first + 1, first + len(block) + 1))
        match given[block_number]:
            case [BlockReply(translations=translations)] if (
                sorted(answer.number for answer in translations) == asked
            ):
                answers.update({answer.number - 1: answer.translation for answer in translations})
            case _:
                unusable.append(WRONG_BLOCK.format(block=block_number))
    return answers, unusable


def out_of_proportion(wordings: list[Wording], kept: dict[int, Rendered]) -> dict[int, str]:
    # The answers out of proportion with their passages, or alike for passages that differ,
    # among the passages long enough to be judged, with why.
    judged = {
        at: (wordings[at].text, done.text)
        for at, done in kept.items()
        if len(wordings[at].text) >= JUDGED_CHARACTERS
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
    # Whether a passage came back with more than half of the marks sent with it, or with all
    # of them where it was sent with none or one. A passage sent without tags keeps what it
    # was sent with.
    sent = 0 if is_bare(wording) else len(wording.marks)
    lost = sent - len(done.marks)
    return lost <= 0 or 2 * lost < sent


def renditions(model: LanguageModel, language: str, passages: Passages) -> Renditions:
    # One request for the passages. A passage whose block the reply answers otherwise than
    # with exactly its passages' answers, or that is answered unusably or out of proportion,
    # is left out of what comes back, with why.
    completion = model.structured(
        instructions=INSTRUCTIONS.format(language=language), prompt=prompt(passages), output=Reply
    )
    wordings = wordings_of(passages)
    answers, unusable = block_answers(passages, completion.output)
    kept: dict[int, Rendered] = {}
    for at, answer in sorted(answers.items()):
        try:
            kept[at] = rendered(wordings[at], at + 1, answer)
        except UnusableReply as refused:
            unusable.append(str(refused))
    refused_answers = out_of_proportion(wordings, kept)
    unusable.extend(refused_answers.values())
    for reason in unusable:
        logger.warning("a passage is asked again: %s", reason)
    return Renditions(
        rendered={at: done for at, done in kept.items() if at not in refused_answers},
        unusable=unusable,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )
