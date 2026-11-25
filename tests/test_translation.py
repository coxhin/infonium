import itertools
import threading
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import pytest
from hypothesis import HealthCheck, Phase, assume, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.document import (
    Block,
    Code,
    Figure,
    FormulaMark,
    Heading,
    LinkMark,
    ListItem,
    Mark,
    Paragraph,
    Place,
    Quotation,
    Style,
    StyleMark,
    Table,
    TimePlace,
)
from infonium.transcript import SENTENCE_ENDS, ends_sentence
from tests.model_standin import TRANSLATION_MODEL, Rejection
from tests.service import SITE, App
from tests.translation_standin import (
    RECORDING,
    TAG,
    TRANSLATED,
    WORDS,
    Passage,
    asked_for,
    asks_passages,
    by_number,
    first_replies,
    holding,
    kept,
    passage_requests,
    settled,
    survey_reply,
    translated_whole,
    translating,
    translation,
)

EXAMPLES = settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
FEW_EXAMPLES = settings(max_examples=8, suppress_health_check=[HealthCheck.function_scoped_fixture])
# For judgments that fail against the part as it stands: a failure is not shrunk, which would
# take minutes of translations each tried three times.
UNSHRUNK_EXAMPLES = settings(FEW_EXAMPLES, phases=[Phase.explicit, Phase.reuse, Phase.generate])
# What a block may hold that is no letter of any script: digits of several scripts, signs and
# punctuation.
LETTERLESS = (
    "12",
    "3.5",
    "—",
    "%",
    "→",
    "١٢٣",
    "€",
    "(42)",
    "…",
    "#7",
    "\N{ARABIC DECIMAL SEPARATOR}",
)
FORMULAS = ("$x_{1}^{2}$", "\\alpha+\\beta", "a+b=c", "\\frac{1}{n}", "E=mc^2")
LINK_TARGET = f"{SITE}/elsewhere"
# A recording's segment that has run this long without the end of a sentence ends at the
# first place's end after it.
SEGMENT_BOUND_SECONDS = 10.0


def mark_key(mark: dict[str, Any]) -> tuple[Any, ...]:
    return (mark["kind"], mark.get("target"), mark.get("style"), mark["start"], mark["end"])


def shifted(marks: list[Mark], by: int) -> set[tuple[Any, ...]]:
    return {
        mark_key(
            mark.model_copy(update={"start": mark.start + by, "end": mark.end + by}).model_dump()
        )
        for mark in marks
    }


def segment_marks(block: dict[str, Any]) -> set[tuple[Any, ...]]:
    [segment] = block["segments"]
    return {mark_key(mark) for mark in segment["marks"]}


def sentence_of(words: list[str]) -> str:
    return " ".join(words)


sentences = st.lists(st.sampled_from(WORDS), min_size=1, max_size=6).map(sentence_of)
letterless = st.lists(st.sampled_from(LETTERLESS), min_size=1, max_size=5).map(sentence_of)


# R1: what is translated.


@st.composite
def judged_blocks(draw: st.DrawFn) -> tuple[list[Block], set[int]]:
    # Blocks of every kind, and the numbers of those that say something in words.
    def worded(text: str) -> list[tuple[Block, bool]]:
        return [
            (Paragraph(text=text, marks=[]), True),
            (Heading(level=2, text=text, marks=[]), True),
            (
                ListItem(ordered=True, depth=0, position=1, continues=False, text=text, marks=[]),
                True,
            ),
            (Quotation(text=text, marks=[]), True),
            (Figure(source=f"{SITE}/picture.png", text=text), True),
            (Table(text=f"{text}\t{text}\n{text}\t{text}"), True),
            (Code(text=text), False),
        ]

    def formula(text: str) -> list[tuple[Block, bool]]:
        return [(Paragraph(text=text, marks=[FormulaMark(start=0, end=len(text))]), False)]

    def without_letters(text: str) -> list[tuple[Block, bool]]:
        return [
            (Paragraph(text=text, marks=[]), False),
            (Heading(level=3, text=text, marks=[]), False),
            (Table(text=f"{text}\t{text}"), False),
        ]

    choices = st.one_of(
        sentences.flatmap(lambda text: st.sampled_from(worded(text))),
        st.sampled_from(FORMULAS).flatmap(lambda text: st.sampled_from(formula(text))),
        letterless.flatmap(lambda text: st.sampled_from(without_letters(text))),
    )
    chosen = draw(st.lists(choices, min_size=1, max_size=8))
    chosen.append((Paragraph(text=draw(sentences), marks=[]), True))
    return [block for block, _ in chosen], {
        number for number, (_, said) in enumerate(chosen) if said
    }


