from random import Random
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from hypothesis import HealthCheck, Phase, assume, given, settings
from hypothesis import strategies as st

from infonium.anchor import Anchor
from infonium.document import HTML_MEDIA_TYPE, Document, Paragraph, document_of
from infonium.library import LOCAL_OWNER
from infonium.passage import Found, Lost, Passage, Standing, standing_in
from tests.notebook import (
    LOST,
    NOWHERE_DOCUMENT,
    NOWHERE_ITEM,
    block_texts,
    found,
    passage,
    phrase_passage,
    refusal,
    reread,
    spans_in,
)
from tests.notebook import document_of as served_document
from tests.revising import (
    COMMON_WORDS,
    FIRST_EXTRACTOR,
    ITEM,
    NEVER_SAID_AGAIN,
    SECOND_EXTRACTOR,
    SENTENCE_END,
    SNAPSHOT,
    Change,
    Coiner,
    Cut,
    Expectation,
    Outcome,
    Revised,
    Unit,
    UnitKind,
    WordSpan,
    anchors_over,
    coiners,
    document_letters,
    expectation,
    first_units,
    judge_standing,
    laid_out,
    marked_passage,
    reference,
    renumbered_word,
    retyped_word,
    revisions,
    rows,
    sentences,
    spaced,
    span_words,
    unit_of_kind,
    word_spans,
)
from tests.service import LEDGER, REVISED, App, article, as_owner, keep_sources, new_owner

# The judgments of brief 01's R6, as the lead amended it, on standing_in, the search that finds
# a passage of one revision in another, as a pure function of documents.
PASSAGES_AN_EXAMPLE = 6
EVERY_CHANGE = tuple(Change)
UNCHANGED_WORDS = (Change.KEPT, Change.RETYPED)
OWN_UNITS = (UnitKind.SENTENCE, UnitKind.UNSPACED, UnitKind.FORMULA, UnitKind.ROW)
EVERY_UNIT = tuple(UnitKind)
SAID_AGAIN = (UnitKind.BOILERPLATE, UnitKind.PLAIN_ROW, UnitKind.SENTENCE)
TABLE_UNITS = (UnitKind.ROW, UnitKind.PLAIN_ROW, UnitKind.ROW)
TABLE_CHANGES = (Change.KEPT, Change.KEPT, Change.RENUMBERED, Change.REMOVED, Change.REWRITTEN)
ENTRY_CHANGES = (Change.REMOVED, Change.REWRITTEN, Change.RENUMBERED)
SURROUNDING_SENTENCES = 2
SECTION_SENTENCES = 2
SHORT_WORDS = COMMON_WORDS[:8]
SENTENCE_WORDS = 90
# Words of a sentence that change beside a marked word, and how far from it the far changes
# begin, counted in words.
BESIDE_WORDS = 4
FAR_WORDS = 34
EQUAL_VALUE = "4.5"
TABLE_ROWS = 8
LONG_DOCUMENT_BLOCKS = 2_000
MANY_PASSAGES = 300
LONG_DOCUMENT_SEED = 20261124
# A page read twice: its second form has one paragraph rewritten.
PAGE_BEFORE = article("Quillmoor ledger", LEDGER[:4])
PAGE_AFTER = article("Quillmoor ledger", (LEDGER[0], REVISED[0], *LEDGER[2:4]))
SLOW = [HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much]
# A judgment marked as failing at c248e4b reports the first example that fails as it was
# made: shrinking it would take minutes of every run.
UNSHRUNK = (Phase.explicit, Phase.reuse, Phase.generate)


