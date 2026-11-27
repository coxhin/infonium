# Folders, one level deep, that a reader sorts subscriptions and items into, and where each of
# them is placed. An item that follows its subscription is in its subscription's folder; any
# other item, and every subscription, is in the folder of its own placement, or in none.
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, StringConstraints

# The most characters a folder's name holds, once the white space around it is trimmed: a
# name stands on one line of the sidebar.
FOLDER_NAME_LENGTH = 50

FolderName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FOLDER_NAME_LENGTH)
]


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