@given(judged=judged_blocks())
@EXAMPLES
def test_every_block_that_says_something_in_words_is_translated_and_no_other(
    app: App, judged: tuple[list[Block], set[int]]
) -> None:
    blocks, worded = judged
    app.model.reset(translating())
    document = kept(app, blocks)
    started = asked_for(app, document.item_id)
    assert started["progress"] == {
        "kind": "translating",
        "model": TRANSLATION_MODEL,
        "translated": 0,
        "total": len(worded),
    }, (
        f"Translating does not count the {len(worded)} blocks that say something in words: "
        f"{started['progress']}"
    )
    view = settled(app, document.item_id)
    assert view["progress"]["kind"] == "translated", (
        f"the translation did not end: {view['progress']}"
    )
    assert set(by_number(view)) == worded, (
        "the blocks translated are not those that say something in words (a code block, a "
        "block one formula covers whole and one without letters are not): "
        f"{sorted(by_number(view))} where {sorted(worded)} say something"
    )


STANDINGS = ("unasked", "translating", "stopped", "translated", "unneeded")
# A refusal of the endpoint that the openai package does not try again by itself.
REFUSED = Rejection(status=HTTPStatus.BAD_REQUEST, body='{"error": {"message": "refused"}}')


def standing_view(app: App, item_id: str, standing: str) -> dict[str, Any]:
    # The view of the document's translation once it stands as named.
    match standing:
        case "unasked":
            return translation(app, item_id)
        case "translating":
            gate = threading.Event()
            app.model.reset(holding(translating(), gate, lambda asked: True))
            try:
                asked_for(app, item_id)
                return translation(app, item_id)
            finally:
                gate.set()
                settled(app, item_id)
        case "stopped":
            refusing = translating()
            app.model.reset(lambda asked: REFUSED if asks_passages(asked) else refusing(asked))
        case "translated":
            app.model.reset(translating())
        case _:
            app.model.reset(translating(survey=survey_reply(written_in_asked_language=True)))
    asked_for(app, item_id)
    return settled(app, item_id)


@pytest.mark.parametrize("standing", STANDINGS)
@given(judged=judged_blocks())
@FEW_EXAMPLES
def test_the_blocks_to_translate_are_those_that_say_something_in_words_whatever_the_progress(
    app: App, standing: str, judged: tuple[list[Block], set[int]]
) -> None:
    blocks, worded = judged
    document = kept(app, blocks)
    view = standing_view(app, document.item_id, standing)
    assert view["progress"]["kind"] == standing, (
        f"the translation did not come to stand {standing}: {view['progress']}"
    )
    assert view["blocks_to_translate"] == sorted(worded), (
        f"the blocks to translate of a translation that is {standing} are not, in order, those "
        f"that say something in words: {view['blocks_to_translate']} where {sorted(worded)}"
    )
    if standing in {"translating", "stopped"}:
        assert view["progress"]["total"] == len(view["blocks_to_translate"]), (
            f"the total of {standing} is not the number of blocks to translate: {view['progress']}"
        )
        assert view["progress"]["translated"] == len(view["blocks"]), (
            f"{standing} does not count the blocks kept: {view['progress']}"
        )
    assert set(by_number(view)) <= set(view["blocks_to_translate"]), (
        f"a block kept of a translation that is {standing} is not among the blocks to translate"
    )


# R2: segments.


@given(judged=judged_blocks())
@EXAMPLES
def test_a_block_without_time_places_is_one_segment_over_its_whole_text(
    app: App, judged: tuple[list[Block], set[int]]
) -> None:
    blocks, worded = judged
    app.model.reset(translating())
    document = kept(app, blocks)
    translated = by_number(translated_whole(app, document.item_id))
    assert set(translated) == worded, "a block that says something in words was not translated"
    for number, block in translated.items():
        source = blocks[number].text
        ranges = [(segment["start"], segment["end"]) for segment in block["segments"]]
        assert ranges == [(0, len(source))], (
            f"block {number}, which has no time places, is not one segment over its whole text: "
            f"{ranges}"
        )
        if not isinstance(blocks[number], Table):
            assert block["segments"][0]["text"] == f"{TRANSLATED}{source}", (
                f"block {number} does not hold the translation the model wrote for it"
            )


