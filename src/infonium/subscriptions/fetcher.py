from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from infonium.document import Tagged
from infonium.subscriptions.feed import Feed


class Validators(BaseModel):
    # Where a subscription's fetcher stopped: what the server said of the feed it last sent, by
    # which it can say the next time that the feed has not changed since.
    etag: str | None = None
    modified: str | None = None


class FetchedKind(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class Changed(Tagged):
    # The feed as it is served now, and where the fetcher stops with it.
    kind: Literal[FetchedKind.CHANGED] = FetchedKind.CHANGED
    feed: Feed
    validators: Validators


class Unchanged(Tagged):
    # The server says that the feed is as it was where the fetcher stopped.
    kind: Literal[FetchedKind.UNCHANGED] = FetchedKind.UNCHANGED


Fetched = Annotated[Changed | Unchanged, Field(discriminator="kind")]


def fetched_feed(address: str, validators: Validators) -> Fetched:
    # One poll of a feed: it is asked for from where the fetcher stopped, and read where the
    # server sends it. This is the shape of a fetcher: what it is set to and where it stopped
    # go in, what its source lists now and where it stops come out.
    raise NotImplementedError
