import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event, Lock

import httpx
import sqlalchemy
from pydantic import AwareDatetime, BaseModel

from infonium.intake import Intake
from infonium.library import (
    Failed,
    Library,
    LibraryStore,
    Subscribed,
    is_busy,
    item_identity,
)
from infonium.reasons import failure_reason
from infonium.subscriptions.discovery import Discovered
from infonium.subscriptions.feed import UnreadableFeed
from infonium.subscriptions.fetcher import Changed, Fetched, Unchanged, Validators
from infonium.subscriptions.subscription import (
    MOST_ADMITTED_A_POLL,
    Admitted,
    Awaited,
    CountedSubscription,
    Follows,
    PollSettled,
    PollUnderway,
    ShownPosting,
    Subscription,
    SubscriptionDraft,
    Subscriptions,
    Unadmitted,
    due_subscriptions,
    failures_in_a_row,
    first_of_each_key,
    followed,
    is_subscribed_reading_underway,
    longest_awaited,
    poll_failed,
    subscribed_address,
    underway_polls,
)

# How many polls run at once; the others wait their turn.
POLLS_AT_ONCE = 2
# The failures of a poll that lie with the feed or the way to it, not with infonium.
FEED_FAILURES = (UnreadableFeed, httpx.HTTPError)
SCHEDULE_STOPPED = (
    "the schedule of subscriptions stopped on a fault, and no feed is polled and no posting"
    " admitted until infonium starts again: {reason}"
)

logger = logging.getLogger(__name__)


class ScheduleStopped(Exception):
    # The poller's watch stopped on a fault of infonium's own. Every operation on subscriptions
    # is refused with it until infonium starts again, so that a reader is told where they look,
    # not shown polls that were due long ago. The cause is kept as the cause.
    pass


class BegunPoll(BaseModel):
    # A poll begun of an owner's subscription, as the subscription was when it was added: one
    # that was removed meanwhile, and maybe added again, is not this poll's to settle.
    owner: str
    subscription_id: str
    added: AwareDatetime


def record_fault(finished: Future[None]) -> None:
    # A poll keeps its own failure; what still ends one is a fault in keeping it. This runs in
    # whichever thread finishes or registers last, which may hold the turn, so it takes none.
    error = None if finished.cancelled() else finished.exception()
    if error is not None:
        logger.error("a poll's failure could not be kept", exc_info=error)


def polled(subscriptions: Subscriptions, begun: BegunPoll) -> list[Subscription]:
    # The subscription a poll was begun of, while that poll is under way; nothing once it was
    # removed.
    return [
        subscription
        for subscription in subscriptions.sought(begun.subscription_id)
        if subscription.added == begun.added and isinstance(subscription.poll, PollUnderway)
    ]


