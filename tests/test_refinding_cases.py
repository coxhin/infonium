import pytest

from infonium.anchor import Anchor
from infonium.document import HTML_MEDIA_TYPE, Document, Paragraph, document_of
from infonium.passage import Found, Lost, Passage, Standing
from infonium.refinding import standing_in
from tests.revising import (
    FIRST_EXTRACTOR,
    SECOND_EXTRACTOR,
    Change,
    Cut,
    Revised,
    Unit,
    UnitKind,
    WordSpan,
    document_letters,
    judge_standing,
    laid_out,
    marked_passage,
    spaced,
)

# Cases that the properties of test_standing drew, or that a review of their mending built,
# kept as they are, since a draw finds them again only by chance. Each is judged by the
# properties' own oracle, and its answer is named besides: a row that stands at its own place
# is found there, a passage whose place is another copy's is lost, and a sentence whose
# neighbour gained a digit after its point stays found.
HARBOUR = "The harbour keeper wrote the winter ledger by night in the north-east town of Zürich."
FERRY = "A ferry crossing in fog was late and the naïve keeper's café stood by the flood."
LATE_FOG = "Late fog over the town and the ferry, the ledger of fifty winters, the keeper's night."
INTRO = "The harbour keeper kept the winter ledger of the north-east town by night."
REST = "The next year the ferry crossing was late in fog and the town kept no ledger."
ENDED = "The ferry service across the harbour ended in 1945."


def plain_row(*words: str) -> Unit:
    return spaced(UnitKind.PLAIN_ROW, list(words))


def row(*words: str) -> Unit:
    return spaced(UnitKind.ROW, list(words))


def sentence(text: str) -> Unit:
    return spaced(UnitKind.SENTENCE, text.split(" "))


def cut(before_unit: bool) -> Cut:
    return Cut(before_unit=before_unit, before_word=None)


def table(rows: list[Unit]) -> list[Cut]:
    # The cuts of one table's rows: a new block before its first row only.
    return [cut(True)] + [cut(False)] * (len(rows) - 1)


def revised(
    first: list[Unit],
    second: list[Unit],
    origins: list[int | None],
    first_cuts: list[Cut],
    second_cuts: list[Cut],
) -> Revised:
    # Two revisions whose units the second keeps as they were, where origins names them, and
    # whose other units of the first are removed.
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


def whole(units: list[Unit], number: int) -> WordSpan:
    return WordSpan(
        first_unit=number, first_word=0, last_unit=number, last_word=len(units[number].words) - 1
    )


def judged(revision: Revised, span: WordSpan) -> Standing:
    # Where the passage of the span stands in the second revision, once the oracle of the
    # properties has accepted it.
    passage = marked_passage(revision, span)
    standing = standing_in(passage, revision.first.document, revision.second.document)
    judge_standing(
        revision,
        span,
        standing,
        document_letters(revision.first.document),
        document_letters(revision.second.document),
    )
    return standing


def found_at(standing: Standing) -> list[tuple[int, int, int]]:
    assert isinstance(standing, Found) and standing.exact, f"not found exact: {standing}"
    return [(anchor.block_number, anchor.start, anchor.end) for anchor in standing.passage.anchors]


def test_a_row_is_found_at_its_place_though_its_letters_run_on_from_a_number_s_point() -> None:
    # The letters 122 of the row 1 2 2 also stand after the point of 88.1 and in the next
    # table's 2 2 1, which are no words of the row.
    first = [
        sentence(HARBOUR),
        plain_row("1", "2", "2"),
        plain_row("3", "1", "3"),
        plain_row("2", "3", "1"),
        plain_row("3", "3", "2"),
        plain_row("1", "1", "3"),
        sentence(LATE_FOG),
    ]
    second = [
        sentence(FERRY),
        *first[1:],
        row("r907", "0.004", "61.75", "88.1"),
        plain_row("2", "2", "3"),
        plain_row("3", "2", "3"),
    ]
    first_cuts = [cut(True), *table(first[1:6]), cut(True)]
    second_cuts = [*first_cuts, cut(True), *table(second[8:])]
    revision = revised(
        first, second, [None, 1, 2, 3, 4, 5, 6, None, None, None], first_cuts, second_cuts
    )
    assert found_at(judged(revision, whole(first, 1))) == [(1, 0, 5)]


