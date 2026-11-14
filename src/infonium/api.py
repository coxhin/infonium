from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from importlib import resources
from typing import Annotated, Any

import httpx
import openai
import sqlalchemy
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, HttpUrl

from infonium.assets import keep_asset
from infonium.digest import Digest, DigestBrief, write_digest
from infonium.document import Document, document_of
from infonium.library import (
    LIBRARY_FILE,
    LOCAL_OWNER,
    Item,
    Library,
    NotInLibrary,
    item_identity,
    metadata,
)
from infonium.llm import LanguageModel, UnstructuredCompletion
from infonium.settings import Settings
from infonium.webpage import EXTRACTOR, UnreadablePage, fetch_page, read_page

ASSETS_DIRECTORY = "assets"

settings = Settings()
engine = sqlalchemy.create_engine(f"sqlite:///{settings.data_dir / LIBRARY_FILE}")
general_recipe = (
    resources.files("infonium").joinpath("recipes", "general.md").read_text(encoding="utf-8")
)


class Failure(BaseModel):
    detail: str


class PageImport(BaseModel):
    url: HttpUrl


class ItemEntry(BaseModel):
    id: str
    url: str
    title: str
    added: AwareDatetime


class ItemReading(BaseModel):
    entry: ItemEntry
    document: Document


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    metadata.create_all(engine)
    yield


app = FastAPI(title="infonium", lifespan=lifespan)


def explained(status: HTTPStatus) -> Callable[[Request, Exception], JSONResponse]:
    def explain(request: Request, error: Exception) -> JSONResponse:
        failure = Failure(detail=str(error) or type(error).__name__)
        return JSONResponse(status_code=status, content=failure.model_dump())

    return explain


app.add_exception_handler(NotInLibrary, explained(HTTPStatus.NOT_FOUND))
app.add_exception_handler(UnreadablePage, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(httpx.HTTPError, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(openai.OpenAIError, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(UnstructuredCompletion, explained(HTTPStatus.BAD_GATEWAY))

NOT_IN_LIBRARY: dict[int | str, dict[str, Any]] = {HTTPStatus.NOT_FOUND: {"model": Failure}}
UPSTREAM_FAILED: dict[int | str, dict[str, Any]] = {HTTPStatus.BAD_GATEWAY: {"model": Failure}}


def current_owner() -> str:
    return LOCAL_OWNER


Owner = Annotated[str, Depends(current_owner)]


@contextmanager
def open_library(owner: str) -> Iterator[Library]:
    with engine.begin() as connection:
        yield Library(connection=connection, owner=owner)


def entry_of(item: Item) -> ItemEntry:
    return ItemEntry(id=item.id, url=item.url, title=item.title, added=item.added)


@app.get("/api/items")
def list_items(owner: Owner) -> list[ItemEntry]:
    with open_library(owner) as library:
        return [entry_of(item) for item in library.items()]


@app.post("/api/items", responses=UPSTREAM_FAILED)
def import_page(page: PageImport, owner: Owner) -> ItemEntry:
    fetched = fetch_page(str(page.url))
    read = read_page(fetched.content, fetched.url)
    item = Item(
        id=item_identity(owner, fetched.url),
        url=fetched.url,
        title=read.title,
        snapshot=keep_asset(settings.data_dir / ASSETS_DIRECTORY, fetched.content),
        added=datetime.now(UTC),
    )
    with open_library(owner) as library:
        library.keep_page(
            item, document_of(item_id=item.id, extractor=EXTRACTOR, blocks=read.blocks)
        )
    return entry_of(item)


@app.get("/api/items/{item_id}", responses=NOT_IN_LIBRARY)
def read_item(item_id: str, owner: Owner) -> ItemReading:
    with open_library(owner) as library:
        return ItemReading(
            entry=entry_of(library.item(item_id)), document=library.document(item_id)
        )


@app.post("/api/items/{item_id}/digest", responses=NOT_IN_LIBRARY | UPSTREAM_FAILED)
def rewrite_digest(item_id: str, owner: Owner) -> Digest:
    with open_library(owner) as library:
        item = library.item(item_id)
        document = library.document(item_id)
    digest = write_digest(
        document,
        DigestBrief(
            title=item.title,
            recipe=general_recipe,
            language=settings.language,
            written=datetime.now(UTC),
        ),
        LanguageModel(client=openai.OpenAI(), name=settings.model),
    )
    with open_library(owner) as library:
        library.keep_digest(digest)
    return digest


@app.put("/api/items/{item_id}/digest", responses=NOT_IN_LIBRARY | UPSTREAM_FAILED)
def ensure_digest(item_id: str, owner: Owner) -> Digest:
    with open_library(owner) as library:
        kept = library.digests(library.document(item_id).id)
    return kept[0] if kept else rewrite_digest(item_id, owner)
