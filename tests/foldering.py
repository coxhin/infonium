from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.api import Media
from infonium.document import (
    HTML_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    RECORDING_MEDIA_TYPES,
    Document,
    Paragraph,
    document_of,
)
from infonium.folder import FOLDER_NAME_LENGTH
from infonium.library import (
    Item,
    LibraryStore,
    Link,
    Origin,
    Read,
    Reading,
    Settled,
    Subscribed,
    Upload,
    item_identity,
)
from tests.feed_standin import FeedStandin
from tests.service import SITE, App
from tests.subscribing import answered, listed, missing_posting, served_feed, subscribed

# What the judgments of folders and placements do through the service's endpoints, the
# libraries they make for it through the library's store, and where the sidebar's rules put
# each subscription and item of an owner's.
FOLDERS = "/api/folders"
PLACEMENTS = "/api/placements"
ITEMS = "/api/items"
# A folder's id that no owner's folder has.
NOWHERE_FOLDER = "d" * 64
# What a made-up document was read by, and the snapshot it names, which no judgment asks for.
EXTRACTOR = "made-up/1"
SNAPSHOT = "0" * 64
MADE_UP_TEXT = "The ferry's ledger was kept in the harbour office of Quillmoor."
# The characters that the white space around a folder's name is drawn from: those that count as
# white space both to Python and to Unicode, by their code points.
WHITE_SPACE = "".join(
    map(
        chr,
        (
            *range(0x09, 0x0E),
            0x20,
            0x85,
            0xA0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
        ),
    )
)
paddings = st.text(alphabet=WHITE_SPACE, max_size=3)
name_characters = st.characters(codec="utf-8", exclude_categories=["Cs"])
# A name's first and last character, which nothing counts as white space.
edge_characters = name_characters.filter(lambda character: not character.isspace())


def names_of(least: int, most: int) -> st.SearchStrategy[str]:
    # Names of least to most characters, at least one, of any character but a surrogate, that
    # neither begin nor end with white space.
    middles = st.text(alphabet=name_characters, min_size=max(least - 2, 0), max_size=most - 2)
    longer = st.builds(
        lambda first, middle, last: first + middle + last, edge_characters, middles, edge_characters
    )
    return longer if least > 1 else st.one_of(edge_characters, longer)


kept_names = names_of(1, FOLDER_NAME_LENGTH)
long_names = names_of(FOLDER_NAME_LENGTH + 1, FOLDER_NAME_LENGTH + 30)


def media_by_rule(media_type: str) -> Media | None:
    # What an item is read as by the media type of the file that tells it: HTML is a web
    # article, a PDF is a PDF, sound or film is a recording, and anything else is no media.
    if media_type == HTML_MEDIA_TYPE:
        return Media.WEB
    if media_type == PDF_MEDIA_TYPE:
        return Media.PDF
    return Media.RECORDING if media_type.startswith(RECORDING_MEDIA_TYPES) else None


def folder_path(folder_id: str) -> str:
    return f"{FOLDERS}/{folder_id}"


def make_folder(app: App, name: str) -> httpx2.Response:
    return app.client.post(FOLDERS, json={"name": name})


def made_folder(app: App, name: str) -> dict[str, Any]:
    # A folder a judgment needs made; it fails here when the folder is not made.
    response = make_folder(app, name)
    if response.status_code != HTTPStatus.OK:
        pytest.fail(
            f"S2: making a folder named {name!r} that a judgment needs was refused: "
            f"{response.status_code} {response.text[:300]}"
        )
    view: dict[str, Any] = response.json()
    return view


def rename_folder(app: App, folder_id: str, name: str) -> httpx2.Response:
    return app.client.patch(folder_path(folder_id), json={"name": name})


def dissolve_folder(app: App, folder_id: str) -> httpx2.Response:
    return app.client.delete(folder_path(folder_id))


def folders_listed(app: App) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = answered(app.client.get(FOLDERS), "S1: listing the folders")
    return views


def place(
    app: App, folder: str | None, subscriptions: list[str], items: list[str]
) -> httpx2.Response:
    return app.client.put(
        PLACEMENTS, json={"folder": folder, "subscriptions": subscriptions, "items": items}
    )


def placed(app: App, folder: str | None, subscriptions: list[str], items: list[str]) -> None:
    # A placing a judgment needs done; it fails here when the placing is refused.
    response = place(app, folder, subscriptions, items)
    if response.status_code != HTTPStatus.NO_CONTENT:
        pytest.fail(
            f"S5: a placing of the owner's own subscriptions and hand-added items in "
            f"{folder} was refused: {response.status_code} {response.text[:300]}"
        )


def entries(app: App) -> list[dict[str, Any]]:
    listed_entries: list[dict[str, Any]] = answered(app.client.get(ITEMS), "listing the items")
    return listed_entries


class Shown(BaseModel):
    # What the service shows an owner of where things are: the ids of their folders as listed,
    # and the folder of each of their subscriptions and items, by its id.
    folders: list[str]
    subscriptions: dict[str, str | None]
    items: dict[str, str | None]


def shown_now(app: App) -> Shown:
    return Shown(
        folders=[view["id"] for view in folders_listed(app)],
        subscriptions={view["id"]: view["folder"] for view in listed(app)},
        items={entry["id"]: entry["folder"] for entry in entries(app)},
    )


