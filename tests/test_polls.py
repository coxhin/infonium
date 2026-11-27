from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from http import HTTPStatus
from threading import Event
from typing import Any, Literal
from uuid import uuid4

import httpx
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, Field

from infonium.reasons import failure_reason
from infonium.subscriptions.feed import (
    KEY_CHARACTERS,
    TITLE_CHARACTERS,
    Feed,
    Posting,
    UnreadableFeed,
)
from infonium.subscriptions.fetcher import Unchanged, Validators
from infonium.subscriptions.subscription import MOST_ADMITTED_A_POLL, Follows
from tests.feed_standin import FEEDS_SITE, INTERVAL, FeedFunction, FeedStandin, held_reached
from tests.notebook import NOWHERE_ITEM, instant
from tests.service import App
from tests.subscribing import (
    AWAITED,
    FAILED,
    SETTLED,
    UNADMITTED,
    UNDERWAY,
    admission_of,
    admissions_ended,
    answered,
    answered_going_on,
    every_posting,
    feed_address,
    missing_posting,
    page_address,
    page_posting,
    poll_by_hand,
    polled,
    polled_going_on,
    served_feed,
    stopped_in_time,
    subscribe,
    subscribed,
    subscription_path,
    viewed,
    waited_for,
    watched,
)

# The judgments of S3 to S7 of the subscriptions' brief: what a poll lists and awaits, a poll
# that fails, the schedule, a poll by hand, and how a subscription follows its feed.
EXAMPLES = settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
STEP = timedelta(seconds=1)
DAY = timedelta(days=1)
# An interval after which the waits of polls failed in a row reach a day at the fourth.
LONG_INTERVAL = timedelta(hours=5)
# An interval longer than a day.
OVER_A_DAY = timedelta(hours=30)
texts = st.characters(codec="utf-8", exclude_categories=["Cs"])
posting_keys = st.text(alphabet=texts, min_size=1, max_size=KEY_CHARACTERS)
titles = st.text(alphabet=texts, min_size=1, max_size=TITLE_CHARACTERS)
publication_times = st.none() | st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2040, 1, 1),
    timezones=st.sampled_from([UTC, timezone(timedelta(hours=9))]),
)


class FailureKind(StrEnum):
    UNREADABLE = "unreadable"
    CONNECTION = "connection"
    OTHER = "other"


def failure_of(kind: FailureKind, said: str) -> Exception:
    # A feed that cannot be read, or anything else a feed function raises: an error of httpx,
    # or another.
    match kind:
        case FailureKind.UNREADABLE:
            return UnreadableFeed(said)
        case FailureKind.CONNECTION:
            return httpx.ConnectError(said)
        case FailureKind.OTHER:
            return RuntimeError(said)


class Appearance(BaseModel):
    # A posting as one reading of a feed lists it: its key by its place among the keys of the
    # scenario, and what the feed says of it that time.
    key: int
    title: str
    published: datetime | None


class Served(BaseModel):
    kind: Literal["served"] = "served"
    postings: list[Appearance]


class NotChanged(BaseModel):
    kind: Literal["unchanged"] = "unchanged"


class Failing(BaseModel):
    kind: Literal["failing"] = "failing"
    failure: FailureKind
    said: str


class Step(BaseModel):
    # How the subscription is set to follow its feed before a poll, and what the poll meets.
    follows: Follows
    outcome: Served | NotChanged | Failing = Field(discriminator="kind")


class Scenario(BaseModel):
    keys: list[str]
    first: list[Appearance]
    admit: int
    steps: list[Step]


@st.composite
def scenarios(draw: st.DrawFn, failures: bool) -> Scenario:
    keys = draw(st.lists(posting_keys, unique=True, min_size=1, max_size=24))
    appearances = st.lists(
        st.builds(
            Appearance,
            key=st.integers(0, len(keys) - 1),
            title=titles,
            published=publication_times,
        ),
        max_size=30,
    )
    outcomes: list[st.SearchStrategy[Served | NotChanged | Failing]] = [
        appearances.map(lambda postings: Served(postings=postings)),
        st.just(NotChanged()),
    ]
    if failures:
        outcomes.append(
            st.builds(
                Failing,
                failure=st.sampled_from(FailureKind),
                said=st.text(alphabet=texts, max_size=600),
            )
        )
    steps = st.lists(
        st.builds(Step, follows=st.sampled_from(Follows), outcome=st.one_of(outcomes)),
        min_size=1,
        max_size=6,
    )
    return Scenario(
        keys=keys,
        first=draw(appearances),
        admit=draw(st.integers(0, MOST_ADMITTED_A_POLL)),
        steps=draw(steps),
    )


