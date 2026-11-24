import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from infonium.library import LIBRARY_FILE, LOCAL_OWNER, LibraryStore
from tests.notebook import (
    LOST,
    NOWHERE_DOCUMENT,
    NOWHERE_ITEM,
    SCRIPTS,
    block_number_of,
    block_texts,
    document_of,
    found,
    highlights_of,
    instant,
    mark,
    marked_view,
    passage,
    phrase_passage,
    refusal,
    reread,
    searched_better,
    spans_in,
)
from tests.service import LEDGER, SITE, App, article, as_owner, keep_sources, new_owner

# The judgments of brief 02's requirements on highlights (R1 to R5, R10 to R13), through the
# service's endpoints.
EXAMPLES = settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
COMMENT_CHARACTERS = 10_000
NOWHERE_HIGHLIGHT = "nowhere"
# A made-up page read twice: its second form keeps every paragraph in the block of the same
# number but one, whose words are all gone.
PAGE = "ferry"
TITLE = "Quillmoor ledger"
REVISED_TITLE = "Quillmoor ledger, revised"
FERRY = LEDGER[:4]
GONE = FERRY[1]
REPLACEMENT = (
    "Nobody recorded anything about crossings during that particular decade, says a later "
    "chronicle of harbours."
)
FIRST_FORM = article(TITLE, FERRY)
SECOND_FORM = article(REVISED_TITLE, (FERRY[0], REPLACEMENT, *FERRY[2:]))
# The tables of a library file made before M3, by a service of commit 5231e56 started on an
# empty folder.
PRE_M3_TABLES = frozenset(
    {
        "digest",
        "document",
        "item",
        "item_turn",
        "library_turn",
        "search_block",
        "search_document",
        "search_words",
        "search_words_config",
        "search_words_content",
        "search_words_data",
        "search_words_docsize",
        "search_words_idx",
        "sqlite_sequence",
        "translated_block",
        "translation",
    }
)
KEPT_BEFORE_M3 = ("item", "document", "digest")
LONG_DOCUMENT_BLOCKS = 2_000
MANY_HIGHLIGHTS = 300


def kept_rows(connection: sqlite3.Connection) -> dict[str, list[Any]]:
    # What a library file holds of the tables made before M3: their schema, and the rows of
    # those that hold the items, their documents and digests.
    schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    held: dict[str, list[Any]] = {
        "schema": [entry for entry in schema if entry[2] in PRE_M3_TABLES]
    }
    for table in KEPT_BEFORE_M3:
        held[table] = sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall())
    return held