def test_a_row_is_found_at_its_place_though_another_row_was_renumbered_into_its_words() -> None:
    # The row 2 2 1 stands where it stood, among the rows that followed it; another row became
    # 2 2 1 elsewhere, among rows unlike the row's. The rows before the row were put in again.
    r11 = row("r11", "4.5", "4.5", "4.5")
    first = [
        r11, r11, r11, r11,
        plain_row("1", "2", "1"),
        row("r12", "4.5", "7", "4.5"),
        r11, r11,
        row("r13", "4.5", "4.5", "4.5"),
        r11, r11, r11, r11,
        plain_row("2", "2", "1"),
        r11, r11,
    ]  # fmt: skip
    second = [
        row("r907", "88.1", "88.1", "88.1"), r11,
        row("r914", "88.1", "88.1", "88.1"), r11, r11,
        row("r921", "88.1", "88.1", "88.1"), r11,
        row("r928", "88.1", "88.1", "88.1"),
        plain_row("2", "2", "1"),
        row("r12", "4.5", "7", "4.5"),
        r11, r11, r11, r11, r11,
        row("r935", "88.1", "88.1", "88.1"), r11,
        plain_row("2", "2", "1"),
        r11, r11,
    ]  # fmt: skip
    origins: list[int | None] = [
        None, 0, None, 1, 2, None, 3, None, 4, 5, 6, 7, None, 9, 10, None, 12, 13, 14, 15
    ]  # fmt: skip
    revision = Revised(
        first_units=first,
        second_units=second,
        origins=origins,
        changes=[
            Change.KEPT,
            Change.KEPT,
            Change.KEPT,
            Change.KEPT,
            Change.RENUMBERED,
            Change.KEPT,
            Change.KEPT,
            Change.KEPT,
            Change.REMOVED,
            Change.KEPT,
            Change.KEPT,
            Change.REWRITTEN,
            Change.KEPT,
            Change.KEPT,
            Change.KEPT,
            Change.KEPT,
        ],
        first=laid_out(first, [cut(True)] * 14 + [cut(False)] * 2, FIRST_EXTRACTOR),
        second=laid_out(second, [cut(True)] * 18 + [cut(False)] * 2, SECOND_EXTRACTOR),
    )
    assert found_at(judged(revision, whole(first, 13))) == [(17, 0, 5)]


def test_a_word_said_again_right_after_its_own_copy_is_not_taken_for_it() -> None:
    # Figure 3. said three times where it was said once: the copy put in after its own keeps
    # the document's end beside it, and is not the passage's.
    figure = spaced(UnitKind.BOILERPLATE, ["Figure", "3."])
    figure_cut = Cut(before_unit=True, before_word=1)
    revision = revised(
        [figure], [figure, figure, figure], [None, 0, None], [figure_cut], [figure_cut] * 3
    )
    standing = judged(revision, WordSpan(first_unit=0, first_word=0, last_unit=0, last_word=0))
    assert isinstance(standing, Lost), f"Figure was taken at a copy put in: {standing}"


def test_a_row_whose_own_row_was_renumbered_is_not_found_at_a_copy_elsewhere() -> None:
    # The row 2 2 1 became 3 2 1; a row of another table, among the same rows, became 2 2 1;
    # and a third table says 2 2 1 too. The passage's place is gone.
    harbour = sentence("The harbour keeper wrote the winter ledger by night, the keeper's night.")
    ferry = sentence(
        "A ferry crossing in fog was late and the café by the flood, the keeper's night."
    )
    late = sentence(
        "Late fog over the town and the ferry, the ledger of fifty winters, the town's flood."
    )
    zurich = sentence("Zürich and the naïve ferry of the north-east, a late harbour in winter fog.")
    before = [
        plain_row("3", "3", "3"),
        plain_row("1", "3", "1"),
        plain_row("3", "1", "3"),
        plain_row("1", "1", "3"),
    ]
    after = [
        plain_row("3", "1", "3"),
        plain_row("3", "3", "1"),
        plain_row("3", "3", "3"),
        plain_row("1", "1", "3"),
    ]
    third = [plain_row("1", "1", "1"), plain_row("2", "2", "1"), plain_row("1", "1", "1")]
    first = [
        harbour,
        *before,
        plain_row("2", "2", "1"),
        *after,
        late,
        ferry,
        *before,
        plain_row("1", "2", "1"),
        *after,
        late,
        zurich,
    ]
    second = [
        harbour,
        *before,
        plain_row("3", "2", "1"),
        *after,
        late,
        ferry,
        *before,
        plain_row("2", "2", "1"),
        *after,
        late,
        zurich,
        *third,
    ]
    first_cuts = [
        cut(True),
        *table([*before, first[5], *after]),
        cut(True),
        cut(True),
        *table([*before, first[16], *after]),
        cut(True),
        cut(True),
    ]
    origins: list[int | None] = [*range(len(first)), None, None, None]
    revision = revised(first, second, origins, first_cuts, [*first_cuts, *table(third)])
    standing = judged(revision, whole(first, 5))
    assert isinstance(standing, Lost), f"a row whose place is gone was found elsewhere: {standing}"


