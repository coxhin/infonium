import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
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

from infonium.chat import (
    Answered,
    Answering,
    ChatEvent,
    Conversation,
    ConversationBusy,
    Drafted,
    ItemConversation,
    ItemConversations,
    Question,
    Questioned,
    Turn,
    Unanswered,
    answer_progress,
)
from infonium.citing import numbered_blocks
from infonium.digest import (
    Digest,
    DraftDigest,
    Section,
    digest_instructions,
    grounded_sections,
)
from infonium.document import Block, BlockNumber, MediaType, error_text
from infonium.embedding import EmbeddingEngine
from infonium.intake import Engines, Intake, failure_reason
from infonium.language_model import (
    LanguageModel,
    UnmeteredCompletion,
    UnstructuredCompletion,
    endable_openai_model,
    openai_model,
)
from infonium.library import (
    LOCAL_OWNER,
    Holding,
    Item,
    Library,
    LibraryStore,
    NotInLibrary,
    Origin,
    Reading,
)
from infonium.model_connection import Ending
from infonium.search import BlockIndex
from infonium.settings import Settings
from infonium.speech import Recogniser
from infonium.translation import Progress, TranslatedBlock

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
UNBUILT = "this part of infonium is not built yet"
# What an ASGI server's message says of itself, and the message that the connection is gone.
MESSAGE_TYPE = "type"
DISCONNECT = "http.disconnect"

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
        browser=settings.browser,
    )
    return Intake(store=library_store(), readers=READERS, engines=engines)


@cache
def block_index() -> BlockIndex:
    return BlockIndex(
        store=library_store(),
        engine=EmbeddingEngine(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
        ),
    )


@cache
def language_model() -> LanguageModel:
    return openai_model(name=settings.model, wire_api=settings.wire_api)


@cache
def answering_model() -> LanguageModel:
    # The model that writes digests, over connections that a reader who leaves cuts.
    return endable_openai_model(name=settings.model, wire_api=settings.wire_api)


@cache
def answering() -> Answering:
    return Answering()


@asynccontextmanager
async def running(service: FastAPI) -> AsyncIterator[None]:
    # infonium's own records are kept from INFO up, and written by the root logger to standard
    # error. The server sets up only its own loggers, which pass nothing on to the root, so
    # none of its lines is written twice.
    logging.basicConfig()
    logging.getLogger(__package__).setLevel(logging.INFO)
    intake().resume()
    block_index().resume()
    yield
    block_index().close()
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
    # The media type is that of the source kept beside the document, which says what a
    # reader can show of it: a page, the pages of a file, or a recording.
    id: str
    media_type: MediaType
    blocks: list[Block]


