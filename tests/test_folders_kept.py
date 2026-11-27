import shutil
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

from infonium.api import Media
from infonium.library import LIBRARY_FILE, LOCAL_OWNER
from tests.feed_standin import FeedStandin
from tests.foldering import (
    entries,
    folders_listed,
    made_folder,
    media_by_rule,
    placed,
    shown_now,
)
from tests.library_before_digest_styles import DATA_FOLDER
from tests.library_before_folders import (
    ANSWERS,
    KEPT_FILE,
    OTHER_OWNER,
    AnswersBeforeFolders,
    OwnerAnswers,
    answered_owner,
)
from tests.service import App, as_owner
from tests.subscribing import subscription_path, viewed
from tests.test_digest_styles import opened_before_styles
from tests.test_subscriptions_kept import (
    INDEX_TABLES,
    VIRTUAL_TABLE,
    library_file,
    rows_of,
    tables_of,
)

# The judgments of S11 of the service's brief of the sidebar: a library kept before folders, by
# the service of commit 3ddbaaa, opens with everything in no folder, answers as it did but for
# the fields that tell an item's media and folder and a subscription's folder, keeps the rows
# it held, and takes folders and placings as a library made now does.
OWNERS = (LOCAL_OWNER, OTHER_OWNER)
# The fields that entries and views of subscriptions have had since folders.
ENTRY_FIELDS = frozenset({"media", "folder"})
VIEW_FIELDS = frozenset({"folder"})


def opened_before_folders(
    start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path
) -> tuple[App, AnswersBeforeFolders]:
    # The service started on a copy of the library kept before folders, polling with the clock
    # it was kept by, so that no poll is due; and what the service answered about it then.
    answers = AnswersBeforeFolders.model_validate_json(ANSWERS.read_text(encoding="utf-8"))
    data = tmp_path / DATA_FOLDER
    data.mkdir()
    shutil.copyfile(KEPT_FILE, data / LIBRARY_FILE)
    return start_app(poller=feeds.poller), answers


def without(answer: dict[str, Any], fields: frozenset[str], what: str) -> dict[str, Any]:
    # An answer as it was given before folders: in no folder, without the fields added since.
    assert answer["folder"] is None, f"S11: {what} of a library kept before folders is in a folder"
    return {field: value for field, value in answer.items() if field not in fields}


def entry_before(entry: dict[str, Any]) -> dict[str, Any]:
    return without(entry, ENTRY_FIELDS, "an item")


def view_before(view: dict[str, Any]) -> dict[str, Any]:
    return without(view, VIEW_FIELDS, "a subscription")


def posting_before(posting: dict[str, Any]) -> dict[str, Any]:
    admission = posting["admission"]
    if "item" not in admission:
        return posting
    return {**posting, "admission": {**admission, "item": entry_before(admission["item"])}}


def answered_as_before(now: OwnerAnswers) -> OwnerAnswers:
    return OwnerAnswers(
        items=[entry_before(entry) for entry in now.items],
        entries={item: entry_before(entry) for item, entry in now.entries.items()},
        media_types=now.media_types,
        subscriptions=[view_before(view) for view in now.subscriptions],
        views={subscription: view_before(view) for subscription, view in now.views.items()},
        postings={
            subscription: [posting_before(posting) for posting in postings]
            for subscription, postings in now.postings.items()
        },
    )


def test_a_library_kept_before_folders_answers_as_before_with_everything_in_no_folder(
    start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path
) -> None:
    app, answers = opened_before_folders(start_app, feeds, tmp_path)
    assert set(answers.owners) == set(OWNERS), "the library kept is not of the owners judged"
    for owner, before in answers.owners.items():
        assert before.items and before.subscriptions, f"the library kept holds nothing of {owner}"
        with as_owner(owner):
            assert folders_listed(app) == [], "S11: a library kept before folders has folders"
            now = answered_as_before(answered_owner(app))
        for part in OwnerAnswers.model_fields:
            assert getattr(now, part) == getattr(before, part), (
                f"S11: the {part} of a library kept before folders are not answered as they were, "
                "apart from the fields added since"
            )