def test_a_sentence_put_in_again_beside_its_own_copy_is_not_taken_for_it() -> None:
    # Continued on the next page. said again at the end of a reference, and its own copy kept
    # in a block of its own before a rewritten sentence.
    sums = sentence("Table 2 sums the rows above it.")
    okafor = spaced(
        UnitKind.REFERENCE,
        [
            "Okafor,",
            "G.",
            "(1955).",
            "On",
            "the",
            "night",
            "ferry",
            "of",
            "the",
            "harbour",
            "Journal",
            "of",
            "Tides",
            "16(4):",
            "65-71.",
        ],
    )
    continued = spaced(UnitKind.BOILERPLATE, ["Continued", "on", "the", "next", "page."])
    first = [
        sums,
        okafor,
        continued,
        sentence("Zürich and the naïve ferry of the north-east, a late harbour in winter fog."),
    ]
    second = [
        sums,
        okafor,
        continued,
        continued,
        sentence("The keeper's café by the flood was late, and the ledger of fifty winters."),
    ]
    revision = revised(
        first,
        second,
        [0, 1, None, 2, None],
        [cut(True)] * 4,
        [cut(True), cut(True), cut(False), cut(True), cut(False)],
    )
    standing = judged(revision, whole(first, 2))
    assert not (isinstance(standing, Found) and standing.passage.anchors[0].block_number == 1), (
        f"the sentence was taken at the copy put in: {standing}"
    )


# A table of the passage's row and the six rows after it, and a table that says them again
# after rows of its own, as the third review of the mend built them.
LEDGER_ROWS = [
    row("r11", "4.5", "7", "12"),
    row("r12", "0.3", "3.25", "7"),
    row("r13", "12", "12", "4.5"),
    row("r14", "3.25", "0.3", "0.3"),
]
FRESH_ROWS = [
    row("r901", "905", "61.75", "88.1"),
    row("r902", "0.004", "905", "905"),
    row("r903", "61.75", "88.1", "0.004"),
    row("r904", "88.1", "905", "61.75"),
]
OWN_ROW = row("r15", "4.5", "0.3", "7")
ROWS_AFTER = [
    row("r16", "7", "12", "3.25"),
    row("r17", "0.3", "4.5", "12"),
    row("r18", "3.25", "3.25", "0.3"),
    row("r19", "12", "7", "7"),
    row("r20", "4.5", "12", "0.3"),
    row("r21", "7", "0.3", "4.5"),
]
OTHER_TABLE_ROWS = [
    row("r31", "905", "88.1", "0.004"),
    row("r32", "61.75", "905", "88.1"),
    row("r33", "0.004", "0.004", "905"),
    LEDGER_ROWS[3],
]
NIGHT = "The harbour keeper wrote the winter ledger by night, the keeper's night."
CAFE = "A ferry crossing in fog was late and the café by the flood, the keeper's night."


def renumbered(unit: Unit) -> Unit:
    # The row with the first digit of its label one more: r16 becomes r26.
    label = unit.words[0]
    return row(f"r{int(label[1]) + 1}{label[2:]}", *unit.words[1:])


