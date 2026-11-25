import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import Any

import httpx2
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from infonium.library import LOCAL_OWNER
from tests.notebook import (
    LOST,
    NOWHERE_DOCUMENT,
    SCRIPTS,
    block_texts,
    document_of,
    found,
    instant,
    passage,
    phrase_passage,
    refusal,
    reread,
    searched_better,
    spans_in,
)
from tests.service import LEDGER, App, Source, article, as_owner, keep_sources, new_owner

# The judgments of brief 02's requirements on notes (R6 to R13), through the service's
# endpoints.
EXAMPLES = settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
FEW_EXAMPLES = settings(max_examples=8, suppress_health_check=[HealthCheck.function_scoped_fixture])
TITLE_CHARACTERS = 200
MARKDOWN_CHARACTERS = 500_000
PASSAGE_ID_CHARACTERS = 32
PASSAGE_ID = r"[0-9a-z]{1,32}"
NOWHERE_NOTE = "nowhere"
# Long enough for the clock to move between two writes, so that "changed later" can be seen.
CLOCK_STEP_SECONDS = 0.005
# Markdown that a reader of Markdown would mend or render: line ends of two kinds, a link to
# a passage the note does not have, markup left open, HTML, and spaces that mean a line break.
MARKDOWNS = (
    "# A heading\r\n\r\nSee [the ferry](passage:a) and [nothing](passage:nothere).\r\n",
    "**open *emphasis and [a link](passage:  \n\n\n\t- a list item  \n",
    "<script>alert(1)</script><p>html</p> &amp; &lt; \\* not emphasis \\",
    "",
    *SCRIPTS,
)
markdown_texts = st.one_of(st.sampled_from(MARKDOWNS), st.text(max_size=200))
title_texts = st.one_of(st.sampled_from(SCRIPTS), st.text(max_size=TITLE_CHARACTERS))
PAGE = "harbour"
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
MANY_PASSAGES = 50
MANY_ITEMS = 20
# The greatest edition the judgments send: a draft names a whole number, and a writer counts
# its editions up from the one it read.
EDITION_BOUND = 2**31


def note_draft(
    title: str, markdown: str, passages: list[tuple[str, dict[str, Any]]], edition: int = 1
) -> dict[str, Any]:
    return {
        "edition": edition,
        "title": title,
        "markdown": markdown,
        "passages": [{"id": identity, "passage": linked} for identity, linked in passages],
    }


def written(app: App, draft: dict[str, Any]) -> dict[str, Any]:
    # A note that a test needs kept; the test fails here when it is not.
    response = app.client.post("/api/notes", json=draft)
    if response.status_code != 200:
        pytest.fail(f"a note a test needs was refused: {response.text}")
    view: dict[str, Any] = response.json()
    return view


def rewritten(app: App, note: str, draft: dict[str, Any]) -> dict[str, Any]:
    response = app.client.put(f"/api/notes/{note}", json=draft)
    if response.status_code != 200:
        pytest.fail(f"a note a test needs rewritten was refused: {response.text}")
    view: dict[str, Any] = response.json()
    return view


def note_of(app: App, note: str) -> dict[str, Any]:
    response = app.client.get(f"/api/notes/{note}")
    assert response.status_code == 200, f"a note that was kept was not found: {response.text}"
    view: dict[str, Any] = response.json()
    return view


def notes(app: App) -> list[dict[str, Any]]:
    response = app.client.get("/api/notes")
    assert response.status_code == 200, f"the notes of the library were not listed: {response}"
    listed: list[dict[str, Any]] = response.json()
    return listed


def items_once(draft: dict[str, Any], item_of: dict[str, str]) -> list[str]:
    # The items a draft's passages lie in, each once, in the order of the passages.
    return list(
        dict.fromkeys(
            item_of[linked["passage"]["anchors"][0]["document_id"]] for linked in draft["passages"]
        )
    )