class Utterance(BaseModel):
    text: str
    moment: float
    duration: float


@st.composite
def spoken(draw: st.DrawFn) -> list[Utterance]:
    # Utterances in the order they are spoken, some ending a sentence, some closing a quote
    # after the sentence's end, and some long stretches without an end.
    utterances = []
    moment = draw(st.integers(0, 20)) / 4
    for _ in range(draw(st.integers(1, 14))):
        words = draw(st.lists(st.sampled_from(WORDS), min_size=1, max_size=4))
        end = draw(st.sampled_from(["", "", "", *sorted(SENTENCE_ENDS), ".”", "?)", ","]))
        duration = draw(st.integers(2, 30)) / 4 + 0.03
        utterances.append(
            Utterance(text=f"{' '.join(words)}{end}", moment=moment, duration=duration)
        )
        moment += duration + draw(st.integers(0, 4)) / 4
    return utterances


def recorded(utterances: list[Utterance]) -> Paragraph:
    places: list[Place] = []
    end = 0
    for utterance in utterances:
        start = end + (1 if places else 0)
        end = start + len(utterance.text)
        places.append(
            TimePlace(start=start, end=end, moment=utterance.moment, duration=utterance.duration)
        )
    return Paragraph(
        text=" ".join(utterance.text for utterance in utterances), marks=[], places=places
    )


def expected_segments(block: Paragraph) -> list[tuple[int, int]]:
    # Each segment ends where a place ends a sentence, or at the end of the first place that
    # ends once the segment has run the bound, or at the block's last place.
    segments = []
    first: TimePlace | None = None
    places = [place for place in block.places if isinstance(place, TimePlace)]
    for number, place in enumerate(places):
        first = first or place
        lasted = place.moment + place.duration - first.moment
        assume(abs(lasted - SEGMENT_BOUND_SECONDS) > 1e-6)
        if (
            ends_sentence(block.text[place.start : place.end])
            or lasted > SEGMENT_BOUND_SECONDS
            or number == len(places) - 1
        ):
            segments.append((first.start, place.end))
            first = None
    return segments


@given(recordings=st.lists(spoken(), min_size=1, max_size=2))
@EXAMPLES
def test_a_recordings_block_is_cut_where_a_sentence_ends_or_the_bound_is_run(
    app: App, recordings: list[list[Utterance]]
) -> None:
    blocks: list[Block] = [recorded(utterances) for utterances in recordings]
    expected = [expected_segments(block) for block in blocks if isinstance(block, Paragraph)]
    app.model.reset(translating())
    document = kept(app, blocks, media_type=RECORDING)
    translated = by_number(translated_whole(app, document.item_id))
    assert set(translated) == set(range(len(blocks))), "a block of a recording was not translated"
    for number, block in enumerate(blocks):
        segments = translated[number]["segments"]
        ranges = [(segment["start"], segment["end"]) for segment in segments]
        assert ranges == expected[number], (
            f"the segments of block {number} of a recording are not cut where a place ends a "
            f"sentence or at the first place's end after {SEGMENT_BOUND_SECONDS} s: {ranges} "
            f"where {expected[number]}"
        )
        for segment in segments:
            assert (
                segment["text"] == f"{TRANSLATED}{block.text[segment['start'] : segment['end']]}"
            ), f"a segment of block {number} does not hold the model's translation of its range"


# R3: tables.


cells = st.one_of(sentences, sentences, st.just(""))
grids = st.integers(1, 4).flatmap(
    lambda width: st.lists(
        st.lists(cells, min_size=width, max_size=width).filter(any), min_size=1, max_size=4
    )
)


def table_of(grid: list[list[str]]) -> Table:
    return Table(text="\n".join("\t".join(row) for row in grid))


def grid_of(text: str) -> list[list[str]]:
    return [row.split("\t") for row in text.split("\n")]


