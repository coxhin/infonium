import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from threading import Barrier

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.library import LIBRARY_FILE
from tests.feed_standin import FeedStandin
from tests.foldering import (
    FOLDERS,
    ITEMS,
    Arrangement,
    Shown,
    arranged_as,
    dissolve_folder,
    folders_listed,
    library_arranged,
    made_folder,
    place,
    shown_now,
)
from tests.library_before_digest_styles import DATA_FOLDER
from tests.service import App, as_owner, new_owner
from tests.subscribing import SUBSCRIPTIONS, subscription_path
from tests.test_subscriptions_kept import INDEX_TABLES, VIRTUAL_TABLE, tables_of

# The judgments of S12 of the service's brief of the sidebar: one owner's placings, dissolvings
# and removals of subscriptions sent at once from several threads, with lists of items,
# subscriptions and folders asked among them.
EXAMPLES = settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
THREADS = 3
MOST_REQUESTS = 3
FOLDERS_MADE = 3
# What the subscriptions of the library hold: as many items that follow each as each number.
FOLLOWING = [1, 1, 1]
HAND_ADDED_KEPT = 3
ITEMS_KEPT = sum(FOLLOWING) + HAND_ADDED_KEPT
folders_drawn = st.none() | st.integers(0, FOLDERS_MADE - 1)


class Asked(StrEnum):
    PLACING = "a placing"
    DISSOLVING = "a dissolving"
    REMOVAL = "a removal of a subscription"
    ITEMS = "the list of items"
    SUBSCRIPTIONS = "the list of subscriptions"
    FOLDERS = "the list of folders"


class Drawn(BaseModel):
    # A request drawn: what it asks, a folder by its place among those made, or none, and
    # subscriptions and items by their places among those kept, every item among them. A
    # dissolving dissolves the folder, the first where none is drawn; a removal removes the
    # first subscription drawn, the first kept where none is.
    asked: Asked
    folder: int | None
    subscriptions: list[int]
    items: list[int]


class Named(BaseModel):
    # A request by the ids of what it names.
    asked: Asked
    folder: str | None
    subscriptions: list[str]
    items: list[str]


class Done(BaseModel):
    # A request as it was sent and answered: when it began and ended on the monotonic clock,
    # the status it was answered, and what a list answered: the folder of each item or each
    # subscription, or the ids of the folders.
    named: Named
    began: float
    ended: float
    status: int
    listed: dict[str, str | None] | list[str] | None


requests_drawn = st.builds(
    Drawn,
    asked=st.sampled_from(Asked),
    folder=folders_drawn,
    subscriptions=st.lists(st.integers(0, len(FOLLOWING) - 1), max_size=2),
    items=st.lists(st.integers(0, ITEMS_KEPT - 1), max_size=3),
)
threads_drawn = st.lists(
    st.lists(requests_drawn, min_size=1, max_size=MOST_REQUESTS), min_size=THREADS, max_size=THREADS
)
beginnings = st.lists(
    folders_drawn,
    min_size=len(FOLLOWING) + HAND_ADDED_KEPT,
    max_size=len(FOLLOWING) + HAND_ADDED_KEPT,
)
# A placing and a dissolving of one folder and a removal of a subscription in it, at once.
RACED = [
    [Drawn(asked=Asked.PLACING, folder=0, subscriptions=[1], items=[3, 4, 5])],
    [Drawn(asked=Asked.DISSOLVING, folder=0, subscriptions=[], items=[])],
    [
        Drawn(asked=Asked.REMOVAL, folder=None, subscriptions=[0], items=[]),
        Drawn(asked=Asked.ITEMS, folder=None, subscriptions=[], items=[]),
    ],
]


