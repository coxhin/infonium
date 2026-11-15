from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import cache
from http import HTTPStatus
from pathlib import PurePath
from typing import Annotated, Any
from urllib.parse import urldefrag, urlsplit

import openai
from fastapi import Depends, FastAPI, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

from infonium.digest import (
    Digest,
    DraftDigest,
    Section,
    digest_instructions,
    digest_prompt,
    grounded_sections,
)
from infonium.document import Block
from infonium.intake import Engines, Intake
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
    Origin,
    Reading,
)
from infonium.settings import Settings
from infonium.speech import Recogniser

JSON = "application/json"
# How many sources are read at once. A reading may hold a model on a graphics card for
# minutes, so the number is small.
READERS = 2
# A source is another site's bytes served from this origin; a browser that opens one treats
# it as inert and as the kind of bytes it is said to be.
SOURCE_HEADERS = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FETCH_SITE = "sec-fetch-site"
OWN_SITES = frozenset({"same-origin", "none"})
ORIGIN = "origin"
HOST = "host"
OTHER_SITE = "only infonium's own page may change the library"
NAMELESS_UPLOAD = "an uploaded file needs a name"

settings = Settings()


@cache
def library_store() -> LibraryStore:
    return LibraryStore(settings.data_dir)


@cache
def intake() -> Intake:
    engines = Engines(
        pdf=settings.pdf_engine,
        recogniser=Recogniser(
            base_url=settings.speech_base_url,
            api_key=settings.speech_api_key,
            model=settings.speech_model,
        ),
    )
    return Intake(store=library_store(), readers=READERS, engines=engines)


@cache
def language_model() -> LanguageModel:
    return openai_model(name=settings.model, wire_api=settings.wire_api)


@asynccontextmanager
async def running(service: FastAPI) -> AsyncIterator[None]:
    intake().resume()
    yield
    intake().close()


app = FastAPI(title="infonium", lifespan=running)


class Failure(BaseModel):
    detail: str


class NamelessUpload(Exception):
    pass


class LinkImport(BaseModel):
    url: HttpUrl


class ItemEntry(BaseModel):
    id: str
    origin: Origin
    reading: Reading


class DocumentView(BaseModel):
    id: str
    blocks: list[Block]


class DigestView(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    sections: list[Section]


def refusal(status: HTTPStatus, detail: str) -> Response:
    return Response(
        content=Failure(detail=detail).model_dump_json(), status_code=status, media_type=JSON
    )


def explained(status: HTTPStatus) -> Callable[[Request, Exception], Response]:
    def explain(request: Request, error: Exception) -> Response:
        return refusal(status, str(error) or type(error).__name__)

    return explain


app.add_exception_handler(NotInLibrary, explained(HTTPStatus.NOT_FOUND))
app.add_exception_handler(NamelessUpload, explained(HTTPStatus.BAD_REQUEST))
app.add_exception_handler(openai.OpenAIError, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(UnstructuredCompletion, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(UnmeteredCompletion, explained(HTTPStatus.BAD_GATEWAY))

NOT_IN_LIBRARY: dict[int | str, dict[str, Any]] = {HTTPStatus.NOT_FOUND: {"model": Failure}}
NAMELESS: dict[int | str, dict[str, Any]] = {HTTPStatus.BAD_REQUEST: {"model": Failure}}
UPSTREAM_FAILED: dict[int | str, dict[str, Any]] = {HTTPStatus.BAD_GATEWAY: {"model": Failure}}


@app.middleware("http")
async def refuse_other_sites(
    request: Request, answered: Callable[[Request], Awaitable[Response]]
) -> Response:
    # A page of another site may not change the library: a form posted across sites needs
    # no leave from this service. A browser says which site a request comes from; where it
    # does not, which it does not over plain HTTP to a named host, the origin it names must
    # be this host. A client that is no browser names neither and is let through.
    site = request.headers.get(FETCH_SITE)
    origin = request.headers.get(ORIGIN)
    if site is not None:
        foreign = site not in OWN_SITES
    else:
        foreign = origin is not None and urlsplit(origin).netloc != request.headers.get(HOST)
    if request.method not in SAFE_METHODS and foreign:
        return refusal(HTTPStatus.FORBIDDEN, OTHER_SITE)
    return await answered(request)


def current_owner() -> str:
    return LOCAL_OWNER


Owner = Annotated[str, Depends(current_owner)]


def entry_of(item: Item) -> ItemEntry:
    return ItemEntry(id=item.id, origin=item.origin, reading=item.reading)


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
        prompt=digest_prompt(holding.title, holding.document),
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


@app.post("/api/items")
def import_link(link: LinkImport, owner: Owner) -> ItemEntry:
    # A fragment names a place in a page, not another page.
    return entry_of(intake().begin_link(owner, urldefrag(str(link.url)).url))


@app.post("/api/items/upload", responses=NAMELESS)
def import_upload(file: UploadFile, owner: Owner) -> ItemEntry:
    if file.filename is None:
        raise TypeError("a file sent in a form has a name")
    name = PurePath(file.filename).name
    if not name:
        raise NamelessUpload(NAMELESS_UPLOAD)
    return entry_of(intake().begin_upload(owner, name, library_store().staged(file.file)))


@app.get("/api/items/{item_id}", responses=NOT_IN_LIBRARY)
def item_entry(item_id: str, owner: Owner) -> ItemEntry:
    with library_store().library(owner) as library:
        return entry_of(library.item(item_id))


@app.get("/api/items/{item_id}/document", responses=NOT_IN_LIBRARY)
def item_document(item_id: str, owner: Owner) -> DocumentView:
    with library_store().library(owner) as library:
        document = library.holding(item_id).document
    return DocumentView(id=document.id, blocks=document.blocks)


@app.get("/api/items/{item_id}/source", responses=NOT_IN_LIBRARY, response_class=FileResponse)
def item_source(item_id: str, owner: Owner) -> FileResponse:
    store = library_store()
    with store.library(owner) as library:
        source = library.kept_source(item_id)
    return FileResponse(
        store.snapshot(source.snapshot), media_type=source.media_type, headers=SOURCE_HEADERS
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