def assert_shows(
    view: dict[str, Any], draft: dict[str, Any], sources: list[Source], revision: int = 1
) -> None:
    # A note's view gives what its draft held, each passage with its item, what the item is
    # called, its revision and where it stands in the document its item holds now.
    by_document = {source.document: source for source in sources}
    assert view["edition"] == draft["edition"], (
        "a note's view does not give the edition of the draft it holds"
    )
    assert (view["title"], view["markdown"]) == (draft["title"], draft["markdown"]), (
        "a note's title or Markdown was not given back character for character"
    )
    assert [(linked["id"], linked["passage"]) for linked in view["passages"]] == [
        (linked["id"], linked["passage"]) for linked in draft["passages"]
    ], "a note's passages are not given under their ids, as linked, in the order of the draft"
    for linked in view["passages"]:
        source = by_document[linked["passage"]["anchors"][0]["document_id"]]
        assert (linked["item"], linked["title"]) == (source.item, source.title), (
            "a note's passage does not name the item it lies in and what that item is called"
        )
        assert linked["revision"] == revision, "a note's passage names another revision"
        assert linked["standing"] == found(linked["passage"]), (
            "a passage of the document its item holds now does not stand where it was linked"
        )


@st.composite
def linked_passages(
    draw: st.DrawFn, sources: list[Source], most: int = 5
) -> list[tuple[str, dict[str, Any]]]:
    ids = draw(st.lists(st.from_regex(PASSAGE_ID, fullmatch=True), max_size=most, unique=True))
    linked = []
    for identity in ids:
        source = draw(st.sampled_from(sources))
        spans = draw(spans_in(source.blocks))
        linked.append((identity, passage(source.document, source.blocks, spans)))
    return linked


@given(
    data=st.data(),
    title=title_texts,
    markdown=markdown_texts,
    texts=st.lists(st.lists(block_texts, min_size=1, max_size=6), min_size=2, max_size=3),
    edition=st.integers(1, EDITION_BOUND),
)
@EXAMPLES
def test_a_note_is_kept_and_given_back_as_it_was_written(
    app: App, data: st.DataObject, title: str, markdown: str, texts: list[list[str]], edition: int
) -> None:
    owner = new_owner()
    sources = keep_sources(
        app.store, owner, [(f"Source {number}", blocks) for number, blocks in enumerate(texts)]
    )
    draft = note_draft(title, markdown, data.draw(linked_passages(sources)), edition)
    with as_owner(owner):
        before = datetime.now(UTC)
        view = written(app, draft)
        after = datetime.now(UTC)
        read = note_of(app, view["id"])
        listed = notes(app)
    assert_shows(view, draft, sources)
    assert before <= instant(view["made"]) <= after, "a note is not kept with when it was made"
    assert view["changed"] == view["made"], "a new note was not last changed when it was made"
    assert read == view, "a note read is not the note as it was answered when it was kept"
    item_of = {source.document: source.item for source in sources}
    assert listed == [
        {
            "id": view["id"],
            "title": title,
            "made": view["made"],
            "changed": view["changed"],
            "items": items_once(draft, item_of),
        }
    ], "the notes of the library do not list the note with the items of its passages, each once"