@given(grid=grids)
@EXAMPLES
def test_a_table_is_translated_cell_by_cell_in_its_grid(app: App, grid: list[list[str]]) -> None:
    app.model.reset(translating())
    document = kept(app, [table_of(grid)])
    [block] = translated_whole(app, document.item_id)["blocks"]
    [segment] = block["segments"]
    translated = grid_of(segment["text"])
    assert [len(row) for row in translated] == [len(row) for row in grid], (
        f"a translated table does not keep its grid: {translated} of {grid}"
    )
    expected = [[f"{TRANSLATED}{cell}" if cell else "" for cell in row] for row in grid]
    assert translated == expected, (
        f"a translated table does not hold each cell's translation in its place: {translated}"
    )


# What the model may write between two words of a cell's answer: a space, or one of the
# table's separators.
GAPS = (" ", "\t", "\n")


@given(
    grid=grids.filter(lambda grid: any(" " in cell for row in grid for cell in row)),
    gaps=st.lists(st.sampled_from(GAPS), min_size=1, max_size=6).filter(
        lambda gaps: set(gaps) != {" "}
    ),
)
@EXAMPLES
def test_a_separator_inside_a_cells_answer_is_folded_into_a_space(
    app: App, grid: list[list[str]], gaps: list[str]
) -> None:
    def broken(passage: Passage) -> str:
        words = passage.text.split(" ")
        between = ["", *itertools.islice(itertools.cycle(gaps), len(words) - 1)]
        return TRANSLATED + "".join(
            f"{gap}{word}" for gap, word in zip(between, words, strict=True)
        )

    app.model.reset(translating(broken))
    document = kept(app, [table_of(grid)])
    [block] = translated_whole(app, document.item_id)["blocks"]
    translated = grid_of(block["segments"][0]["text"])
    assert [len(row) for row in translated] == [len(row) for row in grid], (
        f"a cell's answer that holds a separator broke the table's grid: {translated}"
    )
    assert translated == [
        [f"{TRANSLATED}{cell}" if cell else "" for cell in row] for row in grid
    ], f"a separator inside a cell's answer was not folded into a space: {translated}"
    assert len(passage_requests(app)) == 1, (
        "a cell's answer that holds a separator was asked for again, where it is folded and kept"
    )


# R4: marks.


@st.composite
def marked_paragraph(draw: st.DrawFn, formulas: bool = True) -> Paragraph:
    # A paragraph of words, some under a link or a style, and, where asked, formulas, each
    # under a formula mark; at least one word is under no mark.
    kinds = ["word", "link", "style", *(["formula"] if formulas else [])]
    chosen = [*draw(st.lists(st.sampled_from(kinds), min_size=1, max_size=8)), "word"]
    chosen = draw(st.permutations(chosen))
    parts: list[str] = []
    marks: list[Mark] = []
    end = -1
    for kind in chosen:
        text = draw(st.sampled_from(FORMULAS)) if kind == "formula" else draw(sentences)
        start = end + 1
        end = start + len(text)
        parts.append(text)
        match kind:
            case "link":
                marks.append(LinkMark(target=f"{LINK_TARGET}/{len(marks)}", start=start, end=end))
            case "style":
                marks.append(StyleMark(style=draw(st.sampled_from(Style)), start=start, end=end))
            case "formula":
                marks.append(FormulaMark(start=start, end=end))
            case _:
                pass
    return Paragraph(text=" ".join(parts), marks=marks)


@given(paragraph=marked_paragraph())
@EXAMPLES
def test_marks_the_model_keeps_are_kept_over_the_words_they_enclose(
    app: App, paragraph: Paragraph
) -> None:
    app.model.reset(translating())
    document = kept(app, [paragraph])
    [block] = translated_whole(app, document.item_id)["blocks"]
    [segment] = block["segments"]
    assert segment["text"] == f"{TRANSLATED}{paragraph.text}", (
        f"the kept text is not the model's translation without the markup sent: {segment['text']!r}"
    )
    assert segment_marks(block) == shifted(paragraph.marks, len(TRANSLATED)), (
        "the marks kept are not the source's links, styles and formulas over the words the model "
        f"kept them on: {segment['marks']} for {paragraph.marks}"
    )


def tag_pairs(text: str) -> list[tuple[str, str]]:
    # The tags of a passage, as the name and number of each pair, in the order they open.
    return list(
        dict.fromkeys((match[1], match[2]) for match in TAG.finditer(text) if match[0][1] != "/")
    )


def without_tags(text: str, pairs: list[tuple[str, str]]) -> str:
    for name, number in pairs:
        text = text.replace(f"<{name}{number}>", "").replace(f"</{name}{number}>", "")
    return text