def named(arrangement: Arrangement, drawn: Drawn) -> Named:
    subscriptions = list(arrangement.subscriptions)
    items = list(arrangement.origins)
    folder = None if drawn.folder is None else arrangement.folders[drawn.folder]
    match drawn.asked:
        case Asked.DISSOLVING:
            folder = folder or arrangement.folders[0]
        case Asked.REMOVAL:
            drawn = drawn.model_copy(update={"subscriptions": drawn.subscriptions[:1] or [0]})
    return Named(
        asked=drawn.asked,
        folder=folder,
        subscriptions=[subscriptions[at] for at in drawn.subscriptions],
        items=[items[at] for at in drawn.items],
    )


def sent(app: App, request: Named) -> tuple[int, dict[str, str | None] | list[str] | None]:
    match request.asked:
        case Asked.PLACING:
            return place(
                app, request.folder, request.subscriptions, request.items
            ).status_code, None
        case Asked.DISSOLVING:
            return dissolve_folder(app, str(request.folder)).status_code, None
        case Asked.REMOVAL:
            response = app.client.delete(subscription_path(request.subscriptions[0]))
            return response.status_code, None
        case Asked.ITEMS:
            response = app.client.get(ITEMS)
            return response.status_code, {entry["id"]: entry["folder"] for entry in response.json()}
        case Asked.SUBSCRIPTIONS:
            response = app.client.get(SUBSCRIPTIONS)
            return response.status_code, {view["id"]: view["folder"] for view in response.json()}
        case _:
            response = app.client.get(FOLDERS)
            return response.status_code, [view["id"] for view in response.json()]


def at_once(app: App, threads: list[list[Named]]) -> list[list[Done]]:
    # The requests of each thread sent one after another, the threads let go at the same moment.
    starting = Barrier(len(threads))

    def sent_in_turn(requests: list[Named]) -> list[Done]:
        starting.wait()
        done: list[Done] = []
        for request in requests:
            began = time.monotonic()
            status, listed = sent(app, request)
            done.append(
                Done(
                    named=request, began=began, ended=time.monotonic(), status=status, listed=listed
                )
            )
        return done

    with ThreadPoolExecutor(max_workers=len(threads)) as pool:
        return list(pool.map(sent_in_turn, threads))


def answered_so(arrangement: Arrangement, done: Done) -> tuple[bool, Arrangement]:
    # Whether the rules, the library arranged so, answer the request as it was answered, and how
    # the library is arranged after it.
    request = done.named
    shown = arrangement.shown()
    match request.asked:
        case Asked.PLACING:
            refused = arrangement.refusals_of_placing(
                request.folder, request.subscriptions, request.items
            )
            if refused:
                return done.status in refused, arrangement
            placed = arrangement.placing(request.folder, request.subscriptions, request.items)
            return done.status == HTTPStatus.NO_CONTENT, placed
        case Asked.DISSOLVING if request.folder in arrangement.folders:
            return done.status == HTTPStatus.NO_CONTENT, arrangement.dissolving(str(request.folder))
        case Asked.REMOVAL if request.subscriptions[0] in arrangement.subscriptions:
            removed = arrangement.removing(request.subscriptions[0])
            return done.status == HTTPStatus.NO_CONTENT, removed
        case Asked.DISSOLVING | Asked.REMOVAL:
            return done.status == HTTPStatus.NOT_FOUND, arrangement
        case Asked.ITEMS:
            return (done.status, done.listed) == (HTTPStatus.OK, shown.items), arrangement
        case Asked.SUBSCRIPTIONS:
            return (done.status, done.listed) == (HTTPStatus.OK, shown.subscriptions), arrangement
        case _:
            return (done.status, done.listed) == (HTTPStatus.OK, shown.folders), arrangement