class ListedPosting(BaseModel):
    # A posting as the subscription should list it: the reading of the feed that first listed
    # its key, and the time it is awaited from, if it is.
    key: int
    title: str
    published: datetime | None
    since: datetime | None


def listed_after(
    listed: list[ListedPosting], appearances: list[Appearance], at: datetime, admits: int
) -> list[ListedPosting]:
    # The postings listed, in the order of their numbers, after a poll at the time given found
    # the feed listing the postings given: each key not listed yet is listed once, as the feed
    # first lists it, the first the feed lists given the highest number, and the newest of them
    # as many as are admitted awaited from the time of the poll.
    known = {posting.key for posting in listed}
    new: list[Appearance] = []
    for appearance in appearances:
        if appearance.key not in known:
            known.add(appearance.key)
            new.append(appearance)
    anew = [
        ListedPosting(
            key=appearance.key,
            title=appearance.title,
            published=appearance.published,
            since=at if place < admits else None,
        )
        for place, appearance in enumerate(new)
    ]
    return [*listed, *reversed(anew)]


def wait_after(failures: int, interval: timedelta) -> timedelta:
    # How long after the n-th poll failed in a row the next is due.
    doubled: int = 2 ** (failures - 1)
    return min(interval * doubled, DAY)


def as_listed(posting: dict[str, Any]) -> tuple[str, str, datetime | None, datetime | None]:
    admission = posting["admission"]
    return (
        posting["address"],
        posting["title"],
        None if posting["published"] is None else instant(posting["published"]),
        instant(admission["since"]) if admission["kind"] == AWAITED else None,
    )


def postings_feed(tag: str, appearances: list[Appearance], keys: list[str]) -> list[Posting]:
    return [
        Posting(
            key=keys[appearance.key],
            title=appearance.title,
            address=page_address(f"{tag}-{appearance.key}"),
            published=appearance.published,
        )
        for appearance in appearances
    ]


# S3 and S4: what each poll lists and awaits, and what a failed one keeps.