# The judgments of R6 as amended that the part as committed (c248e4b) fails; the lead takes
# the marks off when the mended part passes them.
def failing_at_c248e4b(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(reason=f"at c248e4b {reason}", strict=False)


GLUED_EXACT = failing_at_c248e4b("words glued together where two blocks were joined are exact")
OTHER_COPY = failing_at_c248e4b(
    "a passage said again is found at another copy of its words, its own copy standing or gone"
)
OWN_COPY_GONE = failing_at_c248e4b(
    "a sentence said in every section is found in another section once its own is removed"
)
NEAR_ELSEWHERE = failing_at_c248e4b(
    "words read a little otherwise are now and then found in a sentence that differs more from "
    "them, or from inside a word"
)
WRONG_PLACES = failing_at_c248e4b(
    "a passage is found in a line alike to its own, in another copy, inside a word or a number, "
    "or across a number changed; and a short passage whose surroundings all stand is lost"
)
OTHER_CELL = failing_at_c248e4b(
    "a cell is found in another row's cell of the same value, its own row standing or changed"
)
OTHER_ENTRY = failing_at_c248e4b(
    "words of an entry removed, rewritten or renumbered are found in another entry"
)


def standing_of(revised: Revised, span: WordSpan) -> Standing:
    return standing_in(
        marked_passage(revised, span), revised.first.document, revised.second.document
    )


def judged_spans(revised: Revised, spans: list[WordSpan]) -> list[Expectation]:
    # Each passage judged where the search says it stands.
    first = document_letters(revised.first.document)
    second = document_letters(revised.second.document)
    return [
        judge_standing(revised, span, standing_of(revised, span), first, second) for span in spans
    ]


def whole_unit(units: list[Unit], number: int) -> WordSpan:
    return WordSpan(
        first_unit=number, first_word=0, last_unit=number, last_word=len(units[number].words) - 1
    )


def revised_as_given(
    first: list[Unit],
    second: list[Unit],
    origins: list[int | None],
    first_cuts: list[Cut],
    second_cuts: list[Cut],
) -> Revised:
    # A second reading whose every unit the test made itself: each with the unit of the first
    # it was made from, word for word.
    return Revised(
        first_units=first,
        second_units=second,
        origins=origins,
        changes=[
            Change.KEPT if number in origins else Change.REMOVED for number in range(len(first))
        ],
        first=laid_out(first, first_cuts, FIRST_EXTRACTOR),
        second=laid_out(second, second_cuts, SECOND_EXTRACTOR),
    )


def blocks_of(units: list[Unit]) -> list[Cut]:
    return [Cut(before_unit=True, before_word=None) for _ in units]


@given(data=st.data(), blocks=st.lists(block_texts, min_size=1, max_size=25))
@settings(max_examples=60)
def test_a_passage_stands_where_it_was_marked_in_its_own_revision(
    data: st.DataObject, blocks: list[str]
) -> None:
    document = document_of(
        item_id=ITEM,
        extractor=FIRST_EXTRACTOR,
        snapshot=SNAPSHOT,
        media_type=HTML_MEDIA_TYPE,
        blocks=[Paragraph(text=text, marks=[]) for text in blocks],
    )
    marked = Passage(
        anchors=[
            Anchor(
                document_id=document.id,
                block_number=number,
                start=start,
                end=end,
                quote=blocks[number][start:end],
            )
            for number, start, end in data.draw(spans_in(blocks))
        ]
    )
    standing = standing_in(marked, document, document)
    assert standing == Found(passage=marked, exact=True), (
        f"R6a: a passage does not stand where it was marked in its own revision: {standing}"
    )


@given(data=st.data())
@settings(max_examples=150, suppress_health_check=SLOW)
def test_the_same_words_are_found_exact_with_anchors_of_their_own_however_the_blocks_are_cut(
    data: st.DataObject,
) -> None:
    # Case, spacing, the shapes of quotation marks and dashes, composed accents and invisible
    # characters change; the blocks are cut anew, inside sentences too; every script.
    coiner = data.draw(coiners())
    units = data.draw(first_units(coiner, OWN_UNITS, said_again=NEVER_SAID_AGAIN))
    revised = data.draw(revisions(coiner, units, changes=UNCHANGED_WORDS, put_in=False))
    spans = [
        whole_unit(units, data.draw(st.integers(0, len(units) - 1))),
        *(data.draw(word_spans(units)) for _ in range(PASSAGES_AN_EXAMPLE)),
    ]
    judged = judged_spans(revised, spans)
    assert judged[0].outcome == Outcome.FOUND_EXACT, (
        "the test made no passage its rule holds the search to: the test's fault"
    )


@GLUED_EXACT
@given(data=st.data(), glued=st.booleans())
@settings(max_examples=60, suppress_health_check=SLOW, phases=UNSHRUNK)
def test_words_of_two_blocks_joined_are_exact_with_a_space_between_and_not_glued_together(
    data: st.DataObject, glued: bool
) -> None:
    coiner = data.draw(coiners())
    units = [data.draw(sentences(coiner)) for _ in range(4)]
    joined = [
        Cut(before_unit=number != 2, before_word=None, joiner="" if glued else " ")
        for number in range(len(units))
    ]
    revised = revised_as_given(units, units, list(range(len(units))), blocks_of(units), joined)
    marked = WordSpan(
        first_unit=1,
        first_word=data.draw(st.integers(0, len(units[1].words) // 2)),
        last_unit=2,
        last_word=data.draw(st.integers(len(units[2].words) // 2, len(units[2].words) - 1)),
    )
    standing = standing_of(revised, marked)
    expected = anchors_over(revised.second, span_words(marked, units))
    assert isinstance(standing, Found) and standing.passage.anchors == expected, (
        f"R6b: a passage over two paragraphs joined into one block was not found where its words "
        f"stand: {standing}"
    )
    assert standing.exact != glued, (
        "R6 (exact): a passage whose words two blocks parted is said "
        + ("exact though they now stand glued together" if glued else "not exact, though the ")
        + ("" if glued else "blocks were only joined with a space")
    )


@st.composite
def told_by_surroundings(draw: st.DrawFn) -> tuple[Revised, int]:
    # A document that says the same words in several places, one of them between sentences of
    # its own: the words are marked there.
    coiner = draw(coiners())
    said_again = draw(unit_of_kind(draw(st.sampled_from(SAID_AGAIN)), coiner))
    before = draw(first_units(coiner, EVERY_UNIT, most_runs=3))
    after = draw(first_units(coiner, EVERY_UNIT, most_runs=3))
    around = [draw(sentences(coiner)) for _ in range(2 * SURROUNDING_SENTENCES)]
    for _ in range(draw(st.integers(1, 3))):
        elsewhere = draw(st.sampled_from([before, after]))
        elsewhere.insert(draw(st.integers(0, len(elsewhere))), said_again)
    units = [
        *before,
        *around[:SURROUNDING_SENTENCES],
        said_again,
        *around[SURROUNDING_SENTENCES:],
        *after,
    ]
    target = len(before) + SURROUNDING_SENTENCES
    guarded = range(target - SURROUNDING_SENTENCES, target + SURROUNDING_SENTENCES + 1)
    fixed = frozenset((number, Change.KEPT) for number in guarded)
    return draw(revisions(coiner, units, EVERY_CHANGE, put_in=True, fixed=fixed)), target


@OTHER_COPY
@given(data=st.data(), made=told_by_surroundings())
@settings(max_examples=150, suppress_health_check=SLOW, phases=UNSHRUNK)
def test_where_the_words_stand_twice_the_place_is_told_by_what_stands_around_them(
    data: st.DataObject, made: tuple[Revised, int]
) -> None:
    revised, target = made
    marked = data.draw(word_spans(revised.first_units, first_unit=target, most_units=1))
    [expected] = judged_spans(revised, [marked])
    assume(expected.outcome == Outcome.FOUND_EXACT)
    judged_spans(
        revised,
        [data.draw(word_spans(revised.first_units)) for _ in range(PASSAGES_AN_EXAMPLE)],
    )


@st.composite
def sections_one_removed(draw: st.DrawFn) -> tuple[Revised, int]:
    # Sections of sentences of their own, each ending in the same sentence; one section gone.
    coiner = draw(coiners())
    said_again = draw(unit_of_kind(UnitKind.BOILERPLATE, coiner))
    sections = draw(st.integers(2, 5))
    units = [
        unit
        for _ in range(sections)
        for unit in [*(draw(sentences(coiner)) for _ in range(SECTION_SENTENCES)), said_again]
    ]
    removed = draw(st.integers(0, sections - 1))
    size = SECTION_SENTENCES + 1
    fixed = frozenset(
        (number, Change.REMOVED if number // size == removed else Change.KEPT)
        for number in range(len(units))
    )
    revised = draw(revisions(coiner, units, UNCHANGED_WORDS, put_in=False, fixed=fixed))
    return revised, removed * size + SECTION_SENTENCES


@OWN_COPY_GONE
@given(data=st.data(), made=sections_one_removed())
@settings(max_examples=100, suppress_health_check=SLOW, phases=UNSHRUNK)
def test_a_sentence_said_in_every_section_is_lost_with_its_own_section(
    data: st.DataObject, made: tuple[Revised, int]
) -> None:
    revised, target = made
    units = revised.first_units
    marked = data.draw(word_spans(units, first_unit=target, most_units=1))
    [gone] = judged_spans(revised, [marked])
    assert gone.outcome == Outcome.LOST, "the test did not remove the marked copy's section"
    judged_spans(revised, [whole_unit(units, number) for number in range(len(units))])


@given(data=st.data(), beside=st.booleans())
@settings(max_examples=100, suppress_health_check=SLOW)
def test_a_word_is_lost_when_its_neighbours_change_beside_it_and_found_when_they_change_far(
    data: st.DataObject, beside: bool
) -> None:
    coiner = data.draw(coiners())
    words = data.draw(
        st.lists(st.sampled_from(SHORT_WORDS), min_size=SENTENCE_WORDS, max_size=SENTENCE_WORDS)
    )
    marked_word = SENTENCE_WORDS // 2
    assume(words.count(words[marked_word]) > 1)
    changed = (
        range(marked_word - BESIDE_WORDS, marked_word + BESIDE_WORDS + 1)
        if beside
        else [*range(marked_word - FAR_WORDS - BESIDE_WORDS, marked_word - FAR_WORDS),
              *range(marked_word + FAR_WORDS, marked_word + FAR_WORDS + BESIDE_WORDS)]
    )  # fmt: skip
    second_words = [
        coiner.rare() if number in changed and number != marked_word else word
        for number, word in enumerate(words)
    ]
    first = [spaced(UnitKind.SENTENCE, words)]
    second = [spaced(UnitKind.SENTENCE, second_words)]
    revised = revised_as_given(first, second, [0], blocks_of(first), blocks_of(second))
    marked = WordSpan(first_unit=0, first_word=marked_word, last_unit=0, last_word=marked_word)
    first_letters = document_letters(revised.first.document)
    second_letters = document_letters(revised.second.document)
    meant = Outcome.LOST if beside else Outcome.FOUND_EXACT
    assume(expectation(revised, marked, first_letters, second_letters).outcome == meant)
    judged_spans(revised, [marked])


@st.composite
def read_a_little_otherwise(draw: st.DrawFn) -> tuple[Revised, int]:
    # Two sentences of their own read a little otherwise between sentences kept, among units
    # changed in every way.
    coiner = draw(coiners())
    before = draw(first_units(coiner, EVERY_UNIT, most_runs=3))
    after = draw(first_units(coiner, EVERY_UNIT, most_runs=3))
    around = [draw(sentences(coiner)) for _ in range(2 * SURROUNDING_SENTENCES)]
    target = len(before) + SURROUNDING_SENTENCES
    units = [
        *before,
        *around[:SURROUNDING_SENTENCES],
        draw(sentences(coiner)),
        draw(sentences(coiner)),
        *around[SURROUNDING_SENTENCES:],
        *after,
    ]
    fixed = frozenset(
        {(target, Change.VARIED), (target + 1, Change.VARIED)}
        | {
            (number, Change.KEPT)
            for number in [
                *range(target - SURROUNDING_SENTENCES, target),
                *range(target + 2, target + 2 + SURROUNDING_SENTENCES),
            ]
        }
    )
    return draw(revisions(coiner, units, EVERY_CHANGE, put_in=True, fixed=fixed)), target


@NEAR_ELSEWHERE
@given(data=st.data(), made=read_a_little_otherwise())
@settings(max_examples=150, suppress_health_check=SLOW, phases=UNSHRUNK)
def test_words_that_differ_a_little_are_found_where_they_stand_and_not_as_exact(
    data: st.DataObject, made: tuple[Revised, int]
) -> None:
    revised, target = made
    units = revised.first_units
    marked = WordSpan(
        first_unit=target,
        first_word=data.draw(st.integers(0, len(units[target].words) // 4)),
        last_unit=target + 1,
        last_word=data.draw(
            st.integers(3 * len(units[target + 1].words) // 4, len(units[target + 1].words) - 1)
        ),
    )
    [expected] = judged_spans(revised, [marked])
    assume(expected.outcome == Outcome.FOUND_NEAR)


@WRONG_PLACES
@given(data=st.data())
@settings(max_examples=400, suppress_health_check=SLOW, phases=UNSHRUNK)
def test_a_passage_is_found_only_where_its_words_stand_and_lost_where_they_are_gone(
    data: st.DataObject,
) -> None:
    # Every unit and every change: sentences said again, lists of lines alike, tables of
    # numbers, references on one template, formulas, paragraphs rewritten or removed, numbers
    # changed, lines put in, blocks cut anew.
    coiner = data.draw(coiners())
    units = data.draw(first_units(coiner, EVERY_UNIT))
    revised = data.draw(revisions(coiner, units, EVERY_CHANGE, put_in=True))
    judged_spans(revised, [data.draw(word_spans(units)) for _ in range(PASSAGES_AN_EXAMPLE)])


@OTHER_CELL
@given(data=st.data())
@settings(max_examples=200, suppress_health_check=SLOW, phases=UNSHRUNK)
def test_a_cell_or_line_of_a_table_is_found_at_its_place_and_lost_with_it_never_at_one_alike(
    data: st.DataObject,
) -> None:
    coiner = data.draw(coiners())
    units = data.draw(first_units(coiner, TABLE_UNITS, most_runs=3))
    revised = data.draw(revisions(coiner, units, TABLE_CHANGES, put_in=True))
    cells = [
        WordSpan(first_unit=number, first_word=cell, last_unit=number, last_word=cell)
        for number in range(len(units))
        for cell in [data.draw(st.integers(0, len(units[number].words) - 1))]
    ]
    judged_spans(revised, [*(whole_unit(units, number) for number in range(len(units))), *cells])


@OTHER_CELL
@given(data=st.data(), changed=st.booleans())
@settings(max_examples=60, suppress_health_check=SLOW, phases=UNSHRUNK)
def test_a_cell_is_found_in_its_place_when_a_row_is_put_in_above_and_lost_when_it_changed(
    data: st.DataObject, changed: bool
) -> None:
    # A table whose second column holds one value in every row.
    coiner = data.draw(coiners())
    table = []
    for _ in range(TABLE_ROWS):
        row = data.draw(rows(coiner))
        table.append(spaced(UnitKind.ROW, [row.words[0], EQUAL_VALUE, *row.words[2:]]))
    marked_row = data.draw(st.integers(1, TABLE_ROWS - 2))
    cell = data.draw(st.integers(1, len(table[marked_row].words) - 2))
    second_rows = list(table)
    if changed:
        words = list(table[marked_row].words)
        words[cell] = renumbered_word(words[cell])
        second_rows[marked_row] = spaced(UnitKind.ROW, words)
    put_in = data.draw(rows(coiner, fresh=True))
    second = [*second_rows[:marked_row], put_in, *second_rows[marked_row:]]
    origins: list[int | None] = [
        *range(marked_row),
        None,
        *range(marked_row, TABLE_ROWS),
    ]
    one_table = [Cut(before_unit=False, before_word=None) for _ in table]
    revised = revised_as_given(
        table, second, origins, one_table, [Cut(before_unit=False, before_word=None)] * len(second)
    )
    marked = WordSpan(first_unit=marked_row, first_word=cell, last_unit=marked_row, last_word=cell)
    standing = standing_of(revised, marked)
    [expected] = judged_spans(revised, [marked])
    if changed:
        assert isinstance(standing, Lost), (
            f"R6 (table cells): a marked cell whose value changed was not lost: {standing}"
        )
    else:
        assert isinstance(standing, Found) and (
            standing.exact and standing.passage.anchors == expected.expected_anchors
        ), (
            f"R6 (table cells): a cell with a row put in above it was not found exact in its own "
            f"place, {expected.expected_anchors}: {standing}"
        )


@OTHER_ENTRY
@given(data=st.data(), change=st.sampled_from(ENTRY_CHANGES))
@settings(max_examples=150, suppress_health_check=SLOW, phases=UNSHRUNK)
def test_an_entry_of_a_list_of_references_removed_rewritten_or_renumbered_is_lost(
    data: st.DataObject, change: Change
) -> None:
    coiner = data.draw(coiners())
    entries = [reference(coiner) for _ in range(data.draw(st.integers(3, 12)))]
    target = data.draw(st.integers(0, len(entries) - 1))
    fixed = frozenset(
        (number, change if number == target else Change.KEPT) for number in range(len(entries))
    )
    revised = data.draw(revisions(coiner, entries, UNCHANGED_WORDS, put_in=False, fixed=fixed))
    spans = [
        *(whole_unit(entries, number) for number in range(len(entries))),
        data.draw(word_spans(entries, first_unit=target, most_units=1)),
    ]
    judged = judged_spans(revised, spans)
    assert judged[target].outcome == Outcome.LOST, (
        f"the test's change of an entry left it standing: {change}: the test's fault"
    )


def long_revisions() -> Revised:
    # A document of 2,000 blocks of a sentence each, and its second reading: some sentences
    # typeset otherwise, some cut in two, some removed and some rewritten.
    chosen = Random(LONG_DOCUMENT_SEED)
    coiner = Coiner(offset=0, stride=1)
    units: list[Unit] = []
    for number in range(LONG_DOCUMENT_BLOCKS):
        words = chosen.sample(COMMON_WORDS, 8)
        words.insert(3, coiner.rare())
        words.append(f"{coiner.rare()}{number}{SENTENCE_END}")
        units.append(spaced(UnitKind.SENTENCE, words))
    second: list[Unit] = []
    origins: list[int | None] = []
    changes: list[Change] = []
    for number, unit in enumerate(units):
        change = [Change.KEPT, Change.RETYPED, Change.KEPT, Change.REMOVED, Change.REWRITTEN][
            chosen.randrange(5)
        ]
        changes.append(change)
        match change:
            case Change.REMOVED:
                continue
            case Change.RETYPED:
                second.append(
                    Unit(
                        kind=unit.kind,
                        words=[retyped_word(word, chosen.randrange(5)) for word in unit.words],
                        gaps=unit.gaps,
                    )
                )
            case Change.REWRITTEN:
                second.append(spaced(UnitKind.SENTENCE, [coiner.rare(), *unit.words[1:4]]))
            case _:
                second.append(unit)
        origins.append(number if change != Change.REWRITTEN else None)
    second_cuts = [
        Cut(before_unit=True, before_word=4 if chosen.randrange(7) == 0 else None) for _ in second
    ]
    return Revised(
        first_units=units,
        second_units=second,
        origins=origins,
        changes=changes,
        first=laid_out(units, blocks_of(units), FIRST_EXTRACTOR),
        second=laid_out(second, second_cuts, SECOND_EXTRACTOR),
    )


def test_three_hundred_passages_of_two_thousand_blocks_each_get_a_standing_and_the_same_twice() -> (
    None
):
    revised = long_revisions()
    chosen = Random(LONG_DOCUMENT_SEED)
    spans = []
    for number in range(MANY_PASSAGES):
        unit = (number * 677) % LONG_DOCUMENT_BLOCKS
        last = min(unit + chosen.randrange(2), LONG_DOCUMENT_BLOCKS - 1)
        spans.append(
            WordSpan(
                first_unit=unit,
                first_word=chosen.randrange(len(revised.first_units[unit].words) - 1),
                last_unit=last,
                last_word=len(revised.first_units[last].words) - 1,
            )
        )
    standings = [standing_of(revised, span) for span in spans]
    again = [standing_of(revised, span) for span in spans]
    assert all(isinstance(standing, Found | Lost) for standing in standings), (
        "R6g: a passage of a long document was given no standing"
    )
    assert again == standings, "R6g: asking for the standings again gave other answers"
    found = [isinstance(standing, Found) and standing.exact for standing in standings]
    unchanged = [
        all(
            revised.changes[unit] in UNCHANGED_WORDS
            for unit in range(span.first_unit, span.last_unit + 1)
        )
        for span in spans
    ]
    assert sum(unchanged) > MANY_PASSAGES // 3, "the test changed too many passages: its fault"
    assert all(was_found for was_found, kept in zip(found, unchanged, strict=True) if kept), (
        "R6g, R6b: a passage of a long document whose words stand unchanged was not found exact"
    )


def standing_asked(app: App, item: str, marked: dict[str, Any]) -> httpx2.Response:
    return app.client.post(f"/api/items/{item}/standing", json=marked)


def kept_document(app: App, document_id: str) -> Document:
    with app.store.library(LOCAL_OWNER) as library:
        return library.owned_document(document_id)


def test_the_endpoint_answers_where_a_passage_of_any_revision_stands_in_the_document_now(
    app: App,
) -> None:
    name = uuid4().hex
    item = app.read_page(name, PAGE_BEFORE)
    first = served_document(app, item)
    kept = phrase_passage(app, item, [(LEDGER[2], "forty-one nights")])
    gone = phrase_passage(app, item, [(LEDGER[1], "Odalys Brenn")])
    reread(app, item, name, PAGE_AFTER)
    second = served_document(app, item)
    added = phrase_passage(app, item, [(REVISED[0], "rewritten from memory")])
    answers = {
        "kept": standing_asked(app, item, kept),
        "gone": standing_asked(app, item, gone),
        "added": standing_asked(app, item, added),
    }
    assert all(answer.status_code == 200 for answer in answers.values()), (
        f"R6: the standing of a passage of a revision of the item was refused: {answers}"
    )
    for marked, answer in ((kept, answers["kept"]), (gone, answers["gone"])):
        direct = standing_in(
            Passage.model_validate(marked),
            kept_document(app, first["id"]),
            kept_document(app, second["id"]),
        )
        assert answer.json() == direct.model_dump(mode="json"), (
            "R6: the endpoint does not answer where standing_in finds the passage in the "
            f"document the item holds now: {answer.json()} against {direct}"
        )
    standing = answers["kept"].json()
    assert standing["kind"] == "found" and standing["exact"], (
        f"R6b: a passage of a paragraph kept was not found exact: {standing}"
    )
    assert {anchor["document_id"] for anchor in standing["passage"]["anchors"]} == {second["id"]}, (
        "R6: a standing is not in the document the item holds now"
    )
    assert answers["gone"].json() == LOST, "R6e: a passage of a paragraph removed was not lost"
    assert answers["added"].json() == found(added), (
        "R6a: a passage of the document the item holds now does not stand where it is"
    )
    reread(app, item, name, PAGE_BEFORE)
    back = standing_asked(app, item, added)
    assert back.status_code == 200 and back.json() == LOST, (
        f"R6: a passage of a later revision does not stand in the earlier document the item "
        f"holds now: {back.text}"
    )
    assert standing_asked(app, item, gone).json() == found(gone), (
        "R6a: a passage stands nowhere once its item holds its document again"
    )


def test_the_endpoint_refuses_a_passage_of_no_revision_of_the_item_or_one_not_held(
    app: App,
) -> None:
    blocks = [LEDGER[0], LEDGER[1]]
    mine, other = keep_sources(app.store, LOCAL_OWNER, [("Mine", blocks), ("Other", blocks)])
    held = passage(mine.document, blocks, [(0, 4, 9)])
    misquoted = {"anchors": [{**held["anchors"][0], "quote": "other"}]}
    beyond = {"anchors": [{**held["anchors"][0], "block_number": len(blocks)}]}
    refused = {
        "a passage of a document no revision of the item": (
            standing_asked(app, mine.item, passage(NOWHERE_DOCUMENT, blocks, [(0, 4, 9)])),
            404,
        ),
        "a passage of another item's document": (
            standing_asked(app, mine.item, passage(other.document, blocks, [(0, 4, 9)])),
            404,
        ),
        "a passage whose quote its document does not hold": (
            standing_asked(app, mine.item, misquoted),
            409,
        ),
        "a passage of a block its document does not have": (
            standing_asked(app, mine.item, beyond),
            409,
        ),
        "a passage of an item that is not there": (standing_asked(app, NOWHERE_ITEM, held), 404),
    }
    for what, (response, status) in refused.items():
        assert response.status_code == status, (
            f"R6: {what} was not refused with {status}: {response.status_code} {response.text}"
        )
    assert standing_asked(app, mine.item, held).json() == found(held), (
        "R6a: a passage held by the item's document was not answered where it stands"
    )
    with as_owner(new_owner()):
        of_another = refusal(standing_asked(app, mine.item, held), mine.item)
        of_none = refusal(standing_asked(app, NOWHERE_ITEM, held), NOWHERE_ITEM)
    assert of_another[0] == 404 and of_another == of_none, (
        f"R6: asking the standing in another owner's item is told apart from an item that is "
        f"not there: {of_another} against {of_none}"
    )