@given(paragraph=marked_paragraph(formulas=False), data=st.data())
@EXAMPLES
def test_a_mark_whose_tags_the_model_lost_is_lost_and_half_lost_is_asked_again(
    app: App, paragraph: Paragraph, data: st.DataObject
) -> None:
    assume(paragraph.marks)
    count = len(paragraph.marks)
    lost = data.draw(st.sets(st.integers(0, count - 1), min_size=1), "lost")

    def losing(passage: Passage) -> str:
        pairs = tag_pairs(passage.text)
        return f"{TRANSLATED}{without_tags(passage.text, [pairs[at] for at in lost])}"

    app.model.reset(first_replies(1, translating(losing), translating()))
    document = kept(app, [paragraph])
    [block] = translated_whole(app, document.item_id)["blocks"]
    in_order = sorted(paragraph.marks, key=lambda mark: mark.start)
    asked_again = 2 * len(lost) >= count
    expected = in_order if asked_again else [m for at, m in enumerate(in_order) if at not in lost]
    requests = len(passage_requests(app))
    assert requests == (2 if asked_again else 1), (
        f"a reply that lost {len(lost)} of its {count} marks was "
        f"{'not ' if asked_again else ''}asked for again ({requests} requests)"
    )
    assert block["segments"][0]["text"] == f"{TRANSLATED}{paragraph.text}", (
        "a reply that lost tags was not kept as the text it holds"
    )
    assert segment_marks(block) == shifted(expected, len(TRANSLATED)), (
        f"after a reply lost the tags of marks {sorted(lost)} of {count}, the marks kept are "
        f"not those whose tags it kept: {block['segments'][0]['marks']}"
    )


# Ways a model leaves one of a passage's own tags standing in the text of its reply: one of
# the pair lost, or cut so that it no longer pairs.
UNPAIRED: dict[str, Callable[[str, str], tuple[str, str]]] = {
    "start cut in half": lambda name, number: (f"{name}{number}>", f"</{name}{number}>"),
    "end lost": lambda name, number: (f"<{name}{number}>", ""),
    "start lost": lambda name, number: ("", f"</{name}{number}>"),
}
FULL_WIDTH_OPEN = "\N{FULLWIDTH LESS-THAN SIGN}"
FULL_WIDTH_CLOSE = "\N{FULLWIDTH GREATER-THAN SIGN}"
# Ways a model writes a pair of a passage's tags otherwise than they were sent: in another case,
# with spaces, or in other brackets.
DISGUISED: dict[str, Callable[[str, str], tuple[str, str]]] = {
    "upper case": lambda name, number: (f"<{name.upper()}{number}>", f"</{name.upper()}{number}>"),
    "spaces": lambda name, number: (f"< {name}{number} >", f"< /{name}{number} >"),
    "full-width brackets": lambda name, number: (
        f"{FULL_WIDTH_OPEN}{name}{number}{FULL_WIDTH_CLOSE}",
        f"{FULL_WIDTH_OPEN}/{name}{number}{FULL_WIDTH_CLOSE}",
    ),
    "Chinese brackets": lambda name, number: (f"〈{name}{number}〉", f"〈/{name}{number}〉"),
    "title brackets": lambda name, number: (f"《{name}{number}》", f"《/{name}{number}》"),
}


def garbled_once(
    app: App, paragraph: Paragraph, at: int, garbling: Callable[[str, str], tuple[str, str]]
) -> tuple[dict[str, Any], int]:
    # The paragraph translated by a model whose first reply writes the tags of its mark at
    # the place given as the garbling does, and the number of requests it took.
    def garbled(passage: Passage) -> str:
        name, number = tag_pairs(passage.text)[at]
        opening, closing = garbling(name, number)
        text = passage.text.replace(f"<{name}{number}>", opening)
        return f"{TRANSLATED}{text.replace(f'</{name}{number}>', closing)}"

    app.model.reset(first_replies(1, translating(garbled), translating()))
    document = kept(app, [paragraph])
    [block] = translated_whole(app, document.item_id)["blocks"]
    return block, len(passage_requests(app))


