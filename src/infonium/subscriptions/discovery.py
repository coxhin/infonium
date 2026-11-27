from pydantic import BaseModel

from infonium.subscriptions.feed import WebAddress
from infonium.subscriptions.fetcher import Changed

# How an address begins that names a route of RSSHub, a bridge that makes feeds of sites
# without one; the route follows.
RSSHUB_SCHEME = "rsshub://"


class Discovered(BaseModel):
    # The feed an address leads to: where it is served, and the feed as it was fetched there
    # in finding it.
    address: WebAddress
    fetched: Changed


def discovered_feed(address: str, rsshub: str) -> Discovered:
    # The feed that an address a reader gave leads to: the feed itself, a page that names its
    # feed, a show or a channel whose site keeps its feed elsewhere, or a route of the RSSHub
    # instance at the address given.
    raise NotImplementedError
