# Folders, one level deep, that a reader sorts subscriptions and items into, and where each of
# them is placed. An item that follows its subscription is in its subscription's folder; any
# other item, and every subscription, is in the folder of its own placement, or in none.
from collections.abc import Collection
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

import sqlalchemy
from pydantic import AwareDatetime, BaseModel, StringConstraints

from infonium.library import (
    Item,
    NotInLibrary,
    OwnerTransaction,
    Subscribed,
    item_table,
    metadata,
    missing_item,
    origin_subscription,
)
from infonium.subscriptions.subscription import (
    Subscriptions,
    missing_subscription,
    subscription_table,
)

# The most characters a folder's name holds, once the white space around it is trimmed: a
# name stands on one line of the sidebar.
FOLDER_NAME_LENGTH = 50
# Stored with every row, as the library's tables do.
FOLDER_FORMAT = 1
PLACEMENT_FORMAT = 1
TAKEN_NAME = 'a folder called "{name}" is in the library already'
MOVES_WITH_SUBSCRIPTION = (
    '"{item}" came with the subscription "{subscription}" and is in that subscription\'s'
    " folder; it moves only with the subscription"
)

FolderName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FOLDER_NAME_LENGTH)
]

# The folders of every owner, each name once among its owner's folders.
folder_table = sqlalchemy.Table(
    "folder",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("name", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
    sqlalchemy.UniqueConstraint("owner", "name"),
)
# The placements of subscriptions and of items, one at most of each. The library's file
# refuses a placement in a folder it does not hold, the removal of a folder a placement is in,
# and the removal of a subscription that has its placement.
subscription_placement_table = sqlalchemy.Table(
    "subscription_placement",
    metadata,
    sqlalchemy.Column(
        "subscription_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(subscription_table.c.id),
        primary_key=True,
    ),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column(
        "folder_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(folder_table.c.id),
        nullable=False,
        index=True,
    ),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
)
item_placement_table = sqlalchemy.Table(
    "item_placement",
    metadata,
    sqlalchemy.Column(
        "item_id", sqlalchemy.String, sqlalchemy.ForeignKey(item_table.c.id), primary_key=True
    ),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column(
        "folder_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(folder_table.c.id),
        nullable=False,
        index=True,
    ),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
)


class FolderDraft(BaseModel):
    # What a reader sends to make a folder or to rename one.
    name: FolderName


class Folder(BaseModel):
    # A folder's id stays as long as the folder does; its name may change.
    id: str
    name: FolderName
    added: AwareDatetime


class Placing(BaseModel):
    # Places every subscription and item named in the folder, or in none.
    folder: str | None
    subscriptions: list[str] = []
    items: list[str] = []


class FolderNameTaken(Exception):
    # Another folder of the owner's has the name.
    pass


class FollowsSubscription(Exception):
    # An item that follows its subscription is in its subscription's folder, and moves only
    # with it.
    pass


def subscription_followed(item: Item, subscriptions: Collection[str]) -> list[Subscribed]:
    # The origin of an item that follows its subscription: one a subscription brought, while
    # that subscription is among those given, the owner's; nothing for any other item.
    match item.origin:
        case Subscribed(subscription=subscription_id) as origin if subscription_id in subscriptions:
            return [origin]
        case _:
            return []


class Placed(BaseModel):
    # Where the owner's subscriptions and items are, as one reading of the library found them:
    # each of the owner's subscriptions with the folder of its placement, or none, and each
    # item that has a placement of its own with its folder.
    subscriptions: dict[str, str | None]
    items: dict[str, str]

    def item_folder(self, item: Item) -> str | None:
        # An item that follows its subscription is in its subscription's folder, whatever
        # placement of its own it keeps; any other is in that of its own placement, or in none.
        followed = subscription_followed(item, self.subscriptions)
        return self.subscriptions[followed[0].subscription] if followed else self.items.get(item.id)


def missing_folder(folder_id: str) -> NotInLibrary:
    return NotInLibrary(f"folder {folder_id} is not in the library")


def folder_row(folder: Folder, owner: str) -> dict[sqlalchemy.Column[Any], str | int]:
    return {
        folder_table.c.id: folder.id,
        folder_table.c.owner: owner,
        folder_table.c.name: folder.name,
        folder_table.c.format: FOLDER_FORMAT,
        folder_table.c.body: folder.model_dump_json(),
    }


class Folders:
    # The folders of one owner, and where the owner's subscriptions and items are placed,
    # read and written in one transaction. What changes them runs in a write transaction, so
    # that what it finds stays as it found it until it is done.
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.connection = transaction.connection
        self.owner = transaction.owner
        self.subscriptions = Subscriptions(transaction)

    def listed(self) -> list[Folder]:
        # The one added first first.
        bodies = self.connection.execute(
            sqlalchemy.select(folder_table.c.body).where(folder_table.c.owner == self.owner)
        ).scalars()
        return sorted(
            (Folder.model_validate_json(body) for body in bodies),
            key=lambda folder: (folder.added, folder.id),
        )

    def folder(self, folder_id: str) -> Folder:
        body = self.connection.execute(
            sqlalchemy.select(folder_table.c.body).where(
                folder_table.c.owner == self.owner, folder_table.c.id == folder_id
            )
        ).scalar_one_or_none()
        if body is None:
            raise missing_folder(folder_id)
        return Folder.model_validate_json(body)

    def refuse_taken(self, name: str) -> None:
        # A name that another folder of the owner's has is refused.
        taken = self.connection.execute(
            sqlalchemy.select(
                sqlalchemy.exists().where(
                    folder_table.c.owner == self.owner, folder_table.c.name == name
                )
            )
        ).scalar_one()
        if taken:
            raise FolderNameTaken(TAKEN_NAME.format(name=name))

    def make(self, draft: FolderDraft, added: datetime) -> Folder:
        self.refuse_taken(draft.name)
        folder = Folder(id=uuid4().hex, name=draft.name, added=added)
        self.connection.execute(
            sqlalchemy.insert(folder_table).values(folder_row(folder, self.owner))
        )
        return folder

    def rename(self, folder_id: str, draft: FolderDraft) -> Folder:
        # A folder given its own name is left as it is.
        folder = self.folder(folder_id)
        if draft.name == folder.name:
            return folder
        self.refuse_taken(draft.name)
        renamed = folder.model_copy(update={"name": draft.name})
        self.connection.execute(
            sqlalchemy.update(folder_table)
            .where(folder_table.c.owner == self.owner, folder_table.c.id == folder_id)
            .values(folder_row(renamed, self.owner))
        )
        return renamed

    def dissolve(self, folder_id: str) -> None:
        # The folder goes with every placement in it; its subscriptions and items stay, in no
        # folder.
        for placements in (subscription_placement_table, item_placement_table):
            self.connection.execute(
                sqlalchemy.delete(placements).where(
                    placements.c.owner == self.owner, placements.c.folder_id == folder_id
                )
            )
        removed = self.connection.execute(
            sqlalchemy.delete(folder_table).where(
                folder_table.c.owner == self.owner, folder_table.c.id == folder_id
            )
        )
        if removed.rowcount == 0:
            raise missing_folder(folder_id)

    def placed(self) -> Placed:
        subscriptions = self.connection.execute(
            sqlalchemy.select(subscription_table.c.id, subscription_placement_table.c.folder_id)
            .outerjoin(
                subscription_placement_table,
                (subscription_placement_table.c.owner == self.owner)
                & (subscription_placement_table.c.subscription_id == subscription_table.c.id),
            )
            .where(subscription_table.c.owner == self.owner)
        ).all()
        items = self.connection.execute(
            sqlalchemy.select(
                item_placement_table.c.item_id, item_placement_table.c.folder_id
            ).where(item_placement_table.c.owner == self.owner)
        ).all()
        return Placed(
            subscriptions={subscription: folder for subscription, folder in subscriptions},
            items={item: folder for item, folder in items},
        )

    def place(self, placing: Placing) -> None:
        # Every subscription and item named is placed in the folder, or in none. The whole
        # placing is refused, and nothing changes, where the folder, a subscription or an item
        # is not the owner's, and then where an item follows its subscription.
        if placing.folder is not None:
            self.folder(placing.folder)
        subscription_ids = list(dict.fromkeys(placing.subscriptions))
        item_ids = list(dict.fromkeys(placing.items))
        placed = self.placed()
        for subscription_id in subscription_ids:
            if subscription_id not in placed.subscriptions:
                raise missing_subscription(subscription_id)
        bodies = self.connection.execute(
            sqlalchemy.select(item_table.c.body).where(
                item_table.c.owner == self.owner, item_table.c.id.in_(item_ids)
            )
        ).scalars()
        items = {item.id: item for item in (Item.model_validate_json(body) for body in bodies)}
        for item_id in item_ids:
            if item_id not in items:
                raise missing_item(item_id)
        for item_id in item_ids:
            for origin in subscription_followed(items[item_id], placed.subscriptions):
                subscription = self.subscriptions.subscription(origin.subscription)
                raise FollowsSubscription(
                    MOVES_WITH_SUBSCRIPTION.format(
                        item=origin.called, subscription=subscription.title
                    )
                )
        self.replace_placements(
            subscription_placement_table.c.subscription_id, subscription_ids, placing.folder
        )
        self.replace_placements(item_placement_table.c.item_id, item_ids, placing.folder)

    def replace_placements(
        self, naming: sqlalchemy.Column[str], ids: list[str], folder: str | None
    ) -> None:
        # The placements of those whose ids are given, in the column of their table that names
        # them, give way to placements in the folder, or to none.
        placements = naming.table
        self.connection.execute(
            sqlalchemy.delete(placements).where(placements.c.owner == self.owner, naming.in_(ids))
        )
        if folder is not None and ids:
            self.connection.execute(
                sqlalchemy.insert(placements),
                [
                    {
                        naming.name: placed_id,
                        placements.c.owner.name: self.owner,
                        placements.c.folder_id.name: folder,
                        placements.c.format.name: PLACEMENT_FORMAT,
                    }
                    for placed_id in ids
                ],
            )

    def release(self, subscription_id: str) -> None:
        # Before the subscription is removed: the items whose origin names it, which follow it
        # no more once it is gone, are placed in the folder it is in, or in none, and its own
        # placement goes.
        folder = self.connection.execute(
            sqlalchemy.select(subscription_placement_table.c.folder_id).where(
                subscription_placement_table.c.owner == self.owner,
                subscription_placement_table.c.subscription_id == subscription_id,
            )
        ).scalar_one_or_none()
        brought = self.connection.execute(
            sqlalchemy.select(item_table.c.id).where(
                item_table.c.owner == self.owner, origin_subscription == subscription_id
            )
        ).scalars()
        self.replace_placements(item_placement_table.c.item_id, list(brought), folder)
        self.connection.execute(
            sqlalchemy.delete(subscription_placement_table).where(
                subscription_placement_table.c.owner == self.owner,
                subscription_placement_table.c.subscription_id == subscription_id,
            )
        )
