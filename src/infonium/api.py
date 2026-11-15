from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from http import HTTPStatus
from typing import Annotated, Any

import httpx
import openai
from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, HttpUrl

from infonium.digest import (
    Digest,
    DraftDigest,
    Section,
    digest_instructions,
    digest_prompt,
    grounded_sections,
)
from infonium.document import Block, document_of
from infonium.language_model import (
    LanguageModel,
    UnmeteredCompletion,
    UnstructuredCompletion,
    openai_model,
)
from infonium.library import (
    LOCAL_OWNER,
    Holding,
    Item,
    LibraryStore,
    NotInLibrary,
    item_identity,
)
from infonium.settings import Settings
from infonium.webpage import EXTRACTOR, UnreadablePage, extract_page, fetch_page

JSON = "application/json"

settings = Settings()
app = FastAPI(title="infonium")


class Failure(BaseModel):
    detail: str


class PageImport(BaseModel):
    url: HttpUrl


class ItemEntry(BaseModel):
    id: str
    url: str
    title: str


class DocumentView(BaseModel):
    id: str
    blocks: list[Block]


class ItemReading(BaseModel):
    entry: ItemEntry
    document: DocumentView


class DigestView(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    sections: list[Section]


def explained(status: HTTPStatus) -> Callable[[Request, Exception], Response]:
    def explain(request: Request, error: Exception) -> Response:
        failure = Failure(detail=str(error) or type(error).__name__)
        return Response(content=failure.model_dump_json(), status_code=status, media_type=JSON)

    return explain


app.add_exception_handler(NotInLibrary, explained(HTTPStatus.NOT_FOUND))
app.add_exception_handler(UnreadablePage, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(httpx.HTTPError, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(openai.OpenAIError, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(UnstructuredCompletion, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(UnmeteredCompletion, explained(HTTPStatus.BAD_GATEWAY))

NOT_IN_LIBRARY: dict[int | str, dict[str, Any]] = {HTTPStatus.NOT_FOUND: {"model": Failure}}
UPSTREAM_FAILED: dict[int | str, dict[str, Any]] = {HTTPStatus.BAD_GATEWAY: {"model": Failure}}


def current_owner() -> str:
    return LOCAL_OWNER


Owner = Annotated[str, Depends(current_owner)]


@cache
def library_store() -> LibraryStore:
    return LibraryStore(settings.data_dir)


@cache
def language_model() -> LanguageModel:
    return openai_model(name=settings.model, wire_api=settings.wire_api)


def entry_of(item: Item) -> ItemEntry:
    return ItemEntry(id=item.id, url=item.url, title=item.title)


def digest_view(digest: Digest) -> DigestView:
    return DigestView(
        model=digest.model,
        input_tokens=digest.input_tokens,
        output_tokens=digest.output_tokens,
        sections=digest.sections,
    )


def written_digest(holding: Holding, owner: str) -> Digest:
    model = language_model()
    completion = model.structured(
        instructions=digest_instructions(settings.language),
        prompt=digest_prompt(holding.item.title, holding.document),
        output=DraftDigest,
    )
    digest = Digest(
        document_id=holding.document.id,
        model=model.name,
        written=datetime.now(UTC),
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        sections=grounded_sections(holding.document, completion.output),
    )
    with library_store().library(owner) as library:
        library.keep_digest(digest)
    return digest


@app.get("/api/items")
def list_items(owner: Owner) -> list[ItemEntry]:
    with library_store().library(owner) as library:
        return [entry_of(item) for item in library.items()]


@app.post("/api/items", responses=UPSTREAM_FAILED)
def import_page(page: PageImport, owner: Owner) -> ItemEntry:
    fetched = fetch_page(str(page.url))
    extracted = extract_page(fetched.content, fetched.url)
    item = Item(
        id=item_identity(owner, fetched.url),
        url=fetched.url,
        title=extracted.title,
        added=datetime.now(UTC),
    )
    with library_store().library(owner) as library:
        library.keep_page(
            item,
            document_of(
                item_id=item.id,
                extractor=EXTRACTOR,
                snapshot=library.keep_snapshot(fetched.content),
                blocks=extracted.blocks,
            ),
        )
    return entry_of(item)


@app.get("/api/items/{item_id}", responses=NOT_IN_LIBRARY)
def item_reading(item_id: str, owner: Owner) -> ItemReading:
    with library_store().library(owner) as library:
        holding = library.holding(item_id)
    return ItemReading(
        entry=entry_of(holding.item),
        document=DocumentView(id=holding.document.id, blocks=holding.document.blocks),
    )


@app.post("/api/items/{item_id}/digest", responses=NOT_IN_LIBRARY | UPSTREAM_FAILED)
def rewrite_digest(item_id: str, owner: Owner) -> DigestView:
    with library_store().library(owner) as library:
        holding = library.holding(item_id)
    return digest_view(written_digest(holding, owner))


@app.put("/api/items/{item_id}/digest", responses=NOT_IN_LIBRARY | UPSTREAM_FAILED)
def ensure_digest(item_id: str, owner: Owner) -> DigestView:
    with library_store().library(owner) as library:
        holding = library.holding(item_id)
        kept = library.kept_digests(holding.document.id)
    return digest_view(kept[0] if kept else written_digest(holding, owner))