def test_a_note_at_the_bounds_of_its_draft_is_kept_whole(app: App) -> None:
    blocks = [f"{SCRIPTS[number % len(SCRIPTS)]} {number}" for number in range(22)]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Long", blocks)])
    twenty = passage(
        source.document, blocks, [(number, 0, len(blocks[number])) for number in range(20)]
    )
    one = passage(source.document, blocks, [(21, 0, 4)])
    shortest, longest = "0", "z" * PASSAGE_ID_CHARACTERS
    title = "\N{MATHEMATICAL FRAKTUR CAPITAL U}" * TITLE_CHARACTERS
    markdown = (f"[a](passage:{shortest}) [b](passage:{longest}) {SCRIPTS[5]}\r\n" * 10_000)[
        :MARKDOWN_CHARACTERS
    ]
    draft = note_draft(title, markdown, [(shortest, twenty), (longest, one)])
    view = written(app, draft)
    assert_shows(view, draft, [source])
    assert note_of(app, view["id"]) == view, "a note at its bounds is not given back as it was kept"
    empty = note_draft("", "", [])
    bare = written(app, empty)
    assert (bare["title"], bare["markdown"], bare["passages"]) == ("", "", []), (
        "a note without a title, text or passages was not kept as it came"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "title past its bound",
        "markdown past its bound",
        "empty id",
        "id in capitals",
        "id past its bound",
        "id with a hyphen",
        "repeated id",
        "passage without anchors",
        "no edition",
        "edition 0",
    ],
)
def test_a_draft_the_models_refuse_is_refused_before_anything_is_kept(app: App, fault: str) -> None:
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Refused", blocks)])
    good = note_draft(
        "Kept", "[a](passage:a)", [("a", passage(source.document, blocks, [(0, 0, 3)]))]
    )
    kept = written(app, good)
    bad = note_draft(
        "Refused", "text", [("a", passage(source.document, blocks, [(0, 0, 3)]))], edition=2
    )
    match fault:
        case "title past its bound":
            bad["title"] = "x" * (TITLE_CHARACTERS + 1)
        case "markdown past its bound":
            bad["markdown"] = "x" * (MARKDOWN_CHARACTERS + 1)
        case "empty id":
            bad["passages"][0]["id"] = ""
        case "id in capitals":
            bad["passages"][0]["id"] = "A"
        case "id past its bound":
            bad["passages"][0]["id"] = "a" * (PASSAGE_ID_CHARACTERS + 1)
        case "id with a hyphen":
            bad["passages"][0]["id"] = "a-b"
        case "repeated id":
            bad["passages"].append(bad["passages"][0])
        case "no edition":
            del bad["edition"]
        case "edition 0":
            bad["edition"] = 0
        case _:
            bad["passages"][0]["passage"] = {"anchors": []}
    listed = notes(app)
    created = app.client.post("/api/notes", json=bad)
    replaced = app.client.put(f"/api/notes/{kept['id']}", json=bad)
    assert (created.status_code, replaced.status_code) == (422, 422), (
        f"a draft with {fault} was not refused: {created.text} {replaced.text}"
    )
    assert (notes(app), note_of(app, kept["id"])) == (listed, kept), (
        f"a draft with {fault} changed what the library keeps"
    )


@pytest.mark.parametrize(
    "fault", ["document not in the library", "words not held", "block past the end"]
)
def test_a_draft_with_a_passage_not_found_or_not_held_changes_nothing(app: App, fault: str) -> None:
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Linked", blocks)])
    good = passage(source.document, blocks, [(0, 0, 3)])
    bad = passage(source.document, blocks, [(1, 0, 3)])
    status = 409
    match fault:
        case "document not in the library":
            bad = passage(NOWHERE_DOCUMENT, blocks, [(1, 0, 3)])
            status = 404
        case "words not held":
            bad["anchors"][0]["quote"] += "\N{PILCROW SIGN}"
        case _:
            bad["anchors"][0]["block_number"] = len(blocks)
    kept = written(app, note_draft("Kept", "[a](passage:a)", [("a", good)]))
    listed = notes(app)
    draft = note_draft(
        "Refused", "[a](passage:a) [b](passage:b)", [("a", good), ("b", bad)], edition=2
    )
    created = app.client.post("/api/notes", json=draft)
    replaced = app.client.put(f"/api/notes/{kept['id']}", json=draft)
    assert (created.status_code, replaced.status_code) == (status, status), (
        f"a note with a passage of a {fault} was not refused as it should be: {created.text}"
    )
    assert (notes(app), note_of(app, kept["id"])) == (listed, kept), (
        "a refused draft changed what the library keeps"
    )


CHANGES = [
    "nothing",
    "title",
    "markdown",
    "a passage removed",
    "a passage added",
    "an id",
    "a passage of another item",
    "the order of the passages",
]