class Poller:
    # Keeps the subscriptions of every owner of a library: subscribes to the feeds that readers
    # give, polls each subscription's feed when it is due, a few at a time and off the thread
    # that asks, and hands the postings that wait to the intake one at a time. The two feed
    # functions are given: the service gives it those of the subscriptions package.
    # Our own loop: the due times must live in the library's file, which a job store would copy.
    def __init__(
        self,
        store: LibraryStore,
        intake: Intake,
        discovered: Callable[[str, str], Discovered],
        fetched: Callable[[str, Validators], Fetched],
        rsshub: str,
        clock: Callable[[], datetime],
        interval: timedelta,
        watch: timedelta,
    ) -> None:
        self.store = store
        self.intake = intake
        self.discovered = discovered
        self.fetched = fetched
        self.rsshub = rsshub
        self.clock = clock
        self.interval = interval
        self.watch_interval = watch
        self.pool = ThreadPoolExecutor(max_workers=POLLS_AT_ONCE, thread_name_prefix="poller")
        self.watching = ThreadPoolExecutor(max_workers=1, thread_name_prefix="poll-watch")
        # A subscription and the postings it lists are changed by one thread at a time. So a
        # poll is begun once whoever asks for it, a posting is admitted once, and a poll of a
        # subscription that was removed keeps nothing. The turn is taken only where an
        # operation begins, in a thread that holds nothing: what runs with it held never takes
        # it, and no callback of a future takes it.
        self.turn = Lock()
        self.stopped = Event()
        # No watch runs until the poller starts.
        self.watcher: Future[None] = Future()

    def start(self) -> None:
        # Polls that the service's stopping cut off begin again, and the watch begins.
        with self.store.every_owner() as connection:
            cut_off = underway_polls(connection)
        for owned in cut_off:
            self.poll_in_background(owned.owner, owned.subscription)
        self.watcher = self.watching.submit(self.watch)
        self.watcher.add_done_callback(self.record_watch)

    def raise_failure(self) -> None:
        # A watch that stopped on a fault fails every operation on subscriptions after it, as
        # the index's worker fails every search.
        if self.watcher.done() and not self.watcher.cancelled():
            failure = self.watcher.exception()
            if failure is not None:
                raise ScheduleStopped(
                    SCHEDULE_STOPPED.format(reason=failure_reason(failure))
                ) from failure

    def close(self) -> None:
        # The watch ends its round first, so that it hands no posting to an intake that closes
        # after it. A poll that waits its turn stays under way in the library, and begins again
        # when the service starts again.
        self.stopped.set()
        self.watching.shutdown(wait=True, cancel_futures=True)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def record_watch(self, finished: Future[None]) -> None:
        # Takes no lock: it may run in a thread that holds the turn.
        error = None if finished.cancelled() else finished.exception()
        if error is not None:
            logger.error(
                "the schedule of subscriptions stopped; every operation on subscriptions is"
                " refused until infonium starts again",
                exc_info=error,
            )

    def watch(self) -> None:
        # At each watch the polls that are due begin, and the posting that has waited longest
        # is admitted. A library file that another writer holds past the wait leaves the
        # watch's work for the next.
        while not self.stopped.is_set():
            try:
                self.begin_due_polls()
                self.admit_next()
            except sqlalchemy.exc.OperationalError as error:
                if not is_busy(error):
                    raise
                logger.warning(
                    "the library's file is busy, so subscriptions wait a watch: %s", error
                )
            self.stopped.wait(self.watch_interval.total_seconds())

    def begin_due_polls(self) -> None:
        with self.turn:
            with self.store.every_owner() as connection:
                due = due_subscriptions(connection, self.clock())
            for owned in due:
                self.begin_poll(owned.owner, owned.subscription)

    def begin_poll(self, owner: str, subscription: Subscription) -> None:
        # Called with the turn held, of a subscription whose poll is not under way.
        underway = PollUnderway(failures=failures_in_a_row(subscription.poll))
        with self.store.transaction(owner) as transaction:
            Subscriptions(transaction).keep(subscription.model_copy(update={"poll": underway}))
        self.poll_in_background(owner, subscription)

    def poll_in_background(self, owner: str, subscription: Subscription) -> None:
        begun = BegunPoll(owner=owner, subscription_id=subscription.id, added=subscription.added)
        finished = self.pool.submit(self.poll, begun)
        finished.add_done_callback(record_fault)

    def poll(self, begun: BegunPoll) -> None:
        # The feed is asked for from where the fetcher stopped, unless the subscription was
        # removed before the poll had its turn. A poll that fails keeps its failure itself, in
        # the poller's own thread, which holds nothing when it begins.
        with self.store.transaction(begun.owner) as transaction:
            asked = polled(Subscriptions(transaction), begun)
        for subscription in asked:
            try:
                self.settle(begun, self.fetched(subscription.feed, subscription.validators))
            except Exception as failure:
                self.keep_failure(begun, failure)
        if not asked:
            logger.info("subscription %s was removed before its poll began", begun.subscription_id)

    def settle(self, begun: BegunPoll, fetched: Fetched) -> None:
        # What is new in the feed is listed, and where the subscription reads its feed the
        # newest of that waits to be admitted; the subscription takes what the feed says of
        # itself, and the poll settles. A subscription removed meanwhile keeps nothing.
        with self.turn, self.store.transaction(begun.owner) as transaction:
            subscriptions = Subscriptions(transaction)
            settling = polled(subscriptions, begun)
            for subscription in settling:
                at = self.clock()
                match fetched:
                    case Unchanged():
                        found, following = 0, subscription
                    case Changed(feed=feed, validators=validators):
                        admitted = (
                            MOST_ADMITTED_A_POLL if subscription.follows is Follows.READS else 0
                        )
                        found = subscriptions.list_feed(subscription.id, feed, admitted, at)
                        following = subscription.model_copy(
                            update={
                                "title": feed.title,
                                "site": feed.site,
                                "validators": validators,
                            }
                        )
                settled = PollSettled(at=at, found=found, due=at + self.interval)
                subscriptions.keep(following.model_copy(update={"poll": settled}))
                logger.info("polling %s found %d new postings", subscription.feed, found)
        if not settling:
            logger.info("subscription %s was removed while it was polled", begun.subscription_id)

    def keep_failure(self, begun: BegunPoll, error: Exception) -> None:
        # A poll that failed is kept as failed, unless its subscription was removed meanwhile.
        with self.turn, self.store.transaction(begun.owner) as transaction:
            subscriptions = Subscriptions(transaction)
            failing = polled(subscriptions, begun)
            for subscription in failing:
                failed = poll_failed(
                    subscription.poll, self.clock(), failure_reason(error), self.interval
                )
                subscriptions.keep(subscription.model_copy(update={"poll": failed}))
        for subscription in failing:
            if isinstance(error, FEED_FAILURES):
                logger.warning("polling %s failed: %s", subscription.feed, failure_reason(error))
            else:
                logger.error("polling %s failed", subscription.feed, exc_info=error)
        if not failing:
            logger.info("subscription %s was removed while it was polled", begun.subscription_id)

    def admit_next(self) -> None:
        # The posting that has waited longest is admitted, unless an item that a subscription
        # brought is being read: so the postings that subscriptions bring keep one reader at
        # most, and what a reader adds by hand does not wait behind them.
        with self.turn:
            with self.store.every_owner() as connection:
                waiting = longest_awaited(connection)
                if waiting and is_subscribed_reading_underway(connection):
                    return
            for posting in waiting:
                self.admit(posting.owner, posting.subscription_id, posting.number)

    def admit(self, owner: str, subscription_id: str, number: int) -> None:
        # Called with the turn held. A posting whose address the owner's library holds an item
        # of already is admitted as that item, whose origin and reading stay as they are; any
        # other begins to be read as an item that the subscription brought, called what the
        # posting is called.
        with self.store.transaction(owner) as transaction:
            listed = Subscriptions(transaction).listed_posting(subscription_id, number)
            held = Library(transaction).sought(item_identity(owner, listed.posting.address))
        posting = listed.posting
        item = (
            held[0]
            if held
            else self.intake.begin_address(
                owner,
                Subscribed(url=posting.address, called=posting.title, subscription=subscription_id),
            )
        )
        admitted = listed.model_copy(update={"admission": Admitted(item=item.id)})
        with self.store.transaction(owner) as transaction:
            Subscriptions(transaction).keep_admission(subscription_id, admitted)
        logger.info(
            "posting %d of subscription %s is admitted as item %s", number, subscription_id, item.id
        )

    # The operations a reader asks for answer the subscription or the posting as the operation
    # left it, read with the turn still held, before a poll it began can settle.

    def subscribe(self, owner: str, draft: SubscriptionDraft) -> CountedSubscription:
        # The feed the address leads to is found and read, and a subscription to it kept that
        # lists every posting the feed holds, the newest of them, as many as the draft says,
        # waiting to be admitted. A feed the owner is subscribed to already is that
        # subscription, unchanged and not polled.
        discovered = self.discovered(subscribed_address(draft.address), self.rsshub)
        feed = discovered.fetched.feed
        # The id follows from the owner and the feed's address, as an item's does from its own.
        subscription_id = item_identity(owner, discovered.address)
        with self.turn, self.store.transaction(owner) as transaction:
            subscriptions = Subscriptions(transaction)
            if not subscriptions.sought(subscription_id):
                now = self.clock()
                subscriptions.keep(
                    Subscription(
                        id=subscription_id,
                        address=draft.address,
                        feed=discovered.address,
                        title=feed.title,
                        site=feed.site,
                        added=now,
                        follows=Follows.READS,
                        poll=PollSettled(
                            at=now, found=len(first_of_each_key(feed)), due=now + self.interval
                        ),
                        validators=discovered.fetched.validators,
                    )
                )
                subscriptions.list_feed(subscription_id, feed, draft.admit, now)
            return subscriptions.counted(subscription_id)

    def poll_now(self, owner: str, subscription_id: str) -> CountedSubscription:
        # A poll begins now, whenever the next was due and though the subscription be paused,
        # unless one is under way.
        with self.turn:
            with self.store.transaction(owner) as transaction:
                subscription = Subscriptions(transaction).subscription(subscription_id)
            if not isinstance(subscription.poll, PollUnderway):
                self.begin_poll(owner, subscription)
            with self.store.transaction(owner) as transaction:
                return Subscriptions(transaction).counted(subscription_id)

    def follow(self, owner: str, subscription_id: str, follows: Follows) -> CountedSubscription:
        with self.turn, self.store.transaction(owner) as transaction:
            subscriptions = Subscriptions(transaction)
            subscription = subscriptions.subscription(subscription_id)
            subscriptions.keep(followed(subscription, follows, self.clock()))
            return subscriptions.counted(subscription_id)

    def unsubscribe(self, owner: str, subscription_id: str) -> None:
        with self.turn, self.store.transaction(owner) as transaction:
            Subscriptions(transaction).remove(subscription_id)

    def read_posting(self, owner: str, subscription_id: str, number: int) -> ShownPosting:
        # A posting is read at the reader's word: one that is not admitted, or waits, is
        # admitted now without waiting for its turn, and one whose item's reading failed is read
        # again with the origin the item has. One whose item is being read, or is read, is left
        # as it stands.
        with self.turn:
            with self.store.transaction(owner) as transaction:
                admission = (
                    Subscriptions(transaction).listed_posting(subscription_id, number).admission
                )
                failed = isinstance(admission, Admitted) and isinstance(
                    Library(transaction).item(admission.item).reading.attempt, Failed
                )
            match admission:
                case Unadmitted() | Awaited():
                    self.admit(owner, subscription_id, number)
                case Admitted(item=item_id) if failed:
                    self.intake.begin_again(owner, item_id)
            with self.store.transaction(owner) as transaction:
                return Subscriptions(transaction).shown(subscription_id, number)