@pytest.mark.parametrize("garbling", sorted(UNPAIRED))
@given(paragraph=marked_paragraph(), data=st.data())
@FEW_EXAMPLES
def test_a_reply_in_which_one_of_its_passages_tags_stands_unpaired_is_asked_again(
    app: App, garbling: str, paragraph: Paragraph, data: st.DataObject
) -> None:
    assume(paragraph.marks)
    at = data.draw(st.integers(0, len(paragraph.marks) - 1), "garbled")
    block, requests = garbled_once(app, paragraph, at, UNPAIRED[garbling])
    [segment] = block["segments"]
    assert segment["text"] == f"{TRANSLATED}{paragraph.text}", (
        f"a reply in which a tag stands unpaired ({garbling}) was kept: {segment['text']!r}"
    )
    assert (requests, segment_marks(block)) == (2, shifted(paragraph.marks, len(TRANSLATED))), (
        f"a reply in which a tag stands unpaired ({garbling}) was not asked for again: it took "
        f"{requests} requests and kept the marks {segment['marks']}"
    )


@pytest.mark.parametrize("garbling", sorted(DISGUISED))
@given(paragraph=marked_paragraph(), data=st.data())
@FEW_EXAMPLES
def test_a_tag_written_otherwise_is_read_as_its_mark_or_asked_again_never_kept_as_text(
    app: App, garbling: str, paragraph: Paragraph, data: st.DataObject
) -> None:
    # Whether such a pair stands rightly is the part's to read; what it keeps of it is judged:
    # nothing of the tag in the text, and the mark it encloses either kept or asked again.
    assume(paragraph.marks)
    at = data.draw(st.integers(0, len(paragraph.marks) - 1), "garbled")
    block, requests = garbled_once(app, paragraph, at, DISGUISED[garbling])
    [segment] = block["segments"]
    assert segment["text"] == f"{TRANSLATED}{paragraph.text}", (
        f"a reply with a tag written in {garbling} was kept with the tag in its text: "
        f"{segment['text']!r}"
    )
    assert segment_marks(block) == shifted(paragraph.marks, len(TRANSLATED)), (
        f"a reply with a tag written in {garbling} was kept without its mark "
        f"({requests} requests): {segment['marks']}"
    )


# Text a source may hold that looks like the part's tags or holds brackets, beside a formula:
# a bracketed title, an interval, an index inside a formula, a tag's word written in the
# source itself, and a tag's word in other brackets.
SOURCE_BRACKETS = (
    ("《F1》", "\\alpha"),
    ("[a-1, a+1]", "\\alpha"),
    ("quorvex", "x[i-1]"),
    ("a1", "[0, 1)"),
    ("〈f2〉", "\\alpha"),
)


@pytest.mark.parametrize(("bracketed", "formula"), SOURCE_BRACKETS)
@given(paragraph=marked_paragraph())
@UNSHRUNK_EXAMPLES
def test_what_the_source_text_holds_is_kept_whatever_tags_the_passage_was_sent_with(
    app: App, bracketed: str, formula: str, paragraph: Paragraph
) -> None:
    lead = f"{bracketed} quorvex "
    text = f"{lead}{formula} {paragraph.text}"
    moved = len(lead) + len(formula) + 1
    marks: list[Mark] = [
        FormulaMark(start=len(lead), end=len(lead) + len(formula)),
        *(
            mark.model_copy(update={"start": mark.start + moved, "end": mark.end + moved})
            for mark in paragraph.marks
        ),
    ]
    app.model.reset(translating())
    document = kept(app, [Paragraph(text=text, marks=marks)])
    [block] = translated_whole(app, document.item_id)["blocks"]
    assert block["segments"][0]["text"] == f"{TRANSLATED}{text}", (
        f"a reply that holds what the source's text holds ({bracketed}, {formula}) was not kept "
        f"as written: {block['segments'][0]['text']!r}"
    )
    assert segment_marks(block) == shifted(marks, len(TRANSLATED)), (
        "the marks of a passage whose source holds bracketed text were not kept"
    )
    assert len(passage_requests(app)) == 1, (
        f"a reply that holds what the source's text holds ({bracketed}, {formula}) was asked again"
    )