def test_the_items_of_a_library_kept_before_folders_are_told_the_media_of_their_documents(
    start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path
) -> None:
    app, answers = opened_before_folders(start_app, feeds, tmp_path)
    told: dict[str, object] = {}
    expected: dict[str, object] = {}
    for owner, before in answers.owners.items():
        with as_owner(owner):
            told |= {entry["id"]: entry["media"] for entry in entries(app)}
        expected |= {
            entry["id"]: (
                media_by_rule(before.media_types[entry["id"]])
                if entry["id"] in before.media_types
                else None
            )
            for entry in before.items
        }
    assert set(expected.values()) == {*Media, None}, (
        "the library kept does not hold an item of each media and one of none"
    )
    assert told == expected, (
        f"S6, S11: the items of a library kept before folders are not told the media of the "
        f"documents they hold, or none where they hold none: {told} against {expected}"
    )


def test_a_library_kept_before_folders_keeps_the_rows_of_its_tables_when_it_is_answered(
    start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path
) -> None:
    file = tmp_path / DATA_FOLDER / LIBRARY_FILE
    file.parent.mkdir()
    shutil.copyfile(KEPT_FILE, file)
    tables = tables_of(file)
    compared = [
        table
        for table, made in tables.items()
        if not table.startswith(INDEX_TABLES) and not made.startswith(VIRTUAL_TABLE)
    ]
    held = rows_of(file, compared)
    assert held["item"] and held["subscription"], "the library kept holds no items or subscriptions"
    app = start_app(poller=feeds.poller)
    for owner in OWNERS:
        with as_owner(owner):
            assert folders_listed(app) == [], "S11: a library kept before folders has folders"
            answered_owner(app)
    app.stop()
    app.store.engine.dispose()
    now = tables_of(library_file(tmp_path))
    for table in compared:
        assert now[table] == tables[table], (
            f"S11: the service changed the table {table} of a library kept before folders"
        )
    after = rows_of(file, compared)
    for table in compared:
        assert after[table] == held[table], (
            f"S11: answering a library kept before folders changed the rows of its table {table}"
        )


def test_a_library_kept_before_folders_takes_folders_placings_and_removals(
    start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path
) -> None:
    app, answers = opened_before_folders(start_app, feeds, tmp_path)
    before = answers.owners[LOCAL_OWNER]
    reading = next(view["id"] for view in before.subscriptions if view["follows"] == "reads")
    brought = [
        entry["id"] for entry in before.items if entry["origin"].get("subscription") == reading
    ]
    hand_added = [entry["id"] for entry in before.items if entry["origin"]["kind"] != "subscribed"]
    assert brought and hand_added, "the library kept holds no items of a subscription or by hand"
    papers = made_folder(app, "Papers")["id"]
    placed(app, papers, [reading], hand_added)
    shown = shown_now(app)
    assert shown.subscriptions[reading] == papers, (
        "S5, S11: a subscription of a library kept before folders is not where it was placed"
    )
    assert {item: shown.items[item] for item in [*brought, *hand_added]} == dict.fromkeys(
        [*brought, *hand_added], papers
    ), "S5, S7, S11: the items of a library kept before folders are not where they were placed"
    assert viewed(app, reading)["folder"] == papers, "S8: a subscription is told in no folder"
    response = app.client.delete(subscription_path(reading))
    assert response.status_code == HTTPStatus.NO_CONTENT, "removing a subscription was refused"
    after = shown_now(app)
    assert {item: after.items[item] for item in brought} == dict.fromkeys(brought, papers), (
        "S9, S11: the items of a removed subscription of a library kept before folders did not "
        "stay in the folder it was in"
    )
    with as_owner(OTHER_OWNER):
        assert shown_now(app).folders == [], "S10, S11: another owner is shown the owner's folder"


def test_the_items_of_a_library_kept_before_digest_styles_are_web_articles_in_no_folder(
    start_app: Callable[..., App], tmp_path: Path
) -> None:
    app, answers = opened_before_styles(start_app, tmp_path)
    told = {entry["id"]: (entry["media"], entry["folder"]) for entry in entries(app)}
    assert told and set(told) == {entry["id"] for entry in answers.items}, (
        "the items of the library kept before digest styles are not those it held"
    )
    assert set(told.values()) == {("web", None)}, (
        f"S6, S11: the pages of a library kept before digest styles are not web articles in no "
        f"folder: {told}"
    )