def judged_polls(
    app: App,
    feeds: FeedStandin,
    scenario: Scenario,
    settled: Callable[[App, str, datetime], dict[str, Any]],
) -> None:
    # Subscribes to a feed as the scenario has it, polls it once for each of its steps, and
    # judges after each what the subscription and its postings are, with the poll ended as the
    # function given waits for it.
    tag = uuid4().hex
    address = feed_address(tag)
    feeds.serve(
        address,
        Feed(
            title=f"Feed {tag}",
            site=None,
            postings=postings_feed(tag, scenario.first, scenario.keys),
        ),
    )
    validators = feeds.validators_of(address)
    at = feeds.clock.advance(STEP)
    subscription = subscribed(app, address, scenario.admit)["id"]
    listed = listed_after([], scenario.first, at, scenario.admit)
    follows = Follows.READS
    failures = 0
    title, site = f"Feed {tag}", None
    for number, step in enumerate(scenario.steps):
        at = feeds.clock.advance(STEP)
        match step.outcome:
            case Served(postings=appearances):
                title, site = f"Feed {tag} at {at}", f"{FEEDS_SITE}/{tag}/{number}"
                feeds.serve(
                    address,
                    Feed(
                        title=title,
                        site=site,
                        postings=postings_feed(tag, appearances, scenario.keys),
                    ),
                )
            case NotChanged():
                feeds.plan_polls(address, Unchanged())
            case Failing(failure=failure, said=said):
                feeds.plan_polls(address, failure_of(failure, said))
        unpaused = follows == Follows.PAUSED and step.follows != Follows.PAUSED
        if step.follows != follows:
            chosen = app.client.put(
                f"{subscription_path(subscription)}/follows", json={"follows": step.follows}
            )
            assert (
                answered(chosen, "setting how a subscription follows")["follows"] == step.follows
            ), "S7: a subscription set to follow its feed otherwise is not answered so"
        if not unpaused:
            begun = poll_by_hand(app, subscription)
            assert begun["poll"] == {"kind": UNDERWAY, "failures": failures}, (
                "S6: a poll by hand does not answer the subscription with its poll under way and "
                f"the polls failed in a row before it: {begun['poll']}"
            )
        view = settled(app, subscription, at)
        asked = feeds.calls_of(FeedFunction.FETCHED, address)
        assert asked[-1].validators == validators and len(asked) == number + 1, (
            "S3: a poll did not ask fetched_feed for the feed from the validators of the last "
            f"reading of it, once: {asked[-1].validators} for {validators}"
        )
        poll = view["poll"]
        found_before = len(listed)
        match step.outcome:
            case Served(postings=appearances):
                admits = MOST_ADMITTED_A_POLL if step.follows == Follows.READS else 0
                listed = listed_after(listed, appearances, at, admits)
                validators = feeds.validators_of(address)
                failures = 0
                expected_poll: dict[str, Any] = {
                    "kind": SETTLED,
                    "at": at,
                    "found": len(listed) - found_before,
                    "due": at + feeds.interval,
                }
            case NotChanged():
                failures = 0
                expected_poll = {"kind": SETTLED, "at": at, "found": 0, "due": at + feeds.interval}
            case Failing(failure=failure, said=said):
                failures += 1
                expected_poll = {
                    "kind": FAILED,
                    "at": at,
                    "reason": failure_reason(failure_of(failure, said)),
                    "failures": failures,
                    "due": at + wait_after(failures, feeds.interval),
                }
        found_poll = {
            **poll,
            "at": instant(poll["at"]),
            "due": instant(poll["due"]),
        }
        assert found_poll == expected_poll, (
            f"S3, S4: after a poll that met {step.outcome.kind} the poll does not stand as it "
            f"should: {found_poll} for {expected_poll}"
        )
        assert (view["title"], view["site"], view["postings"]) == (title, site, len(listed)), (
            "S3: the subscription's title, site or count of postings does not follow the feed it "
            f"read last: {view}"
        )
        follows = step.follows
        shown = every_posting(app, subscription)
        assert [posting["number"] for posting in shown] == list(range(len(listed), 0, -1)), (
            "S3: the postings listed are not numbered from 1, each once"
        )
        expected = [
            (
                page_address(f"{tag}-{posting.key}"),
                posting.title,
                posting.published,
                posting.since,
            )
            for posting in reversed(listed)
        ]
        assert [as_listed(posting) for posting in shown] == expected, (
            "S3: the postings are not each key listed once, numbered by the order the feed first "
            "listed it and kept as first listed, with the newest new ones, MOST_ADMITTED_A_POLL "
            "at most and none where the subscription only lists or is paused, awaited from the "
            "time of their poll"
        )
        assert all(admission_of(posting) in {AWAITED, UNADMITTED} for posting in shown), (
            "S8: a posting was admitted while an item a subscription brought was being read"
        )


@pytest.mark.parametrize("feeds", [LONG_INTERVAL], indirect=True)
@given(scenario=scenarios(failures=False))
@example(
    scenario=Scenario(
        keys=[f"key {number}" for number in range(24)],
        first=[
            Appearance(key=number, title=f"First {number}", published=None) for number in range(3)
        ],
        admit=2,
        steps=[
            Step(
                follows=Follows.READS,
                outcome=Served(
                    postings=[
                        Appearance(key=number, title=f"Then {number}", published=None)
                        for number in range(24)
                    ]
                ),
            ),
            Step(follows=Follows.PAUSED, outcome=NotChanged()),
            Step(
                follows=Follows.LISTS,
                outcome=Served(postings=[Appearance(key=0, title="Again", published=None)]),
            ),
        ],
    )
)
@EXAMPLES
def test_every_sequence_of_polls_lists_each_key_once_numbered_by_newness_within_the_bound(
    subscribing_app: App, feeds: FeedStandin, held_reading: str, scenario: Scenario
) -> None:
    judged_polls(subscribing_app, feeds, scenario, polled)


