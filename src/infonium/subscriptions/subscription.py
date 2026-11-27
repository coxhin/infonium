import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

import sqlalchemy
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints
from sqlalchemy.dialects import sqlite

from infonium.document import Tagged
from infonium.library import (
    SEEN_FORMAT,
    AcrossOwners,
    AttemptKind,
    NotInLibrary,
    OriginKind,
    OwnerTransaction,
    ReadingKind,
    SeenItem,
    attempt_kind,
    field_path,
    item_identity,
    item_table,
    kept_document_id,
    media_type_table,
    metadata,
    origin_kind,
    reading_kind,
    replacing_insert,
    seen_item,
    seen_table,
    timestamp_text,
)
from infonium.subscriptions.discovery import ROUTE_ADDRESS_PATTERN, RSSHUB_SCHEME
from infonium.subscriptions.feed import (
    ADDRESS_CHARACTERS,
    WEB_ADDRESS_PATTERN,
    Feed,
    Posting,
    UnreadableFeed,
)
from infonium.subscriptions.fetcher import Validators

# The most postings one poll admits to the library by itself, the newest ones; the others it
# finds are listed, for the reader to admit. A feed that renames all its postings at once
# looks, to a poll, like a feed of new postings.
MOST_ADMITTED_A_POLL = 10
# How many of its newest postings a new subscription admits unless its reader says otherwise.
ADMITTED_AT_FIRST = 3
# Stored with every body, as the library's tables do.
SUBSCRIPTION_FORMAT = 1
POSTING_FORMAT = 1
# The longest a feed whose polls fail waits for its next poll, however many failed in a row.
LONGEST_WAIT_AFTER_FAILURES = timedelta(days=1)
# What browsers and sites write before a feed's address: feed://host/path, or feed: and the
# whole address, as feed:https://host/path.
FEED_PREFIX = re.compile(r"^(?i:feed):")
# An address that names how it is reached, as https://, rsshub:// and mailto: do; one that
# names none is reached over HTTPS. A host followed by its port, as example.com:8080, names none,
# and neither does an address that begins with // and its host.
NAMED_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)")
ASSUMED_SCHEME = "https://"
HOST_FOLLOWS = "//"
# Where a web address's scheme ends, what ends the part that names its user, its host and its
# port, and what parts the host from a user before it and from a port after it.
SCHEME_END = "://"
AUTHORITY_END = re.compile(r"[/?#]")
USER_END = "@"
PORT_START = ":"
NO_FEED_ADDRESS = (
    "{address} is neither a web address (http:// or https://) nor a route of RSSHub"
    f" ({RSSHUB_SCHEME}…)"
)
# Times that a query compares are kept as whole microseconds since this instant, which order
# as the times do whatever their zone.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MICROSECOND = timedelta(microseconds=1)

PostingNumber = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "The place of a posting among the postings of its subscription, counted from 1 in"
            " the order the subscription came to list them: of two postings, the one with the"
            " higher number is the newer."
        ),
    ),
]


class Follows(StrEnum):
    # How a subscription follows its feed: it polls it and admits what is new, polls it and
    # only lists what is new, or does not poll it.
    READS = "reads"
    LISTS = "lists"
    PAUSED = "paused"


class SubscriptionDraft(BaseModel):
    # What a reader subscribes to: an address, of a feed, of a page, a show or a channel that
    # leads to one, or of a route of RSSHub (rsshub://), and how many of the newest postings
    # the feed lists now are admitted to the library at once. The others are listed.
    address: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=ADDRESS_CHARACTERS)
    ]
    admit: Annotated[int, Field(ge=0, le=MOST_ADMITTED_A_POLL)] = ADMITTED_AT_FIRST


class FollowsChoice(BaseModel):
    follows: Follows


class PollKind(StrEnum):
    UNDERWAY = "underway"
    SETTLED = "settled"
    FAILED = "failed"


class PollUnderway(Tagged):
    # A poll waits its turn or runs. The failures are the polls that failed in a row before it.
    kind: Literal[PollKind.UNDERWAY] = PollKind.UNDERWAY
    failures: Annotated[int, Field(ge=0)]


class PollSettled(Tagged):
    # The last poll read the feed: when, how many postings it found that the subscription had
    # not listed, and when the next poll is due.
    kind: Literal[PollKind.SETTLED] = PollKind.SETTLED
    at: AwareDatetime
    found: Annotated[int, Field(ge=0)]
    due: AwareDatetime


class PollFailed(Tagged):
    # The last poll failed: when, why, how many polls have failed in a row with it, and when
    # the next is due, which is later the more have failed.
    kind: Literal[PollKind.FAILED] = PollKind.FAILED
    at: AwareDatetime
    reason: str
    failures: Annotated[int, Field(ge=1)]
    due: AwareDatetime


# How the latest poll of a subscription stands. A subscription that is paused keeps the time
# its next poll was due, and is not polled then.
Poll = Annotated[PollUnderway | PollSettled | PollFailed, Field(discriminator="kind")]


class AdmissionKind(StrEnum):
    UNADMITTED = "unadmitted"
    AWAITED = "awaited"
    ADMITTED = "admitted"


class Unadmitted(Tagged):
    # The posting is listed, and nothing of it is in the library.
    kind: Literal[AdmissionKind.UNADMITTED] = AdmissionKind.UNADMITTED


class Awaited(Tagged):
    # The posting waits to be handed to the library, since the time given: the postings that
    # subscriptions bring are read one at a time.
    kind: Literal[AdmissionKind.AWAITED] = AdmissionKind.AWAITED
    since: AwareDatetime


class Admitted(Tagged):
    # The posting was handed to the library as the item named. How its reading stands is the
    # item's to tell.
    kind: Literal[AdmissionKind.ADMITTED] = AdmissionKind.ADMITTED
    item: str


# What has come of admitting a posting to the library.
Admission = Annotated[Unadmitted | Awaited | Admitted, Field(discriminator="kind")]

# The subscriptions of every owner. The column scheduled says when the schedule is to poll a
# subscription next, and is empty while it is paused or a poll of it is under way.
subscription_table = sqlalchemy.Table(
    "subscription",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column("scheduled", sqlalchemy.Integer, index=True),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)
# The postings that each subscription lists, by their numbers, each key of its feed once. The
# columns beside the body say, of a posting that waits to be admitted, since when, and of one
# that was admitted, as which item.
posting_table = sqlalchemy.Table(
    "posting",
    metadata,
    sqlalchemy.Column(
        "subscription_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(subscription_table.c.id),
        primary_key=True,
    ),
    sqlalchemy.Column("number", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("key", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("awaited", sqlalchemy.Integer),
    sqlalchemy.Column("item_id", sqlalchemy.String, sqlalchemy.ForeignKey(item_table.c.id)),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
    sqlalchemy.UniqueConstraint("subscription_id", "key"),
    # A subscription's postings counted, and its admitted ones found, from the index alone.
    sqlalchemy.Index("posting_item", "subscription_id", "owner", "item_id"),
)
# The postings that wait, in the order they are admitted.
sqlalchemy.Index("posting_awaited", posting_table.c.awaited, posting_table.c.number.desc())


class Subscription(BaseModel):
    # A subscription as it is kept: the address its reader gave, the feed that address led to,
    # what the feed calls itself and the site it says it belongs to, when it was added, how it
    # follows the feed, how its latest poll stands, and where its fetcher stopped.
    id: str
    address: str
    feed: str
    title: str
    site: str | None
    added: AwareDatetime
    follows: Follows
    poll: Poll
    validators: Validators


class CountedSubscription(BaseModel):
    # A subscription, with how many postings it lists and how many of them are items that a
    # subscription brought, that are read, and that their reader has not seen.
    subscription: Subscription
    postings: int
    unseen: int


class ListedPosting(BaseModel):
    # A posting as its subscription lists it: its number, the posting as the feed listed it
    # when it was first found, and what has come of admitting it.
    number: PostingNumber
    posting: Posting
    admission: Admission


class HeldItem(Tagged):
    # The posting was admitted as this item, as the library holds it now.
    kind: Literal[AdmissionKind.ADMITTED] = AdmissionKind.ADMITTED
    item: SeenItem


class ShownPosting(BaseModel):
    # A posting as a reader is shown it: one that was admitted, with its item.
    number: PostingNumber
    posting: Posting
    admission: Annotated[Unadmitted | Awaited | HeldItem, Field(discriminator="kind")]


class OwnedSubscription(BaseModel):
    # A subscription of any owner's, as the schedule finds it.
    owner: str
    subscription: Subscription


class AwaitedPosting(BaseModel):
    # A posting of any owner's that waits to be admitted.
    owner: str
    subscription_id: str
    number: PostingNumber


def missing_subscription(subscription_id: str) -> NotInLibrary:
    return NotInLibrary(f"subscription {subscription_id} is not in the library")


def missing_posting(subscription_id: str, number: int) -> NotInLibrary:
    return NotInLibrary(f"subscription {subscription_id} lists no posting {number}")


def instant_order(instant: datetime) -> int:
    return (instant - EPOCH) // MICROSECOND


def named_host(web_address: str) -> str:
    # The host a web address names, without the user it may name before it and the port after
    # it; nothing where it names none, as https:///path, https://:8080/path and
    # https://user@/path do.
    authority = AUTHORITY_END.split(web_address.partition(SCHEME_END)[2], maxsplit=1)[0]
    return authority.rpartition(USER_END)[2].partition(PORT_START)[0]


def subscribed_address(address: str) -> str:
    # The address that is looked into for a feed, for the one a reader gave: the web address
    # that one written for a feed (feed:) stands for, and reached over HTTPS where it names no
    # scheme. One that is then neither a web address that names its host nor a route of RSSHub
    # leads to no feed, and nothing is fetched.
    unwrapped = FEED_PREFIX.sub("", address, count=1)
    named = (
        unwrapped
        if NAMED_SCHEME.match(unwrapped)
        else f"{ASSUMED_SCHEME}{unwrapped.removeprefix(HOST_FOLLOWS)}"
    )
    is_web_address = bool(re.fullmatch(WEB_ADDRESS_PATTERN, named)) and bool(named_host(named))
    # A route's scheme is told in any case, as the discovery tells it.
    if not is_web_address and not re.fullmatch(ROUTE_ADDRESS_PATTERN, named, re.IGNORECASE):
        raise UnreadableFeed(NO_FEED_ADDRESS.format(address=address))
    return named


def first_of_each_key(feed: Feed) -> list[Posting]:
    # The postings of a feed in its order, each key where the feed first lists it: a feed is
    # another site's text, and may list a key twice.
    firsts: dict[str, Posting] = {}
    for posting in feed.postings:
        firsts.setdefault(posting.key, posting)
    return list(firsts.values())


def admission_anew(
    posting: Posting, place: int, awaited: int, since: datetime, held: Mapping[str, str]
) -> Admission:
    # What has come of a posting when its subscription comes to list it, at its place among
    # the new ones, the newest first: one whose address the library holds an item of already
    # is admitted as that item, and nothing is read for it; of the others, the first as many
    # as are awaited wait.
    if posting.key in held:
        return Admitted(item=held[posting.key])
    return Awaited(since=since) if place < awaited else Unadmitted()


def listed_anew(
    postings: list[Posting], highest: int, awaited: int, since: datetime, held: Mapping[str, str]
) -> list[ListedPosting]:
    # Postings that a subscription comes to list, in the order the feed lists them, numbered on
    # from the highest number it lists so that the first the feed lists gets the highest. The
    # newest of them, as many as are awaited, wait to be admitted from the time given, but for
    # those the library holds already, by their keys, which are admitted as the items it holds.
    return [
        ListedPosting(
            number=highest + len(postings) - place,
            posting=posting,
            admission=admission_anew(posting, place, awaited, since, held),
        )
        for place, posting in enumerate(postings)
    ]


def failures_in_a_row(poll: Poll) -> int:
    # How many polls have failed in a row, up to the latest that ended.
    match poll:
        case PollFailed(failures=failures) | PollUnderway(failures=failures):
            return failures
        case _:
            return 0


def wait_after_failures(interval: timedelta, failures: int) -> timedelta:
    # The next poll is due one interval after the first failure in a row, two after the
    # second, four after the third and so on, a day at most.
    wait = min(interval, LONGEST_WAIT_AFTER_FAILURES)
    for _ in range(failures - 1):
        wait = min(wait * 2, LONGEST_WAIT_AFTER_FAILURES)
    return wait


def poll_failed(underway: Poll, at: datetime, reason: str, interval: timedelta) -> PollFailed:
    # The poll under way failed, one more in a row.
    failures = failures_in_a_row(underway) + 1
    return PollFailed(
        at=at, reason=reason, failures=failures, due=at + wait_after_failures(interval, failures)
    )


def followed(subscription: Subscription, follows: Follows, now: datetime) -> Subscription:
    # A subscription set to follow its feed as given. One that was paused and is set to poll
    # has its next poll due at once, unless one is under way.
    unpaused = subscription.follows is Follows.PAUSED and follows is not Follows.PAUSED
    match subscription.poll:
        case PollSettled() | PollFailed() as poll if unpaused:
            due_now = poll.model_copy(update={"due": now})
            return subscription.model_copy(update={"follows": follows, "poll": due_now})
        case _:
            return subscription.model_copy(update={"follows": follows})


def scheduled(subscription: Subscription) -> int | None:
    # When the schedule is to poll the subscription next: when its next poll is due, unless it
    # is paused or a poll of it is under way.
    match subscription.poll:
        case PollSettled(due=due) | PollFailed(due=due) if (
            subscription.follows is not Follows.PAUSED
        ):
            return instant_order(due)
        case _:
            return None


def subscription_row(
    subscription: Subscription, owner: str
) -> dict[sqlalchemy.Column[Any], str | int | None]:
    return {
        subscription_table.c.id: subscription.id,
        subscription_table.c.owner: owner,
        subscription_table.c.scheduled: scheduled(subscription),
        subscription_table.c.format: SUBSCRIPTION_FORMAT,
        subscription_table.c.body: subscription.model_dump_json(),
    }


def posting_row(
    subscription_id: str, owner: str, listed: ListedPosting
) -> dict[str, str | int | None]:
    admission = listed.admission
    return {
        posting_table.c.subscription_id.name: subscription_id,
        posting_table.c.number.name: listed.number,
        posting_table.c.owner.name: owner,
        posting_table.c.key.name: listed.posting.key,
        posting_table.c.awaited.name: (
            instant_order(admission.since) if isinstance(admission, Awaited) else None
        ),
        posting_table.c.item_id.name: admission.item if isinstance(admission, Admitted) else None,
        posting_table.c.format.name: POSTING_FORMAT,
        posting_table.c.body.name: listed.model_dump_json(),
    }


def shown_posting(body: str, item: str | None, seen: str | None, noted: str | None) -> ShownPosting:
    # A posting as it is kept, and, of one that was admitted, its item as it is kept with when
    # it was seen and the media type noted of the document it holds.
    listed = ListedPosting.model_validate_json(body)
    match listed.admission:
        case Admitted() if item is not None:
            admission: Unadmitted | Awaited | HeldItem = HeldItem(item=seen_item(item, seen, noted))
        case Admitted(item=item_id):
            raise NotInLibrary(f"item {item_id}, which a posting was admitted as, is missing")
        case Unadmitted() | Awaited() as waiting:
            admission = waiting
    return ShownPosting(number=listed.number, posting=listed.posting, admission=admission)


poll_kind = sqlalchemy.func.json_extract(
    subscription_table.c.body, field_path((Subscription, "poll"), (PollUnderway, "kind"))
)


class Subscriptions:
    # The subscriptions of one owner and the postings they list, read and written in one
    # transaction.
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.connection = transaction.connection
        self.owner = transaction.owner

    def sought(self, subscription_id: str) -> list[Subscription]:
        # The subscription of this id when the owner has it, and nothing when they do not.
        bodies = self.connection.execute(
            sqlalchemy.select(subscription_table.c.body).where(
                subscription_table.c.owner == self.owner,
                subscription_table.c.id == subscription_id,
            )
        ).scalars()
        return [Subscription.model_validate_json(body) for body in bodies]

    def subscription(self, subscription_id: str) -> Subscription:
        found = self.sought(subscription_id)
        if not found:
            raise missing_subscription(subscription_id)
        return found[0]

    def keep(self, subscription: Subscription) -> None:
        self.connection.execute(
            replacing_insert(subscription_table, subscription_row(subscription, self.owner))
        )

    def every_counted(self, *where: sqlalchemy.ColumnElement[bool]) -> list[CountedSubscription]:
        # The owner's subscriptions, or those that the conditions hold of, with their counts,
        # the one added last first, in one query.
        listed = (
            sqlalchemy.select(sqlalchemy.func.count())
            .where(
                posting_table.c.owner == self.owner,
                posting_table.c.subscription_id == subscription_table.c.id,
            )
            .scalar_subquery()
        )
        unseen = (
            sqlalchemy.select(sqlalchemy.func.count())
            .select_from(posting_table)
            .join(item_table, item_table.c.id == posting_table.c.item_id)
            .outerjoin(seen_table, seen_table.c.item_id == posting_table.c.item_id)
            .where(
                posting_table.c.owner == self.owner,
                posting_table.c.subscription_id == subscription_table.c.id,
                posting_table.c.item_id.is_not(None),
                item_table.c.owner == self.owner,
                origin_kind == OriginKind.SUBSCRIBED,
                reading_kind == ReadingKind.READ,
                seen_table.c.item_id.is_(None),
            )
            .scalar_subquery()
        )
        rows = self.connection.execute(
            sqlalchemy.select(subscription_table.c.body, listed, unseen).where(
                subscription_table.c.owner == self.owner, *where
            )
        ).all()
        counted = [
            CountedSubscription(
                subscription=Subscription.model_validate_json(body), postings=postings, unseen=new
            )
            for body, postings, new in rows
        ]
        return sorted(counted, key=lambda each: each.subscription.added, reverse=True)

    def counted(self, subscription_id: str) -> CountedSubscription:
        found = self.every_counted(subscription_table.c.id == subscription_id)
        if not found:
            raise missing_subscription(subscription_id)
        return found[0]

    def of_subscription(self, subscription_id: str) -> list[sqlalchemy.ColumnElement[bool]]:
        return [
            posting_table.c.owner == self.owner,
            posting_table.c.subscription_id == subscription_id,
        ]

    def list_feed(self, subscription_id: str, feed: Feed, awaited: int, since: datetime) -> int:
        # The feed's postings that the subscription does not list yet are listed, the newest of
        # them, as many as are awaited, waiting to be admitted from the time given; how many
        # they are. One whose address the library holds an item of is listed as that item. A
        # posting listed already stays as it is, whatever the feed says of it now.
        listed = self.of_subscription(subscription_id)
        known = set(
            self.connection.execute(sqlalchemy.select(posting_table.c.key).where(*listed)).scalars()
        )
        highest = self.connection.execute(
            sqlalchemy.select(
                sqlalchemy.func.coalesce(sqlalchemy.func.max(posting_table.c.number), 0)
            ).where(*listed)
        ).scalar_one()
        new = [posting for posting in first_of_each_key(feed) if posting.key not in known]
        if new:
            # The item each new posting would be, by its address, and which of those the
            # library holds: a posting of something the reader has already is that item.
            items = {posting.key: item_identity(self.owner, posting.address) for posting in new}
            in_library = set(
                self.connection.execute(
                    sqlalchemy.select(item_table.c.id).where(
                        item_table.c.owner == self.owner, item_table.c.id.in_(items.values())
                    )
                ).scalars()
            )
            held = {key: item for key, item in items.items() if item in in_library}
            self.connection.execute(
                sqlalchemy.insert(posting_table),
                [
                    posting_row(subscription_id, self.owner, posting)
                    for posting in listed_anew(new, highest, awaited, since, held)
                ],
            )
        return len(new)

    def listed_posting(self, subscription_id: str, number: int) -> ListedPosting:
        body = self.connection.execute(
            sqlalchemy.select(posting_table.c.body).where(
                *self.of_subscription(subscription_id), posting_table.c.number == number
            )
        ).scalar_one_or_none()
        if body is None:
            raise missing_posting(subscription_id, number)
        return ListedPosting.model_validate_json(body)

    def keep_admission(self, subscription_id: str, listed: ListedPosting) -> None:
        row = posting_row(subscription_id, self.owner, listed)
        changed = (
            posting_table.c.awaited,
            posting_table.c.item_id,
            posting_table.c.format,
            posting_table.c.body,
        )
        self.connection.execute(
            sqlalchemy.update(posting_table)
            .where(*self.of_subscription(subscription_id), posting_table.c.number == listed.number)
            .values({column: row[column.name] for column in changed})
        )

    def shown_postings(
        self, subscription_id: str, limit: int, *where: sqlalchemy.ColumnElement[bool]
    ) -> list[ShownPosting]:
        # The postings that the conditions hold of, the newest first, with the items of those
        # that were admitted, in one query.
        rows = self.connection.execute(
            sqlalchemy.select(
                posting_table.c.body,
                item_table.c.body,
                seen_table.c.seen,
                media_type_table.c.media_type,
            )
            .select_from(posting_table)
            .outerjoin(
                item_table,
                (item_table.c.id == posting_table.c.item_id) & (item_table.c.owner == self.owner),
            )
            .outerjoin(seen_table, seen_table.c.item_id == posting_table.c.item_id)
            .outerjoin(
                media_type_table,
                (media_type_table.c.owner == self.owner)
                & (media_type_table.c.document_id == kept_document_id),
            )
            .where(*self.of_subscription(subscription_id), *where)
            .order_by(posting_table.c.number.desc())
            .limit(limit)
        ).all()
        return [shown_posting(body, item, seen, noted) for body, item, seen, noted in rows]

    def page(self, subscription_id: str, before: int | None, limit: int) -> list[ShownPosting]:
        # The newest postings, or the newest of those below the number given.
        self.subscription(subscription_id)
        older = [] if before is None else [posting_table.c.number < before]
        return self.shown_postings(subscription_id, limit, *older)

    def shown(self, subscription_id: str, number: int) -> ShownPosting:
        found = self.shown_postings(subscription_id, 1, posting_table.c.number == number)
        if not found:
            raise missing_posting(subscription_id, number)
        return found[0]

    def see(self, subscription_id: str, at: datetime) -> None:
        # Every item of the subscription's postings that is read and not seen is seen now.
        self.subscription(subscription_id)
        read_items = (
            sqlalchemy.select(
                posting_table.c.item_id,
                sqlalchemy.literal(self.owner),
                sqlalchemy.literal(timestamp_text(at)),
                sqlalchemy.literal(SEEN_FORMAT),
            )
            .join(item_table, item_table.c.id == posting_table.c.item_id)
            .where(
                *self.of_subscription(subscription_id),
                posting_table.c.item_id.is_not(None),
                item_table.c.owner == self.owner,
                reading_kind == ReadingKind.READ,
            )
        )
        self.connection.execute(
            sqlite.insert(seen_table)
            .from_select(
                [seen_table.c.item_id, seen_table.c.owner, seen_table.c.seen, seen_table.c.format],
                read_items,
            )
            .on_conflict_do_nothing(index_elements=[seen_table.c.item_id])
        )

    def remove(self, subscription_id: str) -> None:
        # The subscription goes with the postings it lists; the items they brought stay. It is
        # removed only after `Folders.release`, in the same transaction: the library's file
        # refuses to remove a subscription that has its placement, and only `release` takes the
        # placements of their own from the items it brought where it is in no folder.
        self.connection.execute(
            sqlalchemy.delete(posting_table).where(*self.of_subscription(subscription_id))
        )
        removed = self.connection.execute(
            sqlalchemy.delete(subscription_table).where(
                subscription_table.c.owner == self.owner,
                subscription_table.c.id == subscription_id,
            )
        )
        if removed.rowcount == 0:
            raise missing_subscription(subscription_id)


def owned_subscriptions(
    every_owner: AcrossOwners, *where: sqlalchemy.ColumnElement[bool]
) -> list[OwnedSubscription]:
    rows = every_owner.rows(
        sqlalchemy.select(subscription_table.c.owner, subscription_table.c.body).where(*where)
    )
    return [
        OwnedSubscription(owner=owner, subscription=Subscription.model_validate_json(body))
        for owner, body in rows
    ]


def due_subscriptions(every_owner: AcrossOwners, now: datetime) -> list[OwnedSubscription]:
    # Every owner's subscriptions that the schedule is to poll by the time given.
    return owned_subscriptions(every_owner, subscription_table.c.scheduled <= instant_order(now))


def underway_polls(every_owner: AcrossOwners) -> list[OwnedSubscription]:
    # Every owner's subscriptions whose poll is under way.
    return owned_subscriptions(every_owner, poll_kind == PollKind.UNDERWAY)


def longest_awaited(every_owner: AcrossOwners) -> list[AwaitedPosting]:
    # The posting of all owners' that has waited longest to be admitted, and of those that
    # have waited as long the one of the higher number; nothing while none waits.
    rows = every_owner.rows(
        sqlalchemy.select(
            posting_table.c.owner, posting_table.c.subscription_id, posting_table.c.number
        )
        .where(posting_table.c.awaited.is_not(None))
        .order_by(
            posting_table.c.awaited, posting_table.c.number.desc(), posting_table.c.subscription_id
        )
        .limit(1)
    )
    return [
        AwaitedPosting(owner=owner, subscription_id=subscription_id, number=number)
        for owner, subscription_id, number in rows
    ]


def is_subscribed_reading_underway(every_owner: AcrossOwners) -> bool:
    # Whether an item of anyone's that a subscription brought is being read.
    reading = every_owner.rows(
        sqlalchemy.select(
            sqlalchemy.exists().where(
                origin_kind == OriginKind.SUBSCRIBED, attempt_kind == AttemptKind.UNDERWAY
            )
        )
    )
    return bool(reading[0][0])