class Arrangement(BaseModel):
    # Where the sidebar's rules put an owner's subscriptions and items: the folders, in the
    # order they were made; the folder of each subscription's placement; the subscription each
    # item's origin names, if any; and the folder of each item's own placement.
    folders: list[str]
    subscriptions: dict[str, str | None]
    origins: dict[str, str | None]
    own: dict[str, str | None]

    def follows(self, item: str) -> bool:
        return self.origins[item] in self.subscriptions

    def folder_of(self, item: str) -> str | None:
        subscription = self.origins[item]
        if subscription is not None and subscription in self.subscriptions:
            return self.subscriptions[subscription]
        return self.own.get(item)

    def shown(self) -> Shown:
        return Shown(
            folders=self.folders,
            subscriptions=self.subscriptions,
            items={item: self.folder_of(item) for item in self.origins},
        )

    def refusals_of_placing(
        self, folder: str | None, subscriptions: list[str], items: list[str]
    ) -> set[HTTPStatus]:
        # How a placing is refused: 404 where it names a folder, a subscription or an item the
        # owner does not have, 409 where it names an item that follows its subscription; where
        # it is both, either answer is the requirements'.
        missing = (
            (folder is not None and folder not in self.folders)
            or any(subscription not in self.subscriptions for subscription in subscriptions)
            or any(item not in self.origins for item in items)
        )
        return {
            status
            for status, refused in (
                (HTTPStatus.NOT_FOUND, missing),
                (HTTPStatus.CONFLICT, any(self.follows(item) for item in items)),
            )
            if refused
        }

    def placing(
        self, folder: str | None, subscriptions: list[str], items: list[str]
    ) -> "Arrangement":
        return Arrangement(
            folders=self.folders,
            subscriptions={
                subscription: folder if subscription in subscriptions else placement
                for subscription, placement in self.subscriptions.items()
            },
            origins=self.origins,
            own={**self.own, **dict.fromkeys(items, folder)},
        )

    def dissolving(self, folder: str) -> "Arrangement":
        return Arrangement(
            folders=[kept for kept in self.folders if kept != folder],
            subscriptions={
                subscription: None if placement == folder else placement
                for subscription, placement in self.subscriptions.items()
            },
            origins=self.origins,
            own={
                item: None if placement == folder else placement
                for item, placement in self.own.items()
            },
        )

    def removing(self, subscription: str) -> "Arrangement":
        # The items whose origin names the subscription stay in the folder it was in.
        folder = self.subscriptions[subscription]
        return Arrangement(
            folders=self.folders,
            subscriptions={
                kept: placement
                for kept, placement in self.subscriptions.items()
                if kept != subscription
            },
            origins=self.origins,
            own={
                **self.own,
                **dict.fromkeys(
                    (item for item, named in self.origins.items() if named == subscription), folder
                ),
            },
        )


def made_up_document(item_id: str, media_type: str) -> Document:
    return document_of(
        item_id=item_id,
        extractor=EXTRACTOR,
        snapshot=SNAPSHOT,
        media_type=media_type,
        blocks=[Paragraph(text=f"{MADE_UP_TEXT} {media_type}", marks=[])],
    )


def made_up_address() -> str:
    return f"{SITE}/made-up/{uuid4().hex}"


def link_origin() -> Link:
    return Link(url=made_up_address())


def upload_origin() -> Upload:
    return Upload(name=f"{uuid4().hex}.html")


def subscribed_origin(subscription: str) -> Subscribed:
    address = made_up_address()
    return Subscribed(url=address, called=f"Posting at {address}", subscription=subscription)


def item_of(owner: str, origin: Origin) -> str:
    # The id of the owner's item of the origin: an address names it, as the service names it,
    # and a made-up upload by its name.
    match origin:
        case Link(url=address) | Subscribed(url=address):
            return item_identity(owner, address)
        case _:
            return item_identity(owner, origin.name)


def keep_item(store: LibraryStore, owner: str, origin: Origin, reading: Reading) -> str:
    # An item of the origin, kept in the owner's library with the reading given, as the
    # service would keep one; its id.
    item_id = item_of(owner, origin)
    with store.library(owner) as library:
        library.keep_item(Item(id=item_id, origin=origin, added=datetime.now(UTC), reading=reading))
    return item_id


def keep_read_item(store: LibraryStore, owner: str, origin: Origin, media_type: str) -> str:
    # An item of the origin read into a made-up document of the media type given; its id.
    item_id = item_of(owner, origin)
    document = made_up_document(item_id, media_type)
    reading = Read(title=f"Made up {item_id[:8]}", document=document.id, attempt=Settled())
    now = datetime.now(UTC)
    with store.library(owner) as library:
        library.keep_item(Item(id=item_id, origin=origin, added=now, reading=reading))
        library.keep_document(document, now)
    return item_id


def keep_web_item(store: LibraryStore, owner: str, origin: Origin) -> str:
    return keep_read_item(store, owner, origin, HTML_MEDIA_TYPE)


def subscription_of(app: App, feeds: FeedStandin) -> str:
    # A subscription of the owner's, to a feed of its own whose one posting nothing reads.
    tag = uuid4().hex
    return str(subscribed(app, served_feed(feeds, tag, [missing_posting(tag)]), admit=0)["id"])


def unchanged(app: App, before: Shown, what: str) -> None:
    assert shown_now(app) == before, f"{what} changed where the owner's things are"
