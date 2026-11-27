from enum import StrEnum
from http import HTTPStatus
from typing import Any
from uuid import uuid4

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from tests.feed_standin import FeedStandin
from tests.foldering import (
    ITEMS,
    NOWHERE_FOLDER,
    NOWHERE_SUBSCRIPTION,
    Arrangement,
    dissolve_folder,
    entries,
    keep_web_item,
    library_arranged,
    link_origin,
    made_folder,
    place,
    placed,
    shown_now,
    subscription_of,
    unchanged,
)
from tests.notebook import NOWHERE_ITEM
from tests.pdf_engine_standin import PDF_DATA
from tests.service import LEDGER, App, article, as_owner, new_owner
from tests.subscribing import (
    ADMITTED,
    admission_of,
    admissions_ended,
    answered,
    every_posting,
    listed,
    page_address,
    page_posting,
    served_feed,
    subscribed,
    subscription_path,
)

# The judgments of S5 and S7 of the service's brief of the sidebar, through the service's
# endpoints: placing subscriptions and items in folders, and the folder that every answer tells
# of an item.
EXAMPLES = settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
FOLDERS_MADE = 3
HAND_ADDED_KEPT = 4
# What a subscription in a drawn library holds: as many items that follow it as each number.
FOLLOWING = [1, 2]
PDF = (PDF_DATA / "samples" / "trap.pdf").read_bytes()
# A folder by its place among the owner's, or none.
folders_drawn = st.none() | st.integers(0, FOLDERS_MADE - 1)
# A placing by the places of what it names among the folders, the subscriptions and the
# hand-added items of a drawn library, each named any number of times.
placings_drawn = st.tuples(
    folders_drawn,
    st.lists(st.integers(0, len(FOLLOWING) - 1), max_size=3),
    st.lists(st.integers(0, HAND_ADDED_KEPT - 1), max_size=5),
)
# Where a drawn library's subscriptions and hand-added items are placed before a judgment
# places anything: a folder or none for each, the subscriptions first.
beginnings = st.lists(
    folders_drawn,
    min_size=len(FOLLOWING) + HAND_ADDED_KEPT,
    max_size=len(FOLLOWING) + HAND_ADDED_KEPT,
)


class Stranger(StrEnum):
    # What a placing may name that the owner does not have.
    FOLDER_NEVER_MADE = "a folder never made"
    FOLDER_DISSOLVED = "a folder dissolved"
    FOLDER_OF_ANOTHER = "another owner's folder"
    SUBSCRIPTION_NEVER_MADE = "a subscription never made"
    SUBSCRIPTION_REMOVED = "a subscription removed"
    SUBSCRIPTION_OF_ANOTHER = "another owner's subscription"
    ITEM_NEVER_KEPT = "an item never kept"
    ITEM_OF_ANOTHER = "another owner's item"


FOLDER_STRANGERS = frozenset(
    {Stranger.FOLDER_NEVER_MADE, Stranger.FOLDER_DISSOLVED, Stranger.FOLDER_OF_ANOTHER}
)
SUBSCRIPTION_STRANGERS = frozenset(
    {
        Stranger.SUBSCRIPTION_NEVER_MADE,
        Stranger.SUBSCRIPTION_REMOVED,
        Stranger.SUBSCRIPTION_OF_ANOTHER,
    }
)


def hand_added_of(arrangement: Arrangement) -> list[str]:
    return [item for item, subscription in arrangement.origins.items() if subscription is None]


def following_of(arrangement: Arrangement) -> list[str]:
    return [item for item in arrangement.origins if arrangement.follows(item)]


def named_by(
    arrangement: Arrangement, drawn: tuple[int | None, list[int], list[int]]
) -> tuple[str | None, list[str], list[str]]:
    # The folder, subscriptions and hand-added items a drawn placing names, by their ids.
    folder, subscriptions, items = drawn
    every_subscription = list(arrangement.subscriptions)
    hand_added = hand_added_of(arrangement)
    return (
        None if folder is None else arrangement.folders[folder],
        [every_subscription[at] for at in subscriptions],
        [hand_added[at] for at in items],
    )


