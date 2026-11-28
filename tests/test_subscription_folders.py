from http import HTTPStatus
from typing import Any
from uuid import uuid4

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.library import Link
from tests.feed_standin import FeedStandin
from tests.foldering import (
    Arrangement,
    keep_web_item,
    made_folder,
    placed,
    shown_now,
    subscribed_origin,
)
from tests.service import App, as_owner, new_owner
from tests.subscribing import (
    ADMITTED,
    admission_of,
    admissions_ended,
    answered,
    listed,
    missing_posting,
    page_address,
    page_posting,
    served_feed,
    subscribed,
    subscription_path,
    viewed,
)

# The judgments of S8 and S9 of the service's brief of the sidebar, through the service's
# endpoints: the folder that every answer tells of a subscription, and where the items of a
# subscription go when it is removed.
EXAMPLES = settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
FOLDERS_MADE = 3
MOST_SUBSCRIPTIONS = 3
folders_drawn = st.none() | st.integers(0, FOLDERS_MADE - 1)


def folders_of(views: list[dict[str, Any]]) -> dict[str, str | None]:
    return {view["id"]: view["folder"] for view in views}


# S8: the folder of every subscription.


def test_every_answer_that_carries_a_subscription_tells_the_folder_of_its_placement(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    """S8: subscriptions just made are in no folder; the list of subscriptions, a subscription,
    subscribing to its feed again, setting how it follows, a poll by hand and seeing it each
    tell the folder of each subscription's placement, before and after the two
    subscriptions' placements are swapped."""
    app = subscribing_app
    with as_owner(new_owner()):
        tag = uuid4().hex
        addresses = [
            served_feed(feeds, f"{tag}-{number}", [missing_posting(f"{tag}-{number}")])
            for number in range(2)
        ]
        made = [subscribed(app, address, admit=0) for address in addresses]
        assert folders_of(made) == dict.fromkeys(view["id"] for view in made), (
            f"S8: a subscription just made is in a folder: {folders_of(made)}"
        )
        papers = made_folder(app, "Papers")["id"]
        first, second = (view["id"] for view in made)
        for expected in ({first: papers, second: None}, {first: None, second: papers}):
            placed(app, papers, [kept for kept, folder in expected.items() if folder], [])
            placed(app, None, [kept for kept, folder in expected.items() if not folder], [])
            told: dict[str, list[dict[str, Any]]] = {
                "the list of subscriptions": listed(app),
                "one subscription": [viewed(app, subscription) for subscription in expected],
                "subscribing to its feed again": [
                    subscribed(app, address, admit=0) for address in addresses
                ],
                "setting how it follows": [
                    answered(
                        app.client.put(
                            f"{subscription_path(subscription)}/follows", json={"follows": "lists"}
                        ),
                        "setting how a subscription follows its feed",
                    )
                    for subscription in expected
                ],
                "polling it by hand": [
                    answered(
                        app.client.post(f"{subscription_path(subscription)}/poll"), "a poll by hand"
                    )
                    for subscription in expected
                ],
                "seeing it": [
                    answered(
                        app.client.put(f"{subscription_path(subscription)}/seen"),
                        "seeing a subscription",
                    )
                    for subscription in expected
                ],
            }
            for where, views in told.items():
                assert folders_of(views) == expected, (
                    f"S8: {where} does not tell the folder of each subscription's placement: "
                    f"{folders_of(views)} against {expected}"
                )


# S9: a subscription removed.


class Plan(BaseModel):
    # A subscription of a drawn library: the folder it is placed in, by its place among the
    # owner's, or none; how many of its postings it reads; whether an item that follows it is
    # listed by none of its postings; whether it lists a hand-added item, which is placed in
    # the first folder; whether it is removed and made anew before the judgment, and then
    # placed in the folder of made_anew_in; and where its items are placed once it is removed.
    placed_in: int | None
    brought: int
    unlisted: bool
    lists_hand_added: bool
    made_anew: bool
    made_anew_in: int | None
    items_placed_in: int | None


plans = st.builds(
    Plan,
    placed_in=folders_drawn,
    brought=st.integers(0, 2),
    unlisted=st.booleans(),
    lists_hand_added=st.booleans(),
    made_anew=st.booleans(),
    made_anew_in=folders_drawn,
    items_placed_in=folders_drawn,
)


def brought_items(app: App, subscription: str) -> list[str]:
    return [
        posting["admission"]["item"]["id"]
        for posting in admissions_ended(app, subscription)
        if admission_of(posting) == ADMITTED
    ]


def removed(app: App, arrangement: Arrangement, subscription: str) -> Arrangement:
    # The subscription removed, once it is shown in the folder the rules have it in; where the
    # rules then have everything.
    assert viewed(app, subscription)["folder"] == arrangement.subscriptions[subscription], (
        "S8: a subscription is not shown in the folder of its placement"
    )
    response = app.client.delete(subscription_path(subscription))
    assert response.status_code == HTTPStatus.NO_CONTENT, (
        f"removing a subscription was answered {response.status_code}: {response.text[:300]}"
    )
    return arrangement.removing(subscription)


@given(
    drawn=st.lists(plans, min_size=1, max_size=MOST_SUBSCRIPTIONS),
    order=st.permutations(range(MOST_SUBSCRIPTIONS)),
)
@example(
    drawn=[
        Plan(
            placed_in=0,
            brought=1,
            unlisted=True,
            lists_hand_added=True,
            made_anew=True,
            made_anew_in=None,
            items_placed_in=1,
        ),
        Plan(
            placed_in=None,
            brought=1,
            unlisted=False,
            lists_hand_added=False,
            made_anew=True,
            made_anew_in=None,
            items_placed_in=2,
        ),
    ],
    order=[1, 0, 2],
)
@EXAMPLES
def test_the_items_whose_origin_names_a_removed_subscription_stay_in_the_folder_it_was_in(
    subscribing_app: App, feeds: FeedStandin, drawn: list[Plan], order: list[int]
) -> None:
    """S9: in drawn libraries of subscriptions in folders or in none, with items they read,
    items that follow them that no posting lists and hand-added items their postings list,
    some removed and made anew first, removing each in a drawn order leaves every item whose
    origin names it in the folder it was in, or in none, and everything else where it was;
    those items are then placed like any other."""
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        folders = [made_folder(app, f"Folder {number}")["id"] for number in range(FOLDERS_MADE)]
        arrangement = Arrangement(folders=folders, subscriptions={}, origins={}, own={})
        planned: dict[str, Plan] = {}
        for plan in drawn:
            tag = uuid4().hex
            postings = [page_posting(app, f"{tag}-{number}") for number in range(plan.brought)]
            if plan.lists_hand_added:
                hand_added = keep_web_item(
                    app.store, owner, Link(url=page_address(f"{tag}-hand-added"))
                )
                arrangement.origins[hand_added] = None
                postings.append(missing_posting(f"{tag}-hand-added"))
            feed = served_feed(feeds, tag, postings)
            subscription = subscribed(app, feed, admit=plan.brought)["id"]
            planned[subscription] = plan
            arrangement.subscriptions[subscription] = None
            listed_items = brought_items(app, subscription)
            assert len(listed_items) == len(postings), (
                "a subscription did not admit the postings it reads and those of items the "
                "library holds"
            )
            for item in listed_items:
                arrangement.origins.setdefault(item, subscription)
            if plan.unlisted:
                arrangement.origins[
                    keep_web_item(app.store, owner, subscribed_origin(subscription))
                ] = subscription
            arrangement = placed_as(app, arrangement, plan.placed_in, [subscription], [])
            if plan.lists_hand_added:
                arrangement = placed_as(app, arrangement, 0, [], [hand_added])
            if plan.made_anew:
                arrangement = removed(app, arrangement, subscription)
                anew = subscribed(app, feed, admit=0)
                assert anew["id"] == subscription, "a subscription made anew has another id"
                assert anew["folder"] in (None, *folders), (
                    f"S8, S12: a subscription made anew is in a folder that is not there: {anew}"
                )
                arrangement.subscriptions[subscription] = anew["folder"]
                arrangement = placed_as(app, arrangement, plan.made_anew_in, [subscription], [])
        assert shown_now(app) == arrangement.shown(), (
            "S5, S7, S8, S9: before the subscriptions are removed, things are not where the rules "
            "have them"
        )
        made = list(planned)
        for at in order:
            if at >= len(made):
                continue
            arrangement = removed(app, arrangement, made[at])
            assert shown_now(app) == arrangement.shown(), (
                "S9: once a subscription is removed, an item whose origin names it is not in the "
                "folder it was in, or in none where it was in none, or something else moved"
            )
        for subscription, plan in planned.items():
            items = [item for item, named in arrangement.origins.items() if named == subscription]
            arrangement = placed_as(app, arrangement, plan.items_placed_in, [], items)
        assert shown_now(app) == arrangement.shown(), (
            "S5, S9: the items of a removed subscription are not placed like any other"
        )


def placed_as(
    app: App,
    arrangement: Arrangement,
    folder_at: int | None,
    subscriptions: list[str],
    items: list[str],
) -> Arrangement:
    folder = None if folder_at is None else arrangement.folders[folder_at]
    placed(app, folder, subscriptions, items)
    return arrangement.placing(folder, subscriptions, items)