@pytest.mark.parametrize("change", CHANGES)
def test_a_rewrite_puts_the_draft_in_place_and_moves_the_change_only_when_it_differs(
    app: App, change: str
) -> None:
    blocks = [SCRIPTS[0], SCRIPTS[1], SCRIPTS[2]]
    sources = keep_sources(app.store, LOCAL_OWNER, [("First", blocks), ("Second", blocks)])
    first, second = sources
    one = passage(first.document, blocks, [(0, 0, 3)])
    two = passage(first.document, blocks, [(1, 2, 6), (2, 0, 4)])
    draft = note_draft("A note", "[a](passage:a) [b](passage:b)", [("a", one), ("b", two)])
    kept = written(app, draft)
    time.sleep(CLOCK_STEP_SECONDS)
    after = note_draft(
        draft["title"], draft["markdown"], [("a", one), ("b", two)], kept["edition"] + 1
    )
    match change:
        case "title":
            after["title"] = "Another title"
        case "markdown":
            after["markdown"] = draft["markdown"] + " "
        case "a passage removed":
            after["passages"].pop()
        case "a passage added":
            after["passages"].append(
                {"id": "c", "passage": passage(first.document, blocks, [(2, 1, 3)])}
            )
        case "an id":
            after["passages"][1]["id"] = "c"
        case "a passage of another item":
            after["passages"][1]["passage"] = passage(second.document, blocks, [(1, 2, 6)])
        case "the order of the passages":
            after["passages"].reverse()
    view = rewritten(app, kept["id"], after)
    assert_shows(view, after, sources)
    assert (view["id"], view["made"]) == (kept["id"], kept["made"]), (
        "a rewritten note is not the same note, made when it was first written"
    )
    if change == "nothing":
        assert view == {**kept, "edition": after["edition"]}, (
            "a rewrite with the same text at a later edition changed more than the edition"
        )
    else:
        assert instant(view["changed"]) > instant(kept["changed"]), (
            f"a rewrite that changed {change} did not move when the note was last changed"
        )
    assert note_of(app, kept["id"]) == view, "a note read is not the note as it was rewritten"


@given(
    data=st.data(),
    count=st.integers(1, 5),
    rewrites=st.lists(st.integers(0, 4), max_size=6),
)
@FEW_EXAMPLES
def test_the_notes_are_listed_the_one_changed_last_first_with_their_items(
    app: App, data: st.DataObject, count: int, rewrites: list[int]
) -> None:
    owner = new_owner()
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    sources = keep_sources(app.store, owner, [(f"Item {number}", blocks) for number in range(3)])
    item_of = {source.document: source.item for source in sources}
    changed: list[str] = []
    drafts: dict[str, dict[str, Any]] = {}
    editions: dict[str, int] = {}
    with as_owner(owner):
        for number in range(count):
            draft = note_draft(f"Note {number}", "", data.draw(linked_passages(sources)))
            identity = written(app, draft)["id"]
            drafts[identity] = draft
            editions[identity] = 1
            changed.append(identity)
            time.sleep(CLOCK_STEP_SECONDS)
        for step, index in enumerate(rewrites):
            identity = changed[min(index, count - 1)]
            editions[identity] += 1
            draft = note_draft(
                f"Rewrite {step}", "", data.draw(linked_passages(sources)), editions[identity]
            )
            rewritten(app, identity, draft)
            drafts[identity] = draft
            changed.remove(identity)
            changed.append(identity)
            time.sleep(CLOCK_STEP_SECONDS)
        listed = notes(app)
    assert [entry["id"] for entry in listed] == list(reversed(changed)), (
        "the notes of the library are not listed the one changed last first"
    )
    for entry in listed:
        assert entry["title"] == drafts[entry["id"]]["title"], "a note is listed by another title"
        assert entry["items"] == items_once(drafts[entry["id"]], item_of), (
            "a listed note does not give the items of its passages, each once, in their order"
        )