def arranged_as(app: App, arrangement: Arrangement, beginning: list[int | None]) -> Arrangement:
    # The library with each of its subscriptions, then each of its hand-added items, placed in
    # the folder drawn for it, one placing each; where the rules then have everything.
    for named, drawn in zip(
        [*arrangement.subscriptions, *hand_added_of(arrangement)], beginning, strict=True
    ):
        folder = None if drawn is None else arrangement.folders[drawn]
        subscriptions = [named] if named in arrangement.subscriptions else []
        items = [] if subscriptions else [named]
        placed(app, folder, subscriptions, items)
        arrangement = arrangement.placing(folder, subscriptions, items)
    assert shown_now(app) == arrangement.shown(), (
        "S5, S7, S8: the subscriptions and hand-added items placed one by one are not where "
        "they were placed"
    )
    return arrangement


# S5: placing.


@given(beginning=beginnings, placings=st.lists(placings_drawn, min_size=1, max_size=5))
@example(beginning=[None] * 6, placings=[(0, [0, 0, 1], [0, 1, 1]), (0, [0], [0]), (None, [], [3])])
@EXAMPLES
def test_placing_puts_every_subscription_and_item_named_in_the_folder_or_in_none(
    subscribing_app: App,
    feeds: FeedStandin,
    beginning: list[int | None],
    placings: list[tuple[int | None, list[int], list[int]]],
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        arrangement = library_arranged(app, feeds, owner, FOLDERS_MADE, FOLLOWING, HAND_ADDED_KEPT)
        arrangement = arranged_as(app, arrangement, beginning)
        for drawn in placings:
            folder, subscriptions, items = named_by(arrangement, drawn)
            response = place(app, folder, subscriptions, items)
            assert response.status_code == HTTPStatus.NO_CONTENT, (
                "S5: a placing of the owner's own subscriptions and hand-added items, some named "
                f"twice or there already, was answered {response.status_code}, not 204: "
                f"{response.text[:300]}"
            )
            arrangement = arrangement.placing(folder, subscriptions, items)
            shown = shown_now(app)
            assert (shown.folders, shown.subscriptions) == (
                arrangement.folders,
                arrangement.subscriptions,
            ), "S5: a subscription placed is not in the folder it was placed in, or another moved"
            assert {item: shown.items[item] for item in hand_added_of(arrangement)} == {
                item: arrangement.folder_of(item) for item in hand_added_of(arrangement)
            }, (
                "S5: a hand-added item placed is not in the folder it was placed in, or another "
                "moved"
            )


@given(
    beginning=beginnings,
    placing=st.tuples(
        folders_drawn,
        st.lists(st.integers(0, len(FOLLOWING) - 1), max_size=3),
        st.lists(st.integers(0, HAND_ADDED_KEPT - 1), min_size=1, max_size=4),
    ),
    stranger=st.sampled_from(Stranger),
    position=st.integers(0, 4),
)
@example(
    beginning=[None] * 6,
    placing=(0, [0, 1], [0, 1, 2]),
    stranger=Stranger.ITEM_OF_ANOTHER,
    position=4,
)
@example(
    beginning=[None] * 6,
    placing=(1, [0, 1], [0, 1, 2]),
    stranger=Stranger.SUBSCRIPTION_REMOVED,
    position=2,
)
@EXAMPLES
def test_a_placing_that_names_one_thing_not_the_owners_is_refused_whole_and_changes_nothing(
    subscribing_app: App,
    feeds: FeedStandin,
    beginning: list[int | None],
    placing: tuple[int | None, list[int], list[int]],
    stranger: Stranger,
    position: int,
) -> None:
    app = subscribing_app
    owner = new_owner()
    other = new_owner()
    with as_owner(other):
        strangers = {
            Stranger.FOLDER_OF_ANOTHER: made_folder(app, "Theirs")["id"],
            Stranger.SUBSCRIPTION_OF_ANOTHER: subscription_of(app, feeds),
            Stranger.ITEM_OF_ANOTHER: keep_web_item(app.store, other, link_origin()),
        }
    with as_owner(owner):
        arrangement = library_arranged(app, feeds, owner, FOLDERS_MADE, FOLLOWING, HAND_ADDED_KEPT)
        dissolved = made_folder(app, "Dissolved")["id"]
        removed = subscription_of(app, feeds)
        assert dissolve_folder(app, dissolved).status_code == HTTPStatus.NO_CONTENT, (
            "S4: dissolving a folder of the owner's was refused"
        )
        assert app.client.delete(subscription_path(removed)).status_code == HTTPStatus.NO_CONTENT
        strangers |= {
            Stranger.FOLDER_NEVER_MADE: NOWHERE_FOLDER,
            Stranger.FOLDER_DISSOLVED: dissolved,
            Stranger.SUBSCRIPTION_NEVER_MADE: NOWHERE_SUBSCRIPTION,
            Stranger.SUBSCRIPTION_REMOVED: removed,
            Stranger.ITEM_NEVER_KEPT: NOWHERE_ITEM,
        }
        arrangement = arranged_as(app, arrangement, beginning)
        before = shown_now(app)
        folder, subscriptions, items = named_by(arrangement, placing)
        if stranger in FOLDER_STRANGERS:
            folder = strangers[stranger]
        elif stranger in SUBSCRIPTION_STRANGERS:
            subscriptions.insert(position % (len(subscriptions) + 1), strangers[stranger])
        else:
            items.insert(position % (len(items) + 1), strangers[stranger])
        response = place(app, folder, subscriptions, items)
        assert response.status_code == HTTPStatus.NOT_FOUND, (
            f"S5, S10: a placing that names {stranger} among the owner's own was answered "
            f"{response.status_code}, not 404"
        )
        unchanged(app, before, f"S5: a placing refused for naming {stranger}")


@given(
    beginning=beginnings,
    placing=placings_drawn,
    follower=st.integers(0, sum(FOLLOWING) - 1),
    position=st.integers(0, 5),
)
@example(beginning=[None] * 6, placing=(0, [0, 1], [0, 1, 2, 3]), follower=0, position=5)
@EXAMPLES
def test_a_placing_that_names_an_item_following_its_subscription_is_refused_whole_with_409(
    subscribing_app: App,
    feeds: FeedStandin,
    beginning: list[int | None],
    placing: tuple[int | None, list[int], list[int]],
    follower: int,
    position: int,
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        arrangement = library_arranged(app, feeds, owner, FOLDERS_MADE, FOLLOWING, HAND_ADDED_KEPT)
        arrangement = arranged_as(app, arrangement, beginning)
        before = shown_now(app)
        folder, subscriptions, items = named_by(arrangement, placing)
        items.insert(position % (len(items) + 1), following_of(arrangement)[follower])
        response = place(app, folder, subscriptions, items)
        assert response.status_code == HTTPStatus.CONFLICT, (
            "S5: a placing that names an item following its subscription was answered "
            f"{response.status_code}, not 409"
        )
        assert "subscription" in str(response.json().get("detail", "")).casefold(), (
            "S5: the refusal of an item following its subscription does not say that it is in "
            f"its subscription's folder: {response.text}"
        )
        unchanged(app, before, "S5: a placing refused for an item following its subscription")


def test_a_placing_that_names_nothing_changes_nothing(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        arrangement = library_arranged(app, feeds, owner, FOLDERS_MADE, FOLLOWING, HAND_ADDED_KEPT)
        arrangement = arranged_as(app, arrangement, [0, None, 1, None, 2, 0])
        before = shown_now(app)
        for folder in (None, arrangement.folders[2]):
            response = place(app, folder, [], [])
            assert response.status_code == HTTPStatus.NO_CONTENT, (
                f"S5: a placing that names nothing was answered {response.status_code}, not 204"
            )
            unchanged(app, before, "S5: a placing that names nothing")


# S7: the folder of every item.


@given(
    following=st.lists(st.integers(0, 3), min_size=1, max_size=3),
    hand_added=st.integers(0, 5),
    placings=st.lists(
        st.tuples(
            folders_drawn,
            st.lists(st.integers(0, 2), max_size=3),
            st.lists(st.integers(0, 4), max_size=4),
        ),
        max_size=5,
    ),
)
@example(following=[2, 1], hand_added=3, placings=[(0, [0], [0, 1, 2]), (1, [1, 0], [1])])
@EXAMPLES
def test_an_items_folder_is_its_subscriptions_while_it_follows_it_and_its_own_placements_otherwise(
    subscribing_app: App,
    feeds: FeedStandin,
    following: list[int],
    hand_added: int,
    placings: list[tuple[int | None, list[int], list[int]]],
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        arrangement = library_arranged(app, feeds, owner, FOLDERS_MADE, following, hand_added)
        for folder_at, subscriptions_at, items_at in placings:
            folder, subscriptions, items = named_by(
                arrangement,
                (
                    folder_at,
                    [at % len(following) for at in subscriptions_at],
                    [at % hand_added for at in items_at] if hand_added else [],
                ),
            )
            placed(app, folder, subscriptions, items)
            arrangement = arrangement.placing(folder, subscriptions, items)
            told = {entry["id"]: entry["folder"] for entry in entries(app)}
            assert told == {item: arrangement.folder_of(item) for item in arrangement.origins}, (
                "S7: an item is not in its subscription's folder while it follows it, or not in "
                "the folder of its own placement, or none, otherwise"
            )


def folders_told(told: list[dict[str, Any]]) -> dict[str, str | None]:
    return {entry["id"]: entry["folder"] for entry in told}


def brought_by(app: App, subscription: str) -> list[str]:
    # The items a subscription's postings were admitted as, newest first, once none waits.
    return [
        posting["admission"]["item"]["id"]
        for posting in admissions_ended(app, subscription)
        if admission_of(posting) == ADMITTED
    ]


def test_every_answer_that_carries_an_entry_tells_the_folder_its_item_is_in(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        tag = uuid4().hex
        postings = [page_posting(app, f"{tag}-{number}") for number in range(2)]
        subscription = subscribed(app, served_feed(feeds, tag, postings), admit=2)["id"]
        brought = brought_by(app, subscription)
        assert len(brought) == 2, "the two newest postings of a subscription were not admitted"
        linked = app.read_page(f"{tag}-linked", article(f"Linked {tag}", LEDGER))
        uploaded = app.settled(app.upload("trap.pdf", PDF).json()["id"])["id"]
        beside = keep_web_item(app.store, owner, link_origin())
        papers, podcasts = (made_folder(app, name)["id"] for name in ("Papers", "Podcasts"))
        placed(app, papers, [subscription], [beside])
        placed(app, podcasts, [], [linked])
        expected = {
            brought[0]: papers,
            brought[1]: papers,
            linked: podcasts,
            uploaded: None,
            beside: papers,
        }
        listed_entries = entries(app)
        assert set(folders_told(listed_entries)) == set(expected), (
            "the list of items does not hold the items the judgment kept"
        )
        told: dict[str, list[dict[str, Any]]] = {
            "the list of items": listed_entries,
            "one item": [
                answered(app.client.get(f"{ITEMS}/{item}"), "reading an item") for item in expected
            ],
            "seeing an item": [
                answered(app.client.put(f"{ITEMS}/{item}/seen"), "seeing an item")
                for item in (brought[0], linked)
            ],
            "the entries inside postings": [
                posting["admission"]["item"]
                for posting in every_posting(app, subscription)
                if admission_of(posting) == ADMITTED
            ],
            "a posting read at the reader's word": [
                answered(
                    app.client.post(
                        f"{subscription_path(subscription)}/postings/{posting['number']}/reading"
                    ),
                    "reading a posting",
                )["admission"]["item"]
                for posting in every_posting(app, subscription)
            ],
            "an upload": [answered(app.upload("trap.pdf", PDF), "an upload")],
        }
        app.settled(uploaded)
        told["an import"] = [
            answered(app.client.post(ITEMS, json={"url": page_address(f"{tag}-linked")}), "import")
        ]
        app.settled(linked)
        told["a reading again"] = [
            answered(app.client.post(f"{ITEMS}/{item}/reading"), "a reading again")
            for item in (brought[1], linked)
        ]
        for item in (brought[1], linked):
            app.settled(item)
        for where, entries_told in told.items():
            found = folders_told(entries_told)
            assert found and found == {item: expected[item] for item in found}, (
                f"S7: {where} does not tell the folder each item is in: {found} against {expected}"
            )


def test_an_item_two_subscriptions_list_is_in_the_folder_of_the_one_its_origin_names(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        tag = uuid4().hex
        shared = page_posting(app, f"{tag}-shared")
        bringing = subscribed(app, served_feed(feeds, f"{tag}-a", [shared]), admit=1)["id"]
        [item] = brought_by(app, bringing)
        listing = subscribed(app, served_feed(feeds, f"{tag}-b", [shared]), admit=0)["id"]
        assert brought_by(app, listing) == [item], (
            "a posting whose address the library holds an item of is not listed as that item"
        )
        papers, podcasts = (made_folder(app, name)["id"] for name in ("Papers", "Podcasts"))
        placed(app, papers, [bringing], [])
        placed(app, podcasts, [listing], [])
        told = {
            "the list of items": folders_told(entries(app)),
            "one item": folders_told([answered(app.client.get(f"{ITEMS}/{item}"), "an item")]),
            "the postings of the subscription that brought it": folders_told(
                [posting["admission"]["item"] for posting in every_posting(app, bringing)]
            ),
            "the postings of the subscription that lists it": folders_told(
                [posting["admission"]["item"] for posting in every_posting(app, listing)]
            ),
        }
        for where, found in told.items():
            assert found == {item: papers}, (
                f"S7: {where} does not put an item two subscriptions list in the folder of the "
                f"one its origin names: {found}"
            )
        assert app.client.delete(subscription_path(listing)).status_code == HTTPStatus.NO_CONTENT
        assert folders_told(entries(app)) == {item: papers}, (
            "S7, S9: removing a subscription that lists an item another subscription brought "
            "took it out of the folder of the subscription it follows"
        )
        assert place(app, podcasts, [], [item]).status_code == HTTPStatus.CONFLICT, (
            "S5: an item that still follows the subscription that brought it was placed"
        )


def test_an_item_whose_address_is_pasted_is_hand_added_and_in_its_own_placements_folder(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        tag = uuid4().hex
        posting = page_posting(app, tag)
        subscription = subscribed(app, served_feed(feeds, tag, [posting]), admit=1)["id"]
        [item] = brought_by(app, subscription)
        papers, podcasts = (made_folder(app, name)["id"] for name in ("Papers", "Podcasts"))
        placed(app, papers, [subscription], [])
        assert folders_told(entries(app)) == {item: papers}, (
            "S7: an item that follows its subscription is not in its subscription's folder"
        )
        pasted = answered(app.client.post(ITEMS, json={"url": posting.address}), "an import")
        assert (pasted["origin"]["kind"], pasted["folder"]) == ("link", None), (
            "S7: an item whose address the reader pasted is not a hand-added item in no folder: "
            f"{pasted['origin']} {pasted['folder']}"
        )
        app.settled(item)
        placed(app, podcasts, [], [item])
        assert folders_told(entries(app)) == {item: podcasts}, (
            "S5, S7: a hand-added item once its address was pasted is not where it was placed"
        )
        assert listed(app)[0]["folder"] == papers, "S5: placing an item moved its subscription"


def test_an_item_whose_subscription_is_made_anew_follows_it_whatever_its_own_placement(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        tag = uuid4().hex
        feed = served_feed(feeds, tag, [page_posting(app, tag)])
        subscription = subscribed(app, feed, admit=1)["id"]
        [item] = brought_by(app, subscription)
        papers, podcasts = (made_folder(app, name)["id"] for name in ("Papers", "Podcasts"))
        placed(app, papers, [subscription], [])
        assert app.client.delete(subscription_path(subscription)).status_code == (
            HTTPStatus.NO_CONTENT
        )
        assert folders_told(entries(app)) == {item: papers}, (
            "S9: the item of a subscription removed did not stay in the folder it was in"
        )
        anew = subscribed(app, feed, admit=0)
        assert anew["id"] == subscription, "a subscription made anew to a feed has another id"
        assert anew["folder"] in (None, papers), (
            f"S8: a subscription made anew is in a folder never placed in: {anew['folder']}"
        )
        assert folders_told(entries(app)) == {item: anew["folder"]}, (
            "S7: an item that follows its subscription again, its own placement in a folder, is "
            "not in its subscription's folder"
        )
        placed(app, podcasts, [subscription], [])
        assert folders_told(entries(app)) == {item: podcasts}, (
            "S7: an item that follows its subscription again does not move with it"
        )
        assert place(app, None, [], [item]).status_code == HTTPStatus.CONFLICT, (
            "S5: an item that follows its subscription again was placed by itself"
        )