@pytest.mark.parametrize("feeds", [LONG_INTERVAL], indirect=True)
@given(scenario=scenarios(failures=True))
@example(
    scenario=Scenario(
        keys=[f"key {number}" for number in range(24)],
        first=[
            Appearance(key=number, title=f"First {number}", published=None) for number in range(3)
        ],
        admit=2,
        steps=[
            Step(
                follows=Follows.READS,
                outcome=Served(
                    postings=[
                        Appearance(key=number, title=f"Then {number}", published=None)
                        for number in range(24)
                    ]
                ),
            ),
            *[
                Step(follows=Follows.READS, outcome=Failing(failure=failure, said=""))
                for failure in (*FailureKind, FailureKind.UNREADABLE)
            ],
            Step(follows=Follows.PAUSED, outcome=NotChanged()),
        ],
    )
)
@EXAMPLES
def test_every_sequence_of_polls_with_failures_keeps_the_postings_and_waits_longer_in_a_row(
    subscribing_app: App, feeds: FeedStandin, held_reading: str, scenario: Scenario
) -> None:
    judged_polls(subscribing_app, feeds, scenario, polled_going_on)


def test_a_feed_whose_every_posting_is_new_awaits_the_newest_ten(
    subscribing_app: App, feeds: FeedStandin, held_reading: str
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, "renamed", [missing_posting("renamed-old")])
    view = subscribed(app, feed, admit=0)
    renamed = [missing_posting(f"renamed-{number}") for number in range(20)]
    feeds.serve(feed, feeds.served[feed].model_copy(update={"postings": renamed}))
    at = feeds.clock.advance(STEP)
    poll_by_hand(app, view["id"])
    assert polled(app, view["id"], at)["poll"]["found"] == 20, (
        "S3: a poll of a feed whose every posting is new did not find them all"
    )
    admissions = [admission_of(posting) for posting in every_posting(app, view["id"])]
    assert admissions == [AWAITED] * MOST_ADMITTED_A_POLL + [UNADMITTED] * 11, (
        "S3: of twenty new postings, the newest MOST_ADMITTED_A_POLL are not awaited and the "
        f"rest unadmitted: {admissions}"
    )


def test_failed_polls_wait_twice_as_long_each_time_until_one_succeeds(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, "failing", [missing_posting("failing-0")])
    view = subscribed(app, feed, admit=0)
    postings = every_posting(app, view["id"])
    due = instant(view["poll"]["due"])
    errors = [
        UnreadableFeed("The feed is not XML"),
        httpx.ConnectTimeout("timed out"),
        KeyError("entries"),
    ]
    feeds.plan_polls(feed, *errors)
    for failures, error in enumerate(errors, start=1):
        feeds.clock.move_to(due - STEP)
        watched(feeds)
        assert feeds.polls_of(feed) == failures - 1, (
            f"S4, S5: a poll was begun before it was due, after {failures - 1} failures"
        )
        at = feeds.clock.move_to(due)
        poll = polled_going_on(app, view["id"], at)["poll"]
        due = at + INTERVAL * 2 ** (failures - 1)
        assert (poll["kind"], poll["reason"], poll["failures"], instant(poll["due"])) == (
            FAILED,
            failure_reason(error),
            failures,
            due,
        ), f"S4: the {failures}th poll failed in a row is not kept as such: {poll}"
        assert every_posting(app, view["id"]) == postings, "S4: a failed poll changed the postings"
    feeds.clock.move_to(due - STEP)
    watched(feeds)
    assert feeds.polls_of(feed) == len(errors), "S4, S5: a poll was begun before it was due"
    feeds.serve(
        feed,
        feeds.served[feed].model_copy(
            update={"postings": [missing_posting("failing-1"), missing_posting("failing-0")]}
        ),
    )
    at = feeds.clock.move_to(due)
    poll = polled_going_on(app, view["id"], at)["poll"]
    assert (poll["kind"], poll["found"], instant(poll["due"])) == (SETTLED, 1, at + INTERVAL), (
        f"S4: a poll that succeeds after failures does not end their row: {poll}"
    )
    assert feeds.calls_of(FeedFunction.FETCHED, feed)[-1].validators == Validators(
        etag='"1"', modified=None
    ), "S4: a failed poll changed the validators of the feed's last reading"
    feeds.plan_polls(feed, UnreadableFeed("Gone"))
    at = feeds.clock.move_to(at + INTERVAL)
    poll = polled_going_on(app, view["id"], at)["poll"]
    assert (poll["failures"], instant(poll["due"])) == (1, at + INTERVAL), (
        f"S4: a poll that failed after one that succeeded is not counted the first in a row: {poll}"
    )