def made_once(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


@given(
    data=st.data(),
    blocks=st.lists(block_texts, min_size=1, max_size=25),
    comment=st.one_of(st.sampled_from(SCRIPTS), st.text(max_size=40)),
)
@EXAMPLES
def test_a_marked_passage_is_kept_and_given_back_as_it_was_marked(
    app: App, data: st.DataObject, blocks: list[str], comment: str
) -> None:
    owner = new_owner()
    [source] = keep_sources(app.store, owner, [("Marked", blocks)])
    marked = passage(source.document, blocks, data.draw(spans_in(blocks)))
    with as_owner(owner):
        before = datetime.now(UTC)
        view = marked_view(app, source.item, marked, comment)
        after = datetime.now(UTC)
        listed = highlights_of(app, source.item)
    assert (view["item"], view["passage"]) == (source.item, marked), (
        f"a highlight was not kept as the passage of the item it was marked in: {view}"
    )
    assert view["comment"] == comment, "a highlight's comment was not kept as it was given"
    assert view["revision"] == 1, "a highlight does not name the revision it was marked in"
    assert view["standing"] == found(marked), (
        "a highlight of the document its item holds now does not stand where it was marked"
    )
    assert before <= instant(view["made"]) <= after, "a highlight is not kept with when it was made"
    assert listed == [view], "the item's highlights do not give the highlight as it was answered"


def test_a_comment_at_its_bound_and_a_passage_of_twenty_blocks_are_kept_whole(app: App) -> None:
    blocks = [f"{SCRIPTS[number % len(SCRIPTS)]} {number}" for number in range(24)]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Long", blocks)])
    twenty = passage(
        source.document, blocks, [(number, 0, len(blocks[number])) for number in range(2, 22)]
    )
    comment = "\N{MATHEMATICAL FRAKTUR CAPITAL U}" * COMMENT_CHARACTERS
    view = marked_view(app, source.item, twenty, comment)
    assert (view["passage"], view["comment"]) == (twenty, comment), (
        "a passage of twenty blocks or a comment at its bound was not kept as it came"
    )
    assert highlights_of(app, source.item) == [view], "the item's highlights lost a part of one"


def test_two_highlights_that_overlap_are_both_kept(app: App) -> None:
    blocks = [SCRIPTS[1], SCRIPTS[0]]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Overlapping", blocks)])
    inside = passage(source.document, blocks, [(0, 2, 10)])
    across = passage(source.document, blocks, [(0, 5, len(blocks[0])), (1, 0, 8)])
    first = marked_view(app, source.item, inside)
    second = marked_view(app, source.item, across)
    assert first["id"] != second["id"], "a passage that overlaps a highlight was taken for it"
    assert [view["passage"] for view in highlights_of(app, source.item)] == [inside, across], (
        "two highlights that overlap were not both kept"
    )


def test_marking_the_same_anchors_again_answers_the_highlight_that_is_there(app: App) -> None:
    blocks = [SCRIPTS[0], SCRIPTS[2]]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Marked twice", blocks)])
    marked = passage(source.document, blocks, [(0, 4, 9), (1, 0, 6)])
    first = marked_view(app, source.item, marked, "first thought")
    again = marked_view(app, source.item, marked, "second thought")
    assert again == first, "marking a highlight's very anchors again did not answer it unchanged"
    assert highlights_of(app, source.item) == [first], "the same anchors made a second highlight"


@given(
    data=st.data(),
    blocks=st.lists(st.text(alphabet="ab", min_size=1, max_size=5), min_size=1, max_size=6),
    count=st.integers(1, 12),
)
@EXAMPLES
def test_an_items_highlights_follow_its_document_then_the_time_they_were_made(
    app: App, data: st.DataObject, blocks: list[str], count: int
) -> None:
    owner = new_owner()
    [source] = keep_sources(app.store, owner, [("Ordered", blocks)])
    drafts = [
        passage(source.document, blocks, data.draw(spans_in(blocks, most_blocks=3)))
        for _ in range(count)
    ]
    with as_owner(owner):
        ids = [marked_view(app, source.item, draft)["id"] for draft in drafts]
        listed = highlights_of(app, source.item)
    distinct = list(dict.fromkeys(json.dumps(draft, sort_keys=True) for draft in drafts))
    assert len(made_once(ids)) == len(distinct), (
        "the highlights made are not one for each set of anchors marked"
    )
    first_anchor = {
        identity: draft["anchors"][0]
        for identity, draft in reversed(list(zip(ids, drafts, strict=True)))
    }
    made_at = {identity: number for number, identity in enumerate(made_once(ids))}
    expected = sorted(
        made_once(ids),
        key=lambda identity: (
            first_anchor[identity]["block_number"],
            first_anchor[identity]["start"],
            made_at[identity],
        ),
    )
    assert [view["id"] for view in listed] == expected, (
        "an item's highlights are not in the order of its document, then of when they were made"
    )


def test_an_item_without_a_document_has_no_highlights_to_list_or_mark(app: App) -> None:
    unread = app.link(f"{SITE}/no-such-page").json()["id"]
    app.settled(unread)
    blocks = [SCRIPTS[0]]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Read", blocks)])
    marked = passage(source.document, blocks, [(0, 0, 3)])
    assert app.client.get(f"/api/items/{unread}/highlights").status_code == 404, (
        "an item without a document was given highlights"
    )
    assert mark(app, unread, marked).status_code == 404, "an item without a document was marked"
    assert app.client.get(f"/api/items/{NOWHERE_ITEM}/highlights").status_code == 404, (
        "an item that is not in the library was given highlights"
    )
    assert highlights_of(app, source.item) == [], "marking another item kept a highlight here"


def test_a_passage_of_a_document_that_is_no_revision_of_the_item_is_not_found(app: App) -> None:
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    marked, other = keep_sources(app.store, LOCAL_OWNER, [("Marked", blocks), ("Other", blocks)])
    of_other = passage(other.document, blocks, [(0, 0, 9)])
    made_up = passage(NOWHERE_DOCUMENT, blocks, [(0, 0, 9)])
    assert mark(app, marked.item, of_other).status_code == 404, (
        "a passage of another item's document was marked on this item"
    )
    assert mark(app, marked.item, made_up).status_code == 404, (
        "a passage of a document that is not in the library was marked"
    )
    assert (highlights_of(app, marked.item), highlights_of(app, other.item)) == ([], []), (
        "a refused passage was kept as a highlight"
    )


@given(
    data=st.data(),
    blocks=st.lists(block_texts, min_size=1, max_size=8),
    fault=st.sampled_from(["longer quote", "changed character", "block past the end"]),
)
@EXAMPLES
def test_a_passage_its_document_does_not_hold_as_given_is_refused_as_not_held(
    app: App, data: st.DataObject, blocks: list[str], fault: str
) -> None:
    owner = new_owner()
    [source] = keep_sources(app.store, owner, [("Held", blocks)])
    marked = passage(source.document, blocks, data.draw(spans_in(blocks)))
    anchors = marked["anchors"]
    broken = anchors[data.draw(st.integers(0, len(anchors) - 1))]
    match fault:
        case "longer quote":
            broken["quote"] += "\N{PILCROW SIGN}"
        case "changed character":
            other = "\N{SECTION SIGN}" if broken["quote"][-1] != "\N{SECTION SIGN}" else "#"
            broken["quote"] = broken["quote"][:-1] + other
        case _:
            anchors[-1]["block_number"] = len(blocks) + data.draw(st.integers(0, 3))
    with as_owner(owner):
        refused = mark(app, source.item, marked)
        listed = highlights_of(app, source.item)
    assert refused.status_code == 409, (
        f"a passage its document does not hold ({fault}) was not refused as not held: {refused}"
    )
    assert listed == [], "a passage refused as not held was kept as a highlight"


def valid_draft(document: str, blocks: list[str]) -> dict[str, Any]:
    return {"passage": passage(document, blocks, [(0, 0, 5)]), "comment": ""}


@pytest.mark.parametrize(
    "fault",
    ["comment past its bound", "no anchors", "anchors out of order", "two documents", "empty"],
)
def test_a_draft_the_models_refuse_is_refused_before_anything_is_kept(app: App, fault: str) -> None:
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    source, other = keep_sources(app.store, LOCAL_OWNER, [("Refused", blocks), ("Other", blocks)])
    draft = valid_draft(source.document, blocks)
    match fault:
        case "comment past its bound":
            draft["comment"] = "x" * (COMMENT_CHARACTERS + 1)
        case "no anchors":
            draft["passage"] = {"anchors": []}
        case "anchors out of order":
            draft["passage"] = passage(source.document, blocks, [(1, 0, 3), (0, 0, 3)])
        case "two documents":
            draft["passage"]["anchors"].append(
                passage(other.document, blocks, [(1, 0, 3)])["anchors"][0]
            )
        case _:
            draft["passage"] = passage(source.document, blocks, [(0, 3, 3)])
    refused = app.client.post(f"/api/items/{source.item}/highlights", json=draft)
    assert refused.status_code == 422, f"a draft with {fault} was not refused: {refused.text}"
    assert highlights_of(app, source.item) == [], f"a draft with {fault} kept a highlight"


def test_a_comment_takes_the_place_of_what_was_noted_and_no_text_takes_it_away(app: App) -> None:
    blocks = [SCRIPTS[0]]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Commented", blocks)])
    made = marked_view(app, source.item, passage(source.document, blocks, [(0, 4, 9)]), "first")
    path = f"/api/items/{source.item}/highlights/{made['id']}/comment"
    second = app.client.put(path, json={"text": SCRIPTS[5]})
    assert second.status_code == 200 and second.json() == {**made, "comment": SCRIPTS[5]}, (
        f"a comment did not take the place of what was noted, and only that: {second.text}"
    )
    too_long = app.client.put(path, json={"text": "x" * (COMMENT_CHARACTERS + 1)})
    assert too_long.status_code == 422, "a comment past its bound was taken"
    cleared = app.client.put(path, json={"text": ""})
    assert cleared.status_code == 200 and cleared.json() == {**made, "comment": ""}, (
        f"no text did not take the comment away: {cleared.text}"
    )
    assert highlights_of(app, source.item) == [cleared.json()], (
        "the item's highlights do not give the comment as it was put last"
    )


def test_a_removed_highlight_is_gone_from_its_item(app: App) -> None:
    blocks = [SCRIPTS[0]]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Removed", blocks)])
    kept = marked_view(app, source.item, passage(source.document, blocks, [(0, 0, 3)]))
    gone = marked_view(app, source.item, passage(source.document, blocks, [(0, 4, 9)]))
    path = f"/api/items/{source.item}/highlights/{gone['id']}"
    assert app.client.delete(path).status_code == 204, "a highlight was not removed"
    assert highlights_of(app, source.item) == [kept], "a removed highlight is still listed"
    assert app.client.put(f"{path}/comment", json={"text": "late"}).status_code == 404, (
        "a removed highlight was found to comment on"
    )
    assert app.client.delete(path).status_code == 404, "a removed highlight was found again"


def test_a_highlight_is_not_found_under_another_item_or_an_id_that_names_none(app: App) -> None:
    blocks = [SCRIPTS[0]]
    marked, other = keep_sources(app.store, LOCAL_OWNER, [("Marked", blocks), ("Other", blocks)])
    made = marked_view(app, marked.item, passage(marked.document, blocks, [(0, 0, 3)]), "kept")
    for item, highlight in [(other.item, made["id"]), (marked.item, NOWHERE_HIGHLIGHT)]:
        path = f"/api/items/{item}/highlights/{highlight}"
        assert app.client.put(f"{path}/comment", json={"text": "x"}).status_code == 404, (
            "a highlight was found to comment on under an item it is not of, or an id of none"
        )
        assert app.client.delete(path).status_code == 404, (
            "a highlight was found to remove under an item it is not of, or an id of none"
        )
    assert highlights_of(app, marked.item) == [made], "a refused change changed the highlight"


def test_every_items_highlights_are_listed_the_item_marked_last_first(app: App) -> None:
    owner = new_owner()
    blocks = [f"{SCRIPTS[0]} {number}" for number in range(6)]
    sources = keep_sources(
        app.store, owner, [(title, blocks) for title in ("First", "Second", "Third", "None")]
    )
    marking = [0, 1, 0, 2, 1, 0, 2]
    marked_so_far = [0] * len(sources)
    with as_owner(owner):
        for index in marking:
            source = sources[index]
            number = marked_so_far[index]
            marked_so_far[index] += 1
            marked_view(app, source.item, passage(source.document, blocks, [(number, 0, 3)]))
        response = app.client.get("/api/highlights")
        own_lists = {source.item: highlights_of(app, source.item) for source in sources[:3]}
    assert response.status_code == 200, f"the library's highlights were not listed: {response}"
    listed = response.json()
    last_marked = [2, 0, 1]
    assert [entry["item"] for entry in listed] == [sources[i].item for i in last_marked], (
        "the library's highlights are not by item, the item marked last first, without an "
        "item that has none"
    )
    for entry, index in zip(listed, last_marked, strict=True):
        assert entry["title"] == sources[index].title, "an item is not listed by what it is called"
        assert entry["highlights"] == own_lists[entry["item"]], (
            "an item's highlights in the library's list are not those of its own list"
        )
        assert len(entry["highlights"]) == marking.count(index), "a highlight is not listed"


def test_an_item_whose_highlights_are_all_removed_is_not_listed(app: App) -> None:
    owner = new_owner()
    blocks = [SCRIPTS[0]]
    emptied, kept = keep_sources(app.store, owner, [("Emptied", blocks), ("Kept", blocks)])
    with as_owner(owner):
        made = [
            marked_view(app, emptied.item, passage(emptied.document, blocks, [(0, 0, 3)])),
            marked_view(app, emptied.item, passage(emptied.document, blocks, [(0, 4, 9)])),
        ]
        marked_view(app, kept.item, passage(kept.document, blocks, [(0, 0, 3)]))
        for view in made:
            removed = app.client.delete(f"/api/items/{emptied.item}/highlights/{view['id']}")
            assert removed.status_code == 204, "a highlight was not removed"
        listed = app.client.get("/api/highlights").json()
    assert [entry["item"] for entry in listed] == [kept.item], (
        "an item whose highlights are all removed is still listed, or another is not"
    )


def test_after_a_second_reading_a_highlight_stands_where_its_words_are_or_is_lost(
    app: App,
) -> None:
    item = app.read_page(PAGE, FIRST_FORM)
    first = document_of(app, item)
    passages = {
        "across": phrase_passage(
            app, item, [(FERRY[2], "eastern breakwater"), (FERRY[3], "The town council")]
        ),
        "later": phrase_passage(app, item, [(FERRY[2], "forty-one nights")]),
        "earlier": phrase_passage(app, item, [(FERRY[0], "night ferry")]),
        "brenn": phrase_passage(app, item, [(GONE, "Brenn")]),
        "odalys": phrase_passage(app, item, [(GONE, "Odalys")]),
    }
    made = {name: marked_view(app, item, marked) for name, marked in passages.items()}
    numbers = [block_number_of(app, item, text) for text in FERRY]
    reread(app, item, PAGE, SECOND_FORM)
    now = document_of(app, item)
    numbers_now = [block_number_of(app, item, text) for text in (FERRY[0], FERRY[2], FERRY[3])]
    assert numbers_now == [numbers[0], *numbers[2:]], "the page's blocks moved: a test's fault"
    listed = highlights_of(app, item)
    order = ["earlier", "later", "across", "brenn", "odalys"]
    assert [view["id"] for view in listed] == [made[name]["id"] for name in order], (
        "after a second reading an item's highlights are not first those that stand in its "
        "document, in its order, then those it lost, in the order they were made"
    )
    for name, view in zip(order, listed, strict=True):
        assert (view["passage"], view["revision"]) == (passages[name], 1), (
            "a highlight marked before a second reading is not given as it was marked, in "
            "revision 1"
        )
    for name, view in zip(order[:3], listed[:3], strict=True):
        moved = {
            "anchors": [
                {**anchor, "document_id": now["id"]} for anchor in passages[name]["anchors"]
            ]
        }
        assert view["standing"] == found(moved), (
            "a passage whose words the new document holds unchanged in the blocks of the same "
            f"numbers is not found there: {view['standing']}"
        )
    for view in listed[3:]:
        assert view["standing"] == LOST, (
            f"a passage whose words are all gone from the new document is not lost: {view}"
        )
    assert first["id"] != now["id"], "the second reading gave the same document"


def test_a_passage_of_either_revision_is_marked_under_its_number(app: App) -> None:
    item = app.read_page(PAGE, FIRST_FORM)
    of_first = phrase_passage(app, item, [(FERRY[3], "half past eight")])
    reread(app, item, PAGE, SECOND_FORM)
    of_second = phrase_passage(app, item, [(REPLACEMENT, "later chronicle")])
    earlier = marked_view(app, item, of_first)
    later = marked_view(app, item, of_second)
    assert (earlier["revision"], later["revision"]) == (1, 2), (
        "a passage is not marked under the number of the revision it lies in"
    )
    assert later["standing"] == found(of_second), (
        "a passage of the document the item holds now does not stand where it was marked"
    )
    assert earlier["standing"]["kind"] == "found", (
        "a passage of an earlier revision whose words the item's document holds is not found"
    )
    response = app.client.get("/api/highlights")
    [entry] = response.json()
    assert entry["title"] == REVISED_TITLE, (
        "the library's highlights do not name an item by what it is called now"
    )


def test_a_highlight_lost_is_found_again_when_its_words_come_back(app: App) -> None:
    item = app.read_page(PAGE, FIRST_FORM)
    gone = phrase_passage(app, item, [(GONE, "Odalys")])
    made = marked_view(app, item, gone)
    reread(app, item, PAGE, SECOND_FORM)
    [lost] = highlights_of(app, item)
    reread(app, item, PAGE, FIRST_FORM)
    [back] = highlights_of(app, item)
    assert lost["standing"] == LOST, "a passage whose words are all gone was not lost"
    assert back["standing"] == found(gone), (
        "a highlight is not found where it was marked once the item holds that document again"
    )
    assert back["passage"] == made["passage"], "a highlight changed by being read again"


def test_a_standing_is_worked_out_when_read_so_a_better_search_finds_more(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = app.read_page(PAGE, FIRST_FORM)
    made = marked_view(app, item, phrase_passage(app, item, [(GONE, "Odalys")]))
    reread(app, item, PAGE, SECOND_FORM)
    now = document_of(app, item)
    [lost] = highlights_of(app, item)
    with searched_better(monkeypatch):
        [better] = highlights_of(app, item)
    [again] = highlights_of(app, item)
    assert lost["standing"] == LOST, "a passage whose words are all gone was not lost"
    assert better["standing"]["kind"] == "found" and not better["standing"]["exact"], (
        "a highlight's standing was not worked out when it was read: a better search did not "
        f"find what the first lost: {better['standing']}"
    )
    assert better["standing"]["passage"]["anchors"][0]["document_id"] == now["id"], (
        "a highlight's standing is not in the document the item holds now"
    )
    assert again == lost and better["passage"] == made["passage"], (
        "what a search worked out was kept with the highlight"
    )


def test_another_owners_highlights_are_never_answered_or_told_apart_from_none(app: App) -> None:
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    [mine] = keep_sources(app.store, LOCAL_OWNER, [("Mine", blocks)])
    my_passage = passage(mine.document, blocks, [(0, 0, 3)])
    my_view = marked_view(app, mine.item, my_passage, "mine")
    other = new_owner()
    [theirs] = keep_sources(app.store, other, [("Theirs", blocks)])
    with as_owner(other):
        their_view = marked_view(app, theirs.item, passage(theirs.document, blocks, [(1, 0, 3)]))
        nowhere_passage = passage(NOWHERE_DOCUMENT, blocks, [(0, 0, 3)])
        compared = {
            "listing an item's highlights": (
                refusal(app.client.get(f"/api/items/{mine.item}/highlights"), mine.item),
                refusal(app.client.get(f"/api/items/{NOWHERE_ITEM}/highlights"), NOWHERE_ITEM),
            ),
            "marking an item": (
                refusal(mark(app, mine.item, my_passage), mine.item),
                refusal(mark(app, NOWHERE_ITEM, my_passage), NOWHERE_ITEM),
            ),
            "marking a passage of a document": (
                refusal(mark(app, theirs.item, my_passage), mine.document),
                refusal(mark(app, theirs.item, nowhere_passage), NOWHERE_DOCUMENT),
            ),
            "commenting on a highlight": (
                refusal(
                    app.client.put(
                        f"/api/items/{theirs.item}/highlights/{my_view['id']}/comment",
                        json={"text": "theirs"},
                    ),
                    my_view["id"],
                ),
                refusal(
                    app.client.put(
                        f"/api/items/{theirs.item}/highlights/{NOWHERE_HIGHLIGHT}/comment",
                        json={"text": "theirs"},
                    ),
                    NOWHERE_HIGHLIGHT,
                ),
            ),
            "removing a highlight": (
                refusal(
                    app.client.delete(f"/api/items/{theirs.item}/highlights/{my_view['id']}"),
                    my_view["id"],
                ),
                refusal(
                    app.client.delete(f"/api/items/{theirs.item}/highlights/{NOWHERE_HIGHLIGHT}"),
                    NOWHERE_HIGHLIGHT,
                ),
            ),
        }
        their_library = app.client.get("/api/highlights").json()
    for operation, (of_another, of_none) in compared.items():
        assert of_another[0] == 404, f"{operation} of another owner's was not refused as not found"
        assert of_another == of_none, (
            f"{operation} of another owner's is told apart from one that is not there: "
            f"{of_another} against {of_none}"
        )
    assert [entry["item"] for entry in their_library] == [theirs.item], (
        "an owner's list of highlights holds another owner's, or not their own"
    )
    assert their_library[0]["highlights"] == [their_view], "an owner's own highlight is not listed"
    assert highlights_of(app, mine.item) == [my_view], "another owner changed a highlight"
    assert [entry["item"] for entry in app.client.get("/api/highlights").json()] == [mine.item], (
        "an owner's list of highlights holds another owner's"
    )


def test_highlights_are_kept_over_a_restart(start_app: Callable[..., App]) -> None:
    app = start_app()
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    first, second = keep_sources(app.store, LOCAL_OWNER, [("One", blocks), ("Two", blocks)])
    marked_view(app, first.item, passage(first.document, blocks, [(0, 0, 3), (1, 0, 2)]), "kept")
    removed = marked_view(app, first.item, passage(first.document, blocks, [(0, 4, 9)]))
    marked_view(app, second.item, passage(second.document, blocks, [(1, 2, 5)]), SCRIPTS[5])
    deleted = app.client.delete(f"/api/items/{first.item}/highlights/{removed['id']}")
    assert deleted.status_code == 204, "a highlight was not removed"
    before = app.client.get("/api/highlights").json()
    app.stop()
    again = start_app()
    after = again.client.get("/api/highlights").json()
    assert sum(len(entry["highlights"]) for entry in before) == 2, "a highlight was not made"
    assert after == before, "the highlights are not as they were after the service started again"


def test_a_library_made_before_highlights_is_served_and_left_as_it_was(
    start_app: Callable[..., App], tmp_path: Path
) -> None:
    store = LibraryStore(tmp_path / "data")
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    sources = keep_sources(store, LOCAL_OWNER, [("Older", blocks), ("Oldest", blocks)])
    store.lock.release()
    store.engine.dispose()
    with closing(sqlite3.connect(tmp_path / "data" / LIBRARY_FILE)) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        newer = [name for (name,) in tables if name not in PRE_M3_TABLES]
        assert newer, "a library of today holds no table that one made before M3 lacks"
        for name in newer:
            connection.execute(f'DROP TABLE "{name}"')
        connection.commit()
        held = kept_rows(connection)
    app = start_app()
    assert highlights_of(app, sources[0].item) == [], "an older library was given highlights"
    assert app.client.get("/api/highlights").json() == [], "an older library was given highlights"
    assert app.client.get("/api/notes").json() == [], "an older library was given notes"
    marked_view(app, sources[0].item, passage(sources[0].document, blocks, [(0, 0, 3)]))
    note = app.client.post(
        "/api/notes",
        json={
            "title": "On an older library",
            "markdown": "[it](passage:a)",
            "passages": [{"id": "a", "passage": passage(sources[1].document, blocks, [(1, 0, 3)])}],
        },
    )
    assert note.status_code == 200, f"a note was not kept in an older library: {note.text}"
    app.stop()
    with closing(sqlite3.connect(tmp_path / "data" / LIBRARY_FILE)) as connection:
        assert kept_rows(connection) == held, (
            "the service changed what a library made before M3 held"
        )


def test_three_hundred_highlights_on_two_thousand_blocks_are_all_listed_in_order(
    app: App,
) -> None:
    blocks = [
        f"Block {number} of a long made-up document." for number in range(LONG_DOCUMENT_BLOCKS)
    ]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Long", blocks)])
    numbers = [(number * 677) % LONG_DOCUMENT_BLOCKS for number in range(MANY_HIGHLIGHTS)]
    for number in numbers:
        marked_view(app, source.item, passage(source.document, blocks, [(number, 0, 5)]))
    listed = highlights_of(app, source.item)
    assert [view["passage"]["anchors"][0]["block_number"] for view in listed] == sorted(numbers), (
        "the highlights of an item with many are not all listed in the order of its document"
    )
    assert all(view["standing"] == found(view["passage"]) for view in listed), (
        "a highlight of the document its item holds now does not stand where it was marked"
    )