class DigestView(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    sections: list[Section]


class TranslationView(BaseModel):
    # How far the translation of a document into a language has come, and the blocks
    # translated so far, in the order of the document. A block that is not among them is
    # shown as its source: one that is not translated yet, and one that never is, such as
    # a listing or a formula. The tokens are what the translation has taken of its models so
    # far, blocks translated again included.
    document: str
    language: str
    progress: Progress
    input_tokens: int
    output_tokens: int
    blocks: list[TranslatedBlock]


class PageView(BaseModel):
    # The web page an item was read from, made ready for a frame that runs none of its
    # scripts: its addresses are those of the site it came from, and an element that holds
    # the text of one block, and nothing else, carries that block's number in the
    # attribute named here. The blocks placed so are listed; any other block has no
    # element of its own in the page.
    html: str
    block_attribute: str
    placed: list[BlockNumber]


def refusal(status: HTTPStatus, detail: str) -> Response:
    return Response(
        content=Failure(detail=detail).model_dump_json(), status_code=status, media_type=JSON
    )


def explained(status: HTTPStatus) -> Callable[[Request, Exception], Response]:
    def explain(request: Request, error: Exception) -> Response:
        return refusal(status, error_text(error))

    return explain


app.add_exception_handler(NotInLibrary, explained(HTTPStatus.NOT_FOUND))
app.add_exception_handler(NamelessUpload, explained(HTTPStatus.BAD_REQUEST))
app.add_exception_handler(openai.OpenAIError, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(UnstructuredCompletion, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(UnmeteredCompletion, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(ConversationBusy, explained(HTTPStatus.CONFLICT))

NOT_IN_LIBRARY: dict[int | str, dict[str, Any]] = {HTTPStatus.NOT_FOUND: {"model": Failure}}
NAMELESS: dict[int | str, dict[str, Any]] = {HTTPStatus.BAD_REQUEST: {"model": Failure}}
UPSTREAM_FAILED: dict[int | str, dict[str, Any]] = {HTTPStatus.BAD_GATEWAY: {"model": Failure}}
ANSWERING: dict[int | str, dict[str, Any]] = {HTTPStatus.CONFLICT: {"model": Failure}}
# What a model's failure to answer is told as, once the lines of an answer have begun.
MODEL_FAILURES = (openai.OpenAIError, UnstructuredCompletion, UnmeteredCompletion)


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


def questioned_item(item_id: str, owner: Owner) -> Iterator[Questioned]:
    # The item's conversation, held for a question until the question's response has been
    # sent, with what the question is asked of. A conversation that is answering, or an item
    # without a document, is refused before a line of the answer is sent.
    with answering().claimed(ItemConversation(owner=owner, item_id=item_id)):
        with library_store().transaction(owner) as transaction:
            questioned = Questioned(
                holding=Library(transaction).holding(item_id),
                turns=ItemConversations(transaction).turns(item_id),
            )
        yield questioned


QuestionedItem = Annotated[Questioned, Depends(questioned_item)]


async def ended_on_leaving(request: Request, ending: Ending) -> None:
    # The server says the connection is gone also once the response is complete, when ending
    # the reply ends nothing.
    while (await request.receive())[MESSAGE_TYPE] != DISCONNECT:
        pass
    ending.end()


async def answer_ending(request: Request) -> AsyncIterator[Ending]:
    # A reader who leaves in the middle of an answer ends it at once, whatever the model is
    # doing: the thread that writes the lines waits in a read of the model's reply, and only
    # cutting the reply's connection ends that wait.
    ending = Ending()
    watching = asyncio.create_task(ended_on_leaving(request, ending))
    try:
        yield ending
    finally:
        watching.cancel()


AnswerEnding = Annotated[Ending, Depends(answer_ending)]


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
        prompt=numbered_blocks(holding.title, holding.document),
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
    return DocumentView(id=document.id, media_type=document.media_type, blocks=document.blocks)


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


@app.get("/api/items/{item_id}/translation", responses=NOT_IN_LIBRARY)
def item_translation(item_id: str, owner: Owner) -> TranslationView:
    # The translation of the item's document into the language of digests, as far as it has
    # come; nothing is started.
    raise NotImplementedError(UNBUILT)


@app.put("/api/items/{item_id}/translation", responses=NOT_IN_LIBRARY)
def ensure_translation(item_id: str, owner: Owner) -> TranslationView:
    # Starts the translation unless it is under way, done or unneeded. One that stopped goes
    # on from the blocks it has.
    raise NotImplementedError(UNBUILT)


@app.post("/api/items/{item_id}/translation", responses=NOT_IN_LIBRARY)
def translate_anew(item_id: str, owner: Owner) -> TranslationView:
    # Starts the translation from its first block, in place of what was translated before.
    raise NotImplementedError(UNBUILT)


@app.post(
    "/api/items/{item_id}/translation/blocks/{block_number}",
    responses=NOT_IN_LIBRARY | UPSTREAM_FAILED,
)
def retranslate_block(item_id: str, block_number: BlockNumber, owner: Owner) -> TranslatedBlock:
    # Translates one block again with the model that writes digests, and keeps the result in
    # place of the block's earlier translation.
    raise NotImplementedError(UNBUILT)


@app.get("/api/items/{item_id}/page", responses=NOT_IN_LIBRARY)
def item_page(item_id: str, owner: Owner) -> PageView:
    # The page of an item that was read from a web page; any other item has none.
    raise NotImplementedError(UNBUILT)


@app.get("/api/items/{item_id}/conversation", responses=NOT_IN_LIBRARY)
def item_conversation(item_id: str, owner: Owner) -> Conversation:
    # An item without a document has no conversation.
    with library_store().transaction(owner) as transaction:
        Library(transaction).holding(item_id)
        return Conversation(turns=ItemConversations(transaction).turns(item_id))


@app.post("/api/items/{item_id}/conversation", responses=NOT_IN_LIBRARY | ANSWERING)
def ask_about_item(
    item_id: str,
    question: Question,
    owner: Owner,
    questioned: QuestionedItem,
    ending: AnswerEnding,
) -> Iterator[ChatEvent]:
    # Answers a question about the item's document, after the questions asked of it before.
    # What happens on the way is sent as it happens, each event a line of JSON. The turn is
    # kept before it is sent; a reader who leaves before then leaves nothing kept. A failure
    # of infonium's own is not told as an answer: it ends the lines without a last event.
    progress = answer_progress(
        answering_model(), questioned.holding, questioned.turns, question.text, ending
    )
    try:
        for step in progress:
            match step:
                case Drafted():
                    yield step
                case Turn():
                    with library_store().transaction(owner) as transaction:
                        ItemConversations(transaction).keep_turn(item_id, step)
                    yield Answered(turn=step)
    except MODEL_FAILURES as failure:
        yield Unanswered(reason=failure_reason(failure))


@app.delete(
    "/api/items/{item_id}/conversation",
    responses=NOT_IN_LIBRARY | ANSWERING,
    status_code=HTTPStatus.NO_CONTENT,
)
def forget_item_conversation(item_id: str, owner: Owner) -> None:
    # A conversation that is answering is not forgotten: the answer would be kept after it.
    with (
        answering().claimed(ItemConversation(owner=owner, item_id=item_id)),
        library_store().transaction(owner) as transaction,
    ):
        Library(transaction).holding(item_id)
        ItemConversations(transaction).forget(item_id)


@app.get("/api/conversation")
def library_conversation(owner: Owner) -> Conversation:
    raise NotImplementedError(UNBUILT)


@app.post("/api/conversation")
def ask_about_library(question: Question, owner: Owner) -> Iterator[ChatEvent]:
    # Answers a question about everything the library holds, after the questions asked of
    # the library before, and sends its events the same way.
    yield Unanswered(reason=UNBUILT)


@app.delete("/api/conversation", status_code=HTTPStatus.NO_CONTENT)
def forget_library_conversation(owner: Owner) -> None:
    raise NotImplementedError(UNBUILT)