def in_some_order(arrangement: Arrangement, threads: list[list[Done]], final: Shown) -> bool:
    # Whether some order of the requests, each thread's in the order it sent them, and each
    # request after every request that was answered before it was sent, is answered as they
    # were, and leaves the library as it is shown at the end.
    tried: set[tuple[tuple[int, ...], str]] = set()

    def ordered_from(arrangement: Arrangement, sent_so_far: tuple[int, ...]) -> bool:
        key = (sent_so_far, arrangement.model_dump_json())
        if key in tried:
            return False
        tried.add(key)
        waiting = [
            (thread, done[count])
            for thread, (count, done) in enumerate(zip(sent_so_far, threads, strict=True))
            if count < len(done)
        ]
        if not waiting:
            return arrangement.shown() == final
        for thread, request in waiting:
            if any(
                other.ended < request.began for elsewhere, other in waiting if elsewhere != thread
            ):
                continue
            answered_as_sent, after = answered_so(arrangement, request)
            further = tuple(count + (place == thread) for place, count in enumerate(sent_so_far))
            if answered_as_sent and ordered_from(after, further):
                return True
        return False

    return ordered_from(arrangement, (0,) * len(threads))


def told(threads: list[list[Done]]) -> list[list[str]]:
    return [
        [f"{done.named.asked} answered {done.status}" for done in requests] for requests in threads
    ]


@given(drawn=threads_drawn, beginning=beginnings)
@example(drawn=RACED, beginning=[None, 0, None, 0, 0, None])
@EXAMPLES
def test_requests_sent_at_once_are_answered_as_some_order_of_them_would_be(
    subscribing_app: App, feeds: FeedStandin, drawn: list[list[Drawn]], beginning: list[int | None]
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        arrangement = library_arranged(app, feeds, owner, FOLDERS_MADE, FOLLOWING, HAND_ADDED_KEPT)
        arrangement = arranged_as(app, arrangement, beginning)
        threads = at_once(
            app, [[named(arrangement, request) for request in each] for each in drawn]
        )
        final = shown_now(app)
    assert in_some_order(arrangement, threads, final), (
        "S12: placings, dissolvings and removals of subscriptions sent at once, with lists among "
        "them, were not answered as some order of them would be, or did not leave the library as "
        f"it would: {told(threads)}, ending with {final}"
    )


def rows_naming(file: Path, names: list[str]) -> list[str]:
    # The tables of the library's file, but its index's, that hold one of the ids given in a
    # column of their own or as a string of JSON, each with the id it holds.
    with closing(sqlite3.connect(file)) as connection:
        return [
            f"{table}: {name}"
            for table, made in tables_of(file).items()
            if not table.startswith(INDEX_TABLES) and not made.startswith(VIRTUAL_TABLE)
            for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for cell in row
            for name in names
            if isinstance(cell, str) and (cell == name or f'"{name}"' in cell)
        ]


@given(drawn=threads_drawn, beginning=beginnings)
@example(drawn=RACED, beginning=[None, 0, None, 0, 0, None])
@EXAMPLES
def test_no_placement_names_a_folder_dissolved_among_requests_sent_at_once(
    subscribing_app: App,
    feeds: FeedStandin,
    tmp_path: Path,
    drawn: list[list[Drawn]],
    beginning: list[int | None],
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        arrangement = library_arranged(app, feeds, owner, FOLDERS_MADE, FOLLOWING, HAND_ADDED_KEPT)
        arrangement = arranged_as(app, arrangement, beginning)
        names = {folder["id"]: folder["name"] for folder in folders_listed(app)}
        at_once(app, [[named(arrangement, request) for request in each] for each in drawn])
        final = shown_now(app)
        told_folders = {*final.items.values(), *final.subscriptions.values()} - {None}
        assert told_folders <= set(final.folders), (
            f"S12: after requests sent at once, an item or a subscription is told in a folder "
            f"that is not there: {told_folders - set(final.folders)}"
        )
        dissolved = [folder for folder in names if folder not in final.folders]
        holding = rows_naming(tmp_path / DATA_FOLDER / LIBRARY_FILE, dissolved)
        assert holding == [], (
            f"S4, S12: after requests sent at once, the library still holds a placement in a "
            f"folder that was dissolved: {holding}"
        )
        anew = {made_folder(app, names[folder])["id"] for folder in dissolved}
        shown = shown_now(app)
        assert anew.isdisjoint({*shown.items.values(), *shown.subscriptions.values()}), (
            "S4, S12: a folder made anew with the name of one dissolved among requests sent at "
            f"once holds what that one held: {shown}"
        )