def test_a_removed_note_is_gone(app: App) -> None:
    blocks = [SCRIPTS[0]]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Linked", blocks)])
    kept = written(app, note_draft("Kept", "", []))
    gone = written(
        app, note_draft("Gone", "", [("a", passage(source.document, blocks, [(0, 0, 3)]))])
    )
    path = f"/api/notes/{gone['id']}"
    assert app.client.delete(path).status_code == 204, "a note was not removed"
    assert [entry["id"] for entry in notes(app)] == [kept["id"]], "a removed note is still listed"
    assert app.client.get(path).status_code == 404, "a removed note was found"
    assert app.client.put(path, json=note_draft("Back", "", [], edition=2)).status_code == 404, (
        "a removed note was found to rewrite"
    )
    assert app.client.delete(path).status_code == 404, "a removed note was found to remove"
    assert app.client.delete(f"/api/notes/{NOWHERE_NOTE}").status_code == 404, (
        "a note that is not there was found to remove"
    )
    assert note_of(app, kept["id"]) == kept, "removing a note changed another"


def test_a_note_shows_each_passage_where_it_stands_after_a_second_reading(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = app.read_page(PAGE, FIRST_FORM)
    kept_blocks = [SCRIPTS[0]]
    [other] = keep_sources(app.store, LOCAL_OWNER, [("Beside", kept_blocks)])
    stays = phrase_passage(app, item, [(FERRY[2], "forty-one nights")])
    goes = phrase_passage(app, item, [(GONE, "Odalys")])
    late = phrase_passage(app, item, [(FERRY[3], "half past eight")])
    beside = passage(other.document, kept_blocks, [(0, 4, 9)])
    before = written(
        app,
        note_draft(
            "Ferry", "[x](passage:x)", [("goes", goes), ("beside", beside), ("stays", stays)]
        ),
    )
    reread(app, item, PAGE, SECOND_FORM)
    now = document_of(app, item)
    new = phrase_passage(app, item, [(REPLACEMENT, "later chronicle")])
    after = rewritten(
        app,
        before["id"],
        note_draft(
            "Ferry",
            "[x](passage:x)",
            [("goes", goes), ("beside", beside), ("stays", stays), ("new", new), ("late", late)],
            edition=2,
        ),
    )
    view = note_of(app, before["id"])
    assert view == after, "a note read is not the note as it was rewritten"
    shown = {linked["id"]: linked for linked in view["passages"]}
    assert [linked["id"] for linked in view["passages"]] == [
        "goes",
        "beside",
        "stays",
        "new",
        "late",
    ], "a note's passages are not in the order of its draft"
    assert [shown[name]["revision"] for name in ("goes", "stays", "new", "late")] == [1, 1, 2, 1], (
        "a note's passage does not name the revision it was linked in"
    )
    assert {shown[name]["title"] for name in ("goes", "stays", "new")} == {REVISED_TITLE}, (
        "a note's passage does not give what its item is called now"
    )
    assert shown["goes"]["standing"] == LOST, "a passage whose words are all gone is not lost"
    moved = {"anchors": [{**anchor, "document_id": now["id"]} for anchor in stays["anchors"]]}
    assert shown["stays"]["standing"] == found(moved), (
        "a passage whose words the new document holds unchanged in the block of the same number "
        "is not found there"
    )
    assert (shown["new"]["standing"], shown["beside"]["standing"]) == (found(new), found(beside)), (
        "a passage of the document its item holds now does not stand where it was linked"
    )
    assert shown["late"]["standing"]["kind"] == "found", (
        "a passage of an earlier revision whose words the item's document holds is not found"
    )
    assert shown["goes"]["passage"] == goes, "a note's passage is not given as it was linked"
    with searched_better(monkeypatch):
        better = note_of(app, before["id"])["passages"][0]
    assert better["standing"]["kind"] == "found", (
        "a note's passage's standing was not worked out when it was read: a better search did "
        "not find what the first lost"
    )


def test_another_owners_notes_are_never_answered_or_told_apart_from_none(app: App) -> None:
    blocks = [SCRIPTS[0]]
    [mine] = keep_sources(app.store, LOCAL_OWNER, [("Mine", blocks)])
    my_passage = passage(mine.document, blocks, [(0, 0, 3)])
    my_note = written(app, note_draft("Mine", "[a](passage:a)", [("a", my_passage)]))
    other = new_owner()
    [theirs] = keep_sources(app.store, other, [("Theirs", blocks)])

    def compared(method: Callable[..., httpx2.Response], **arguments: Any) -> tuple[Any, Any]:
        return (
            refusal(method(f"/api/notes/{my_note['id']}", **arguments), my_note["id"]),
            refusal(method(f"/api/notes/{NOWHERE_NOTE}", **arguments), NOWHERE_NOTE),
        )

    with as_owner(other):
        their_note = written(
            app, note_draft("Theirs", "", [("a", passage(theirs.document, blocks, [(0, 0, 3)]))])
        )
        refusals = {
            "reading a note": compared(app.client.get),
            "rewriting a note at its own edition": compared(
                app.client.put, json=note_draft("Taken", "", [])
            ),
            "rewriting a note at a later edition": compared(
                app.client.put, json=note_draft("Taken", "", [], edition=2)
            ),
            "removing a note": compared(app.client.delete),
            "linking a passage of a document": (
                refusal(
                    app.client.post(
                        "/api/notes", json=note_draft("Taken", "", [("a", my_passage)])
                    ),
                    mine.document,
                ),
                refusal(
                    app.client.post(
                        "/api/notes",
                        json=note_draft(
                            "Taken", "", [("a", passage(NOWHERE_DOCUMENT, blocks, [(0, 0, 3)]))]
                        ),
                    ),
                    NOWHERE_DOCUMENT,
                ),
            ),
        }
        their_list = notes(app)
    for operation, (of_another, of_none) in refusals.items():
        assert of_another[0] == 404, f"{operation} of another owner's was not refused as not found"
        assert of_another == of_none, (
            f"{operation} of another owner's is told apart from one that is not there: "
            f"{of_another} against {of_none}"
        )
    assert [entry["id"] for entry in their_list] == [their_note["id"]], (
        "an owner's notes hold another owner's, or not their own"
    )
    assert [entry["id"] for entry in notes(app)] == [my_note["id"]], (
        "an owner's notes hold another owner's"
    )
    assert note_of(app, my_note["id"]) == my_note, "another owner changed a note"


def test_notes_are_kept_over_a_restart(start_app: Callable[..., App]) -> None:
    app = start_app()
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    first, second = keep_sources(app.store, LOCAL_OWNER, [("One", blocks), ("Two", blocks)])
    kept = written(
        app,
        note_draft(
            SCRIPTS[1],
            MARKDOWNS[0],
            [
                ("a", passage(second.document, blocks, [(1, 0, 3)])),
                ("b", passage(first.document, blocks, [(0, 2, 5), (1, 0, 2)])),
            ],
        ),
    )
    gone = written(app, note_draft("Gone", "", []))
    assert app.client.delete(f"/api/notes/{gone['id']}").status_code == 204, (
        "a note was not removed"
    )
    listed = notes(app)
    app.stop()
    again = start_app()
    assert notes(again) == listed, "the notes are not as they were after the service started again"
    assert note_of(again, kept["id"]) == kept, "a note is not as it was after the service restarted"


def test_a_note_of_fifty_passages_in_twenty_items_is_given_whole(app: App) -> None:
    blocks = [f"{SCRIPTS[number % len(SCRIPTS)]} {number}" for number in range(3)]
    sources = keep_sources(
        app.store, LOCAL_OWNER, [(f"Item {number}", blocks) for number in range(MANY_ITEMS)]
    )
    linked = [
        (
            f"p{number}",
            passage(
                sources[number % MANY_ITEMS].document,
                blocks,
                [(number // MANY_ITEMS, 0, 4)],
            ),
        )
        for number in range(MANY_PASSAGES)
    ]
    draft = note_draft("Many", "", linked)
    view = written(app, draft)
    assert_shows(note_of(app, view["id"]), draft, sources)
    [entry] = notes(app)
    assert entry["items"] == [source.item for source in sources], (
        "a note of many passages does not list each of its items once, in their order"
    )


# R14: a note takes only a later writing.


def put(app: App, note: str, draft: dict[str, Any]) -> httpx2.Response:
    return app.client.put(f"/api/notes/{note}", json=draft)


def assert_failure(response: httpx2.Response, status: int, what: str) -> None:
    assert response.status_code == status, (
        f"{what} was not answered {status}: {response.status_code} {response.text}"
    )
    body = response.json()
    assert set(body) == {"detail"} and isinstance(body["detail"], str), (
        f"{what} was not answered with a Failure: {body}"
    )


def numbered_draft(number: int, edition: int, source: Source) -> dict[str, Any]:
    # A draft told from every other by its title, its Markdown and its passages.
    linked = passage(source.document, source.blocks, [(number % len(source.blocks), 0, 1)])
    return note_draft(f"Draft {number}", f"text {number}", [(f"p{number}", linked)], edition)


@given(
    first=st.integers(1, 6),
    editions=st.lists(st.integers(1, 8), min_size=1, max_size=10),
)
@EXAMPLES
def test_a_note_takes_only_a_draft_of_a_later_edition_than_it_holds(
    app: App, first: int, editions: list[int]
) -> None:
    owner = new_owner()
    [source] = keep_sources(app.store, owner, [("Edited", [SCRIPTS[0], SCRIPTS[1]])])
    with as_owner(owner):
        held = written(app, numbered_draft(0, first, source))
        assert held["edition"] == first, "a new note was not kept at the edition its draft names"
        for number, edition in enumerate(editions, start=1):
            draft = numbered_draft(number, edition, source)
            response = put(app, held["id"], draft)
            if edition > held["edition"]:
                assert response.status_code == 200, (
                    f"a draft of a later edition ({edition} over {held['edition']}) was refused:"
                    f" {response.text}"
                )
                view = response.json()
                assert_shows(view, draft, [source])
                assert view["made"] == held["made"], "a rewrite changed when the note was made"
                held = view
            else:
                assert_failure(
                    response, 412, f"a draft of edition {edition} to a note at {held['edition']}"
                )
                assert note_of(app, held["id"]) == held, (
                    "a draft that was not of a later edition changed the note"
                )
        final = note_of(app, held["id"])
        [entry] = notes(app)
    assert final == held, "the note does not hold the last draft it took"
    assert final["edition"] == max(first, *editions), (
        "a note sent drafts one after another does not end at the greatest edition sent"
    )
    assert (entry["title"], entry["changed"]) == (final["title"], final["changed"]), (
        "the notes of the library list a note otherwise than it is held"
    )


@given(editions=st.lists(st.integers(1, 4), min_size=2, max_size=8))
@FEW_EXAMPLES
def test_of_drafts_sent_at_once_the_note_ends_at_the_greatest_edition_taking_one_of_each(
    app: App, editions: list[int]
) -> None:
    owner = new_owner()
    [source] = keep_sources(app.store, owner, [("Raced", [SCRIPTS[0], SCRIPTS[1]])])
    with as_owner(owner):
        kept = written(app, numbered_draft(0, 1, source))
        drafts = [
            numbered_draft(number, edition, source)
            for number, edition in enumerate(editions, start=1)
        ]
        with ThreadPoolExecutor(max_workers=len(drafts)) as pool:
            responses = list(pool.map(lambda draft: put(app, kept["id"], draft), drafts))
        final = note_of(app, kept["id"])
    statuses = [response.status_code for response in responses]
    assert set(statuses) <= {200, 412}, f"a draft sent at once was answered otherwise: {statuses}"
    taken = [edition for edition, status in zip(editions, statuses, strict=True) if status == 200]
    assert len(taken) == len(set(taken)), (
        f"of drafts of one edition sent at once more than one was taken: {editions} {statuses}"
    )
    assert all(edition > 1 for edition in taken), "a draft not later than the note was taken"
    for draft, response in zip(drafts, responses, strict=True):
        if response.status_code == 200:
            assert_shows(response.json(), draft, [source])
    for edition, response in zip(editions, responses, strict=True):
        if response.status_code == 412:
            assert_failure(response, 412, "a stale draft sent at once")
            assert edition <= max([1, *taken]), (
                "a draft was refused that no edition the note held could have been later than"
            )
    greatest = max(editions)
    assert final["edition"] == greatest, (
        "a note sent drafts at once does not end at the greatest edition sent"
    )
    if greatest == 1:
        assert final == kept, "drafts not later than the note changed it"
        return
    winners = [
        draft
        for draft, status in zip(drafts, statuses, strict=True)
        if status == 200 and draft["edition"] == greatest
    ]
    assert len(winners) == 1, "of drafts of the greatest edition sent at once, not one was taken"
    assert_shows(final, winners[0], [source])


def test_of_several_drafts_of_one_edition_at_the_same_moment_exactly_one_is_taken(
    app: App,
) -> None:
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Raced", [SCRIPTS[0], SCRIPTS[1]])])
    kept = written(app, numbered_draft(0, 1, source))
    drafts = [numbered_draft(number, 2, source) for number in range(1, 9)]
    barrier = Barrier(len(drafts))

    def sent(draft: dict[str, Any]) -> httpx2.Response:
        barrier.wait()
        return put(app, kept["id"], draft)

    with ThreadPoolExecutor(max_workers=len(drafts)) as pool:
        responses = list(pool.map(sent, drafts))
    statuses = [response.status_code for response in responses]
    assert sorted(statuses) == [200] + [412] * (len(drafts) - 1), (
        f"of drafts of one edition sent at the same moment, not exactly one was taken: {statuses}"
    )
    [winner] = [draft for draft, status in zip(drafts, statuses, strict=True) if status == 200]
    final = note_of(app, kept["id"])
    assert_shows(final, winner, [source])
    assert final == responses[statuses.index(200)].json(), (
        "the note does not hold the draft that was taken"
    )


def test_the_same_text_at_a_later_edition_takes_the_edition_and_leaves_the_change(
    app: App,
) -> None:
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Same", [SCRIPTS[0], SCRIPTS[1]])])
    kept = written(app, numbered_draft(1, 1, source))
    time.sleep(CLOCK_STEP_SECONDS)
    again = rewritten(app, kept["id"], numbered_draft(1, 2, source))
    assert again == {**kept, "edition": 2}, (
        "the same text at a later edition did not take the edition alone, leaving the change"
    )
    assert_failure(
        put(app, kept["id"], numbered_draft(1, 2, source)), 412, "the same draft sent twice"
    )
    assert note_of(app, kept["id"]) == again, "a refused draft changed the note"
    time.sleep(CLOCK_STEP_SECONDS)
    later = rewritten(app, kept["id"], numbered_draft(2, 5, source))
    assert later["edition"] == 5 and instant(later["changed"]) > instant(again["changed"]), (
        "a later draft with other text did not take its edition and move the change"
    )


@pytest.mark.parametrize(
    ("fault", "later_status"), [("words not held", 409), ("document not in the library", 404)]
)
def test_a_stale_draft_is_refused_as_stale_before_its_passages_are_looked_at(
    app: App, fault: str, later_status: int
) -> None:
    blocks = [SCRIPTS[0], SCRIPTS[1]]
    [source] = keep_sources(app.store, LOCAL_OWNER, [("Stale", blocks)])
    kept = written(app, note_draft("Kept", "", [], edition=3))
    bad = passage(source.document, blocks, [(1, 0, 3)])
    if fault == "words not held":
        bad["anchors"][0]["quote"] += "\N{PILCROW SIGN}"
    else:
        bad = passage(NOWHERE_DOCUMENT, blocks, [(1, 0, 3)])
    for edition in (1, 3):
        assert_failure(
            put(app, kept["id"], note_draft("Stale", "", [("b", bad)], edition)),
            412,
            f"a stale draft (edition {edition} to a note at 3) with a passage of {fault}",
        )
    assert_failure(
        put(app, kept["id"], note_draft("Later", "", [("b", bad)], edition=4)),
        later_status,
        f"a later draft with a passage of {fault}",
    )
    assert note_of(app, kept["id"]) == kept, "a refused draft changed the note"
    for edition in (1, 4):
        assert put(app, NOWHERE_NOTE, note_draft("Absent", "", [], edition)).status_code == 404, (
            f"a note that is not there was not refused as not found at edition {edition}"
        )
