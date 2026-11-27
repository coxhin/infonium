from typing import Annotated

from pydantic import AwareDatetime, BaseModel, StringConstraints

# The most characters kept of a posting's key, of a title, and of an address. A feed is another
# site's text: what goes beyond is cut, replaced or left out, as read_feed says.
KEY_CHARACTERS = 200
TITLE_CHARACTERS = 300
ADDRESS_CHARACTERS = 2000
WEB_ADDRESS_PATTERN = r"^(?i:https?)://\S+$"

PostingKey = Annotated[str, StringConstraints(min_length=1, max_length=KEY_CHARACTERS)]
Title = Annotated[str, StringConstraints(min_length=1, max_length=TITLE_CHARACTERS)]
WebAddress = Annotated[
    str, StringConstraints(pattern=WEB_ADDRESS_PATTERN, max_length=ADDRESS_CHARACTERS)
]


class Posting(BaseModel):
    # One thing a feed lists for reading: an article, an episode, a video. The key tells it from
    # the feed's other postings and is the same each time the feed is read. The address is what
    # is fetched and read when the posting is admitted to a library: its recording where it
    # carries one, its page otherwise.
    key: PostingKey
    title: Title
    address: WebAddress
    published: AwareDatetime | None


class Feed(BaseModel):
    # What a feed holds when it is read: what it calls itself, the site it says it belongs to,
    # and its postings in the order it lists them, which is the newest first in most feeds.
    title: Title
    site: WebAddress | None
    postings: list[Posting]


class UnreadableFeed(Exception):
    # An address that gives no feed to read, and why, in words a reader is shown.
    pass


def read_feed(content: bytes, declared: str, address: str) -> Feed:
    # The feed that the bytes served at an address hold, declared as the kind given (the
    # Content-Type of the answer, or nothing).
    raise NotImplementedError