# Ways a model alters the text of a formula it copies, none of them bringing a letter the
# formula does not hold: spaces dropped or added, backslashes doubled or lost, braces lost,
# and the control characters that a backslash and a letter become when the model writes
# them unescaped into JSON.
FORMULA_ALTERATIONS: dict[str, Callable[[str], str]] = {
    "spaces dropped": lambda formula: formula.replace(" ", ""),
    "spaces added": lambda formula: " ".join(formula),
    "backslashes doubled": lambda formula: formula.replace("\\", "\\\\"),
    "backslashes lost": lambda formula: formula.replace("\\", ""),
    "braces lost": lambda formula: formula.replace("{", "").replace("}", ""),
    "control characters": lambda formula: (
        formula.replace("\\f", "\f").replace("\\b", "\b").replace("\\t", "\t")
    ),
}
FORMULA_TAG = "f"
# Words of a sentence that a model may put inside a formula's tags.
PROSE = ("the energy of the quorvex", "where zindlap holds", "harbour")


def formula_replaced(passage: Passage, at: int, inside: Callable[[str], str | None]) -> str:
    # The passage translated, with the text between the tags of its formula at the place
    # given as the function makes it; where it makes none, the formula's tags are lost.
    pairs = [pair for pair in tag_pairs(passage.text) if pair[0] == FORMULA_TAG]
    name, number = pairs[at % len(pairs)]
    opening, closing = f"<{name}{number}>", f"</{name}{number}>"
    before, rest = passage.text.split(opening, 1)
    formula, after = rest.split(closing, 1)
    written = inside(formula)
    if written is None:
        return f"{TRANSLATED}{before}{formula}{after}"
    return f"{TRANSLATED}{before}{opening}{written}{closing}{after}"


with_formulas = marked_paragraph().filter(
    lambda paragraph: any(isinstance(mark, FormulaMark) for mark in paragraph.marks)
)


@given(
    paragraph=with_formulas,
    alterations=st.lists(st.sampled_from(sorted(FORMULA_ALTERATIONS)), min_size=1, max_size=3),
    at=st.integers(0, 7),
)
@EXAMPLES
def test_a_formula_altered_without_a_letter_of_its_own_is_kept_as_the_source_has_it(
    app: App, paragraph: Paragraph, alterations: list[str], at: int
) -> None:
    def altered(formula: str) -> str:
        for alteration in alterations:
            formula = FORMULA_ALTERATIONS[alteration](formula)
        return formula

    app.model.reset(translating(lambda passage: formula_replaced(passage, at, altered)))
    document = kept(app, [paragraph])
    [block] = translated_whole(app, document.item_id)["blocks"]
    assert block["segments"][0]["text"] == f"{TRANSLATED}{paragraph.text}", (
        f"a formula whose text came back with {alterations} was not kept as the source has it: "
        f"{block['segments'][0]['text']!r}"
    )
    assert segment_marks(block) == shifted(paragraph.marks, len(TRANSLATED)), (
        "the formula marks kept are not over the source's formulas"
    )
    assert len(passage_requests(app)) == 1, (
        f"a reply whose formula came back with {alterations} and no letter of its own was asked "
        "for again"
    )


@given(
    paragraph=with_formulas,
    change=st.sampled_from(["tags lost", *PROSE]),
    at=st.integers(0, 7),
)
@EXAMPLES
def test_a_formula_whose_tags_are_lost_or_that_holds_prose_is_asked_again(
    app: App, paragraph: Paragraph, change: str, at: int
) -> None:
    def inside(formula: str) -> str | None:
        return None if change == "tags lost" else f"{formula} {change}"

    if change != "tags lost":
        formulas = [
            paragraph.text[mark.start : mark.end]
            for mark in paragraph.marks
            if isinstance(mark, FormulaMark)
        ]
        chosen = formulas[at % len(formulas)]
        assume(any(letter.isalpha() and letter not in chosen for letter in change))
    app.model.reset(
        first_replies(
            1,
            translating(lambda passage: formula_replaced(passage, at, inside)),
            translating(),
        )
    )
    document = kept(app, [paragraph])
    [block] = translated_whole(app, document.item_id)["blocks"]
    assert len(passage_requests(app)) == 2, (
        f"a reply whose formula {'lost its tags' if change == 'tags lost' else 'held prose'} "
        "was not asked for again"
    )
    assert block["segments"][0]["text"] == f"{TRANSLATED}{paragraph.text}", (
        f"the reply asked for again in place of one whose formula was {change!r} was not kept: "
        f"{block['segments'][0]['text']!r}"
    )
    assert segment_marks(block) == shifted(paragraph.marks, len(TRANSLATED)), (
        "the formula marks kept are not over the source's formulas"
    )