@pytest.mark.parametrize("feeds", [OVER_A_DAY], indirect=True)
def test_an_interval_longer_than_a_day_waits_a_day_after_a_failure(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, "slow", [missing_posting("slow-0")])
    view = subscribed(app, feed, admit=0)
    assert instant(view["poll"]["due"]) - instant(view["poll"]["at"]) == OVER_A_DAY, (
        "S1: a new subscription's next poll is not due one interval after it"
    )
    feeds.plan_polls(feed, UnreadableFeed("Gone for now"))
    at = feeds.clock.advance(STEP)
    poll_by_hand(app, view["id"])
    poll = polled_going_on(app, view["id"], at)["poll"]
    assert instant(poll["due"]) == at + DAY, (
        "S4: a failed poll of a subscription polled less often than daily waits more than a "
        f"day: {poll}"
    )
    at = feeds.clock.advance(STEP)
    poll_by_hand(app, view["id"])
    poll = polled_going_on(app, view["id"], at)["poll"]
    assert instant(poll["due"]) == at + OVER_A_DAY, (
        f"S3: a poll that settles does not fall due one interval after it: {poll}"
    )


# S5: the schedule.


def test_a_subscription_is_polled_once_its_poll_is_due_and_not_before(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, "scheduled", [missing_posting("scheduled-0")])
    view = subscribed(app, feed, admit=0)
    due = instant(view["poll"]["due"])
    for _ in range(2):
        feeds.clock.move_to(due - STEP)
        polls = feeds.polls_of(feed)
        watched(feeds)
        assert feeds.polls_of(feed) == polls, "S5: a subscription was polled before it was due"
        at = feeds.clock.move_to(due + STEP)
        poll = polled(app, view["id"], at)["poll"]
        assert (poll["kind"], instant(poll["due"])) == (SETTLED, at + INTERVAL), (
            f"S5: a subscription whose poll was due was not polled by the schedule: {poll}"
        )
        due = instant(poll["due"])


def test_no_more_than_two_polls_run_at_once(subscribing_app: App, feeds: FeedStandin) -> None:
    app = subscribing_app
    addresses = [served_feed(feeds, f"crowd-{number}", []) for number in range(5)]
    views = [subscribed(app, address, admit=0) for address in addresses]
    holds = [feeds.hold_poll(address) for address in addresses]
    at = feeds.clock.advance(INTERVAL)
    waited_for(feeds.running_now, lambda running: running >= 2, "two polls under way")
    watched(feeds)
    by_hand = served_feed(feeds, "crowd-by-hand", [])
    hand_view = subscribed(app, by_hand, admit=0)
    hand_hold = feeds.hold_poll(by_hand)
    assert poll_by_hand(app, hand_view["id"])["poll"]["kind"] == UNDERWAY, (
        "S6: a poll by hand is not answered under way"
    )
    watched(feeds)
    assert feeds.running_now() == 2, (
        f"S5: {feeds.running_now()} polls run at once where five were due and one asked by hand"
    )
    for hold in [*holds, hand_hold]:
        hold.release()
    for view in views:
        polled(app, view["id"], at)
    waited_for(
        lambda: viewed(app, hand_view["id"])["poll"]["kind"],
        lambda kind: kind != UNDERWAY,
        "the poll by hand",
    )
    assert feeds.most_running <= 2, f"S5: {feeds.most_running} polls ran at once"


def test_a_poll_under_way_is_the_only_one_of_its_subscription_by_hand_or_by_schedule(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, "single", [missing_posting("single-0")])
    view = subscribed(app, feed, admit=0)
    hold = feeds.hold_poll(feed)
    feeds.clock.advance(STEP)
    first = poll_by_hand(app, view["id"])
    held_reached(hold, "the poll by hand")
    second = poll_by_hand(app, view["id"])
    feeds.clock.advance(INTERVAL)
    watched(feeds)
    third = poll_by_hand(app, view["id"])
    assert [answer["poll"]["kind"] for answer in (first, second, third)] == [UNDERWAY] * 3, (
        "S6: a poll by hand while one is under way is not answered with that one under way"
    )
    assert feeds.polls_of(feed) == 1, (
        "S5, S6: a subscription was polled again while a poll of it was under way"
    )
    hold.release()
    polled(app, view["id"], feeds.clock.time())
    watched(feeds)
    assert feeds.most_running_of[feed] == 1, "S5: two polls of one subscription ran at once"


