import logging
import re
from collections import defaultdict

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
# Whatever reads as one of these tags, as the model may write it: in any case, with spaces,
# with or without a number, with attributes, closing itself, or written as HTML entities.
TAG = re.compile(
    rf"(?:<|&lt;)\s*(/?)\s*({'|'.join(TAG_NAMES)})(?![a-z])\s*(\d*)(?:\s[^<>]{{0,80}}?)?\s*/?\s*"
    r"(?:>|&gt;)",
    re.IGNORECASE,
)
CLOSING_SLASH = "/"
OPENING = "<{name}{number}>"
CLOSING = f"<{CLOSING_SLASH}{{name}}{{number}}>"
# Whether each tag of a pair closes, in the order a usable pair stands.
OPENED_THEN_CLOSED = [False, True]
# How a term lists several forms of itself, and the part of a form that may be left out.
TERM_FORMS = re.compile(r"\s*/\s*")
OPTIONAL_PART = re.compile(r"\([^)]*\)")
NO_WORDS = "the reply has no words for passage {number}"
LOST_FORMULA = (
    "the reply to passage {number} holds the formula {formula!r} {found} times as a token of "
    "its own, where the source holds it {asked} times"
)
INSTRUCTIONS = """\
You translate passages of a document into {language}, for a reader who reads the \
document in that language.

The passages are numbered; each starts on a line of its own with its number in square \
brackets. Translate every passage, and only the passages: the text before and after them \
is there so that you know what you are translating. Reply with one translation for each \
passage, under the same number.

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
    # Passages to translate, numbered from 1 in the order they are given, the terms of their
    # document, and the text of the document around them.
    title: str
    terms: list[Term]
    before: str
    wordings: list[Wording]
    after: str


class PassageReply(BaseModel):
    number: int
    translation: str


class Reply(BaseModel):
    translations: list[PassageReply]


class Renditions(BaseModel):
    # What came back for a request: each passage that came back usable, by its place among
    # the passages asked for, and what the request took of the model.
    rendered: dict[int, Rendered]
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


def term_forms(term: Term) -> list[str]:
    forms = [OPTIONAL_PART.sub("", form).strip() for form in TERM_FORMS.split(term.source)]
    return [form.casefold() for form in forms if form]


def occurring_terms(terms: list[Term], wordings: list[Wording]) -> list[Term]:
    # The terms that one of their forms names in the passages.
    said = "\n".join(wording.text for wording in wordings).casefold()
    return [term for term in terms if any(form in said for form in term_forms(term))]


def prompt(passages: Passages) -> str:
    # The request carries, of the document's terms, those its passages name.
    terms = occurring_terms(passages.terms, passages.wordings)
    numbered = [
        PASSAGE.format(number=number, markup=markup(wording))
        for number, wording in enumerate(passages.wordings, start=1)
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


def runs_on(character: str) -> bool:
    # A letter or a digit of a script written with spaces between words runs on into the
    # letter or digit beside it, as one word or number.
    return character.isalnum() and not is_unspaced(character)


def stands_alone(text: str, start: int, end: int) -> bool:
    # Whether a range of a text is a token of its own: no word or number runs on into it.
    before, after = text[start - 1 : start] if start else "", text[end : end + 1]
    return not (runs_on(before) and runs_on(text[start])) and not (
        runs_on(after) and runs_on(text[end - 1])
    )


def token_starts(text: str, part: str) -> list[int]:
    return [
        found.start()
        for found in re.finditer(re.escape(part), text)
        if stands_alone(text, found.start(), found.end())
    ]


def formulas_by_text(
    wording: Wording, number: int, text: str, lost: list[FormulaMark], taken: set[int]
) -> list[Mark]:
    # Formulas whose tags the reply lost, found by their text: where it stands as a token of
    # its own, as often as the source holds the formula, at the places no tag has taken.
    found: list[Mark] = []
    for formula in dict.fromkeys(wording.text[mark.start : mark.end] for mark in lost):
        starts = token_starts(text, formula)
        asked = [
            mark
            for mark in wording.marks
            if isinstance(mark, FormulaMark) and wording.text[mark.start : mark.end] == formula
        ]
        if len(starts) != len(asked):
            raise UnusableReply(
                LOST_FORMULA.format(
                    number=number, formula=formula, found=len(starts), asked=len(asked)
                )
            )
        missing = [mark for mark in lost if wording.text[mark.start : mark.end] == formula]
        free = [start for start in starts if start not in taken]
        found.extend(
            kept_mark(mark, start, start + len(formula))
            for mark, start in zip(missing, free, strict=True)
        )
    return found


def rendered(wording: Wording, number: int, reply: str) -> Rendered:
    # The reply to one passage, without anything that reads as a tag, and with each mark of
    # the source whose tags it keeps, in the order they stand in it. A passage sent without
    # tags is read back as it is. A mark whose tags the reply lost or garbled is left out,
    # but a formula comes back as it was: where its tags are lost, it is found by its text,
    # and a reply in which that fails is of no use. A formula is never marked inside a word
    # or a number.
    text, ranges = (reply.strip(), {}) if is_bare(wording) else tag_ranges(reply)
    if not text:
        raise UnusableReply(NO_WORDS.format(number=number))
    marks: list[Mark] = []
    lost: list[FormulaMark] = []
    for mark_number, mark in enumerate(wording.marks, start=1):
        start, end = trimmed(*ranges.get(f"{tag_name(mark)}{mark_number}", (0, 0)), text)
        if isinstance(mark, FormulaMark):
            formula = wording.text[mark.start : mark.end]
            if text[start:end] == formula and stands_alone(text, start, end):
                marks.append(kept_mark(mark, start, end))
            else:
                lost.append(mark)
        elif start < end:
            marks.append(kept_mark(mark, start, end))
    taken = {mark.start for mark in marks if isinstance(mark, FormulaMark)}
    marks.extend(formulas_by_text(wording, number, text, lost, taken))
    return Rendered(text=text, marks=sorted(marks, key=lambda mark: (mark.start, mark.end)))


def keeps_marks(wording: Wording, done: Rendered) -> bool:
    # Whether a passage came back with more than half of the marks sent with it, or with all
    # of them where it was sent with none or one. A passage sent without tags keeps what it
    # was sent with.
    sent = 0 if is_bare(wording) else len(wording.marks)
    lost = sent - len(done.marks)
    return lost <= 0 or 2 * lost < sent


def renditions(model: LanguageModel, language: str, passages: Passages) -> Renditions:
    # One request for the passages. A passage that the reply leaves out, answers twice or
    # answers unusably is left out of what comes back.
    completion = model.structured(
        instructions=INSTRUCTIONS.format(language=language), prompt=prompt(passages), output=Reply
    )
    answers: dict[int, list[str]] = defaultdict(list)
    for answer in completion.output.translations:
        answers[answer.number].append(answer.translation)
    kept: dict[int, Rendered] = {}
    for at, wording in enumerate(passages.wordings):
        match answers[at + 1]:
            case [translation]:
                try:
                    kept[at] = rendered(wording, at + 1, translation)
                except UnusableReply as unusable:
                    logger.warning("a passage is asked again: %s", unusable)
            case answered:
                logger.warning("a passage is asked again: %d replies to it", len(answered))
    return Renditions(
        rendered=kept,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )
