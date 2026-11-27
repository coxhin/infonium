from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

from infonium.document import Tagged
from infonium.subscriptions.feed import ADDRESS_CHARACTERS

# The most postings one poll admits to the library by itself, the newest ones; the others it
# finds are listed, for the reader to admit. A feed that renames all its postings at once
# looks, to a poll, like a feed of new postings.
MOST_ADMITTED_A_POLL = 10
# How many of its newest postings a new subscription admits unless its reader says otherwise.
ADMITTED_AT_FIRST = 3

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