def test_what_is_due_is_kept_over_a_restart(
    start_app: Callable[..., App], feeds: FeedStandin
) -> None:
    app = start_app(poller=feeds.poller)
    early = served_feed(feeds, "early", [missing_posting("early-0")])
    late = served_feed(feeds, "late", [missing_posting("late-0")])
    early_view = subscribed(app, early, admit=0)
    feeds.clock.advance(INTERVAL / 2)
    late_view = subscribed(app, late, admit=0)
    app.stop()
    at = feeds.clock.move_to(instant(early_view["poll"]["due"]) + STEP)
    again = start_app(poller=feeds.poller)
    polled(again, early_view["id"], at)
    watched(feeds)
    assert feeds.polls_of(late) == 0, (
        "S5: a poll not yet due when the service started again was not left for its time"
    )
    at = feeds.clock.move_to(instant(late_view["poll"]["due"]))
    polled(again, late_view["id"], at)


def failed_at_once(app: App, feeds: FeedStandin, subscription: str, failures: int) -> None:
    # A poll whose feed fails at once, by hand or by the schedule, and what the reader asks
    # after it.
    error = UnreadableFeed(f"Failure {failures}")
    feed = feeds.calls_of(FeedFunction.DISCOVERED)[0].address
    feeds.plan_polls(feed, error)
    if failures % 2:
        at = feeds.clock.advance(STEP)
        poll_by_hand(app, subscription)
    else:
        at = feeds.clock.move_to(instant(viewed(app, subscription)["poll"]["due"]))
    poll = polled_going_on(app, subscription, at)["poll"]
    assert (poll["kind"], poll["reason"], poll["failures"]) == (
        FAILED,
        failure_reason(error),
        failures,
    ), f"S4: a poll whose feed failed at once is not kept as failed: {poll}"
    answered_going_on(app, lambda: app.client.get("/api/subscriptions"), "listing")
    other = served_feed(feeds, f"at-once-other-{failures}", [])
    answered_going_on(app, lambda: subscribe(app, other, 0), "subscribing to another feed")


def test_polls_whose_feed_fails_at_once_are_kept_failed_and_the_poller_goes_on(
    start_app: Callable[..., App], feeds: FeedStandin
) -> None:
    app = start_app(poller=feeds.poller)
    feed = served_feed(feeds, "at-once", [missing_posting("at-once-0")])
    subscription = subscribed(app, feed, admit=0)["id"]
    for failures in range(1, 5):
        failed_at_once(app, feeds, subscription, failures)
        stopped_in_time(app, "after polls that failed at once")
        app = start_app(poller=feeds.poller)


# S6: a poll by hand.


@pytest.mark.parametrize("follows", list(Follows))
def test_a_poll_by_hand_begins_now_whenever_the_next_was_due(
    subscribing_app: App, feeds: FeedStandin, follows: Follows
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, f"by-hand-{follows}", [missing_posting(f"by-hand-{follows}-0")])
    view = subscribed(app, feed, admit=0)
    answered(
        app.client.put(f"{subscription_path(view['id'])}/follows", json={"follows": follows}),
        "setting how a subscription follows",
    )
    feeds.serve(
        feed,
        feeds.served[feed].model_copy(
            update={
                "postings": [missing_posting(f"by-hand-{follows}-1"), *feeds.served[feed].postings]
            }
        ),
    )
    at = feeds.clock.advance(STEP)
    begun = poll_by_hand(app, view["id"])
    assert begun["poll"] == {"kind": UNDERWAY, "failures": 0}, (
        f"S6: a poll by hand of a subscription that {follows} is not answered under way: {begun}"
    )
    poll = polled(app, view["id"], at)["poll"]
    assert (poll["kind"], poll["found"]) == (SETTLED, 1), (
        f"S6: a poll by hand of a subscription that {follows}, not due, did not poll it: {poll}"
    )
    assert (
        app.client.post(f"{subscription_path(NOWHERE_ITEM)}/poll").status_code
        == HTTPStatus.NOT_FOUND
    ), "S6: a poll by hand of a subscription that is not there was not refused"