def rows_said_again(own_row: Unit, third_table: list[Unit]) -> tuple[Revised, WordSpan]:
    # The passage's row stands where it stood as own_row says, each row after it renumbered,
    # the rows before it rewritten; another table says the row and the rows that followed it
    # as they were, and a third table, where given, says the row once more.
    first = [sentence(NIGHT), *LEDGER_ROWS, OWN_ROW, *ROWS_AFTER, sentence(CAFE)]
    first_cuts = [cut(True), *table([*LEDGER_ROWS, OWN_ROW, *ROWS_AFTER]), cut(True)]
    after = [renumbered(unit) for unit in ROWS_AFTER]
    second = [
        sentence(NIGHT),
        *FRESH_ROWS,
        own_row,
        *after,
        sentence(CAFE),
        *OTHER_TABLE_ROWS,
        OWN_ROW,
        *ROWS_AFTER,
        *third_table,
    ]
    second_cuts = [
        cut(True),
        *table([*FRESH_ROWS, own_row, *after]),
        cut(True),
        *table([*OTHER_TABLE_ROWS, OWN_ROW, *ROWS_AFTER]),
        *(table(third_table) if third_table else []),
    ]
    origins: list[int | None] = [
        0, None, None, None, None, 5, 6, 7, 8, 9, 10, 11, 12,
        *[None] * (len(OTHER_TABLE_ROWS) + 1 + len(ROWS_AFTER) + len(third_table)),
    ]  # fmt: skip
    return revised(first, second, origins, first_cuts, second_cuts), whole(first, 5)


def test_a_row_kept_among_renumbered_rows_is_not_taken_at_a_table_that_repeats_them() -> None:
    # Each row after the own row changed by one digit: its side still stands, with a change in
    # every quarter of it, and the other table keeps the side as it was.
    revision, span = rows_said_again(OWN_ROW, [])
    standing = judged(revision, span)
    assert not (isinstance(standing, Found) and standing.passage.anchors[0].block_number == 3), (
        f"the row was taken in the table that repeats its rows: {standing}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known limit: where the passage's own row is renumbered too, and its rows' side is "
        "found again only through a quarter of it kept as it is, the table that repeats the "
        "rows is taken for the row's place"
    ),
)
def test_a_renumbered_row_is_not_found_at_a_table_that_repeats_its_rows() -> None:
    third = [row("r51", "61.75", "61.75", "905"), OWN_ROW, row("r52", "0.004", "88.1", "88.1")]
    revision, span = rows_said_again(row("r15", "5.5", "0.3", "7"), third)
    judged(revision, span)


def paragraphs(extractor: str, *texts: str) -> Document:
    return document_of(
        item_id="d" * 64,
        extractor=extractor,
        snapshot="0" * 64,
        media_type=HTML_MEDIA_TYPE,
        blocks=[Paragraph(text=text, marks=[]) for text in texts],
    )


def passage_of(document: Document, quote: str) -> Passage:
    start = document.blocks[1].text.index(quote)
    return Passage(
        anchors=[
            Anchor(
                document_id=document.id,
                block_number=1,
                start=start,
                end=start + len(quote),
                quote=quote,
            )
        ]
    )


def test_a_sentence_ending_in_a_year_stays_found_when_a_footnote_s_digit_follows_it() -> None:
    # The web reader joins a footnote's mark to the sentence it follows: 1945.3, 1945.³.
    first = paragraphs("first", INTRO, f"{ENDED} {REST}")
    for mark in ("3", "\N{SUPERSCRIPT THREE}"):
        second = paragraphs("second", INTRO, f"{ENDED}{mark} {REST}")
        standing = standing_in(passage_of(first, ENDED), first, second)
        assert found_at(standing) == [(1, 0, len(ENDED))], f"lost after {ENDED}{mark}"


def test_a_passage_after_numbers_that_lost_their_space_stays_found() -> None:
    marked = "2 and the ledger of the north-east harbour town for the winter."
    first = paragraphs("first", INTRO, f"See tables 1, {marked}")
    second = paragraphs(
        "second",
        INTRO,
        "See tables 1,2 and the ledger of the north-east harbour town for the winter.",
    )
    standing = standing_in(passage_of(first, marked), first, second)
    assert isinstance(standing, Found) and standing.exact, f"lost after 1,2: {standing}"