def test_a_poll_by_hand_tells_the_polls_failed_in_a_row_before_it(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, "row", [missing_posting("row-0")])
    view = subscribed(app, feed, admit=0)
    feeds.plan_polls(feed, UnreadableFeed("one"), UnreadableFeed("two"))
    for _ in range(2):
        at = feeds.clock.advance(STEP)
        poll_by_hand(app, view["id"])
        polled_going_on(app, view["id"], at)
    hold = feeds.hold_poll(feed)
    begun = poll_by_hand(app, view["id"])
    held_reached(hold, "the third poll")
    assert begun["poll"] == {"kind": UNDERWAY, "failures": 2}, (
        f"S6: a poll under way does not tell the polls that failed in a row before it: {begun}"
    )
    assert viewed(app, view["id"])["poll"] == begun["poll"], (
        "S6: a subscription read while its poll is under way does not show the poll under way"
    )
    hold.release()


# S7: how a subscription follows its feed.


@pytest.mark.parametrize("follows", [Follows.READS, Follows.LISTS])
def test_a_subscription_unpaused_is_polled_at_once(
    subscribing_app: App, feeds: FeedStandin, follows: Follows
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, f"unpaused-{follows}", [missing_posting(f"unpaused-{follows}-0")])
    view = subscribed(app, feed, admit=0)
    path = f"{subscription_path(view['id'])}/follows"
    paused = answered(app.client.put(path, json={"follows": Follows.PAUSED}), "pausing")
    assert paused["follows"] == Follows.PAUSED, f"S7: a subscription paused is not: {paused}"
    at = feeds.clock.advance(STEP)
    resumed = answered(app.client.put(path, json={"follows": follows}), "unpausing")
    assert resumed["follows"] == follows, f"S7: a subscription unpaused is not: {resumed}"
    polled(app, view["id"], at)
    assert feeds.polls_of(feed) == 1, "S7: a subscription unpaused was not polled at once"


def test_a_paused_subscription_is_not_polled_by_the_schedule(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, "paused", [missing_posting("paused-0")])
    view = subscribed(app, feed, admit=0)
    answered(
        app.client.put(
            f"{subscription_path(view['id'])}/follows", json={"follows": Follows.PAUSED}
        ),
        "pausing",
    )
    feeds.clock.advance(3 * INTERVAL)
    watched(feeds)
    assert feeds.polls_of(feed) == 0, "S7: a paused subscription was polled by the schedule"
    assert viewed(app, view["id"])["poll"] == view["poll"], (
        "S7: a paused subscription's poll changed though it was not polled"
    )


def test_a_paused_subscription_keeps_its_postings_awaited_and_still_admits_them(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    app.site.held["paused-held"] = Event()
    postings = [page_posting(app, "paused-held"), page_posting(app, "paused-awaited")]
    view = subscribed(app, served_feed(feeds, "paused-awaits", postings), admit=2)
    waited_for(lambda: app.site.hits["/page/paused-held"], lambda hits: hits >= 1, "the reading")
    answered(
        app.client.put(
            f"{subscription_path(view['id'])}/follows", json={"follows": Follows.PAUSED}
        ),
        "pausing",
    )
    awaited = [admission_of(posting) for posting in every_posting(app, view["id"])]
    assert awaited[1] == AWAITED, f"S7: a paused subscription's awaited posting is not: {awaited}"
    app.site.let_go()
    admitted = [admission_of(posting) for posting in admissions_ended(app, view["id"])]
    assert admitted == ["admitted", "admitted"], (
        f"S7: a paused subscription's awaited postings were not admitted: {admitted}"
    )


@pytest.mark.parametrize("choice", [{"follows": "polls"}, {"follows": None}, {}])
def test_a_way_of_following_that_is_none_is_refused(
    subscribing_app: App, feeds: FeedStandin, choice: dict[str, Any]
) -> None:
    view = subscribed(subscribing_app, served_feed(feeds, "choices", []), admit=0)
    response = subscribing_app.client.put(f"{subscription_path(view['id'])}/follows", json=choice)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, (
        f"S7: a way of following that is none ({choice}) was not refused: {response.status_code}"
    )
    assert viewed(subscribing_app, view["id"]) == view, "S7: a refused choice changed something"
