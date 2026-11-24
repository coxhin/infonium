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
from pydantic import AwareDatetime, BaseModel, Field, HttpUrl

from infonium.asking import UnusableTranslation
from infonium.blockholders import BLOCK_ATTRIBUTE, CELL_ATTRIBUTE, PAGE_ATTRIBUTE
from infonium.chat import (
    Answered,
    Answering,
    ChatEvent,
    Conversation,
    ConversationBusy,
    Drafted,
    KeptTurns,
    Question,
    Searched,
    Turn,
    Unanswered,
)
from infonium.citing import numbered_blocks
from infonium.digest import (
    Digest,
    DraftDigest,
    Section,
    digest_instructions,
    grounded_sections,
)
from infonium.document import HTML_MEDIA_TYPE, Block, BlockNumber, MediaType, error_text
from infonium.embedding import EmbeddingEngine
from infonium.framedpage import FramedPage, KeptPages
from infonium.highlight import Comment, HighlightDraft
from infonium.intake import Engines, Intake
from infonium.item_conversation import (
    ITEM_BUSY,
    ItemConversation,
    Questioned,
    answer_progress,
    item_turns,
)
from infonium.language_model import (
    MODEL_FAILURES,
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
    OwnerTransaction,
    Reading,
)
from infonium.library_conversation import (
    LIBRARY_BUSY,
    SEARCH_FAILED,
    LibraryConversation,
    library_answer_progress,
    library_shelf,
    library_turns,
)
from infonium.model_connection import Ending
from infonium.note import NoteDraft
from infonium.passage import Passage, PassageNotHeld, Standing
from infonium.reasons import failure_reason
from infonium.revision import Revision, RevisionNumber
from infonium.search import BlockIndex, IndexFailed
from infonium.settings import Settings
from infonium.speech import Recogniser
from infonium.translation import Progress, TranslatedBlock
from infonium.translator import TranslationNotAsked, TranslationStanding, Translator

JSON = "application/json"
# How many sources are read at once. A reading may hold a model on a graphics card for
# minutes, so the number is small.
READERS = 2
# A source is another site's bytes served from this origin; a browser that opens one treats
# it as inert and as the kind of bytes it is said to be.
SOURCE_HEADERS = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}
# A web page an item was read from, served as a document of its own, is inert and quiet
# whoever opens its address, a frame or a tab: these headers are the first line of defence,
# and what framedpage removes from the page the second. The page keeps infonium's origin so
# that the reader's code can reach into it, but no script, plugin or frame runs in it, no
# form is sent from it, and a click on a link does not ping an address.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "sandbox allow-same-origin; script-src 'none'; object-src 'none'; frame-src 'none';"
        " form-action 'none'; connect-src 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
# How many pages built for documents are kept, those asked for last; a page of a megabyte
# takes a fifth of a second to build.
PAGES_KEPT = 8
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FETCH_SITE = "sec-fetch-site"
OWN_SITES = frozenset({"same-origin", "none"})
ORIGIN = "origin"
HOST = "host"
OTHER_SITE = "only infonium's own page may change the library"
NAMELESS_UPLOAD = "an uploaded file needs a name"
UNBUILT = "this part of infonium is not built yet"
OPENAI_LOGGER = "openai"
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


@cache
def translation_model() -> LanguageModel:
    return openai_model(name=settings.translation_model, wire_api=settings.wire_api)


@cache
def translator() -> Translator:
    return Translator(
        store=library_store(),
        language=settings.language,
        translating=translation_model,
        retranslating=language_model,
    )


@asynccontextmanager
async def running(service: FastAPI) -> AsyncIterator[None]:
    # infonium's own records are kept from INFO up, and written by the root logger to standard
    # error. The server sets up only its own loggers, which pass nothing on to the root, so
    # none of its lines is written twice.
    logging.basicConfig()
    logging.getLogger(__package__).setLevel(logging.INFO)
    # The openai client says at INFO when it sends a request again; a slow answer is
    # explained by that beside the times of each request.
    logging.getLogger(OPENAI_LOGGER).setLevel(logging.INFO)
    intake().resume()
    translator().resume()
    block_index().resume()
    yield
    block_index().close()
    intake().close()
    translator().close()


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
    # far, blocks translated again included. The blocks to translate are the numbers of the
    # document's blocks that a translation translates, in order, whatever its progress: the
    # rest are never translated.
    document: str
    language: str
    progress: Progress
    input_tokens: int
    output_tokens: int
    blocks: list[TranslatedBlock]
    blocks_to_translate: list[BlockNumber]


class PageBlocks(BaseModel):
    # Where the blocks of an item's document stand in the web page it was read from, as the
    # page is served at the item's page address.
    page_attribute: str = Field(
        description=(
            "The name of the attribute that the root element of the served page carries, with"
            " the item's id as its value. By it the reader knows that a frame holds this item's"
            " page and nothing else, such as an error page that a proxy sent in its place, or"
            " another item's page. The page's own use of the attribute is removed."
        )
    )
    block_attribute: str = Field(
        description=(
            "The name of the attribute that marks, in the served page, the element that holds"
            " the whole text of one block that is not a table, and nothing else a reader or the"
            " rest of the page needs. Its value is the block's number, and one element at most"
            " carries a number. The element's content may be replaced by the block's"
            " translation, and the element may be scrolled to. It holds no picture, control,"
            " formula, image map or stylesheet, and no words for a browser without scripts."
            " The links, emphasis and line breaks inside it are part of the block's text, and"
            " the translation's marks and line breaks stand for them; a line break in the text"
            " or the translation is written into the page as a br element. Words that the page"
            " shows struck out are not part of the block's text, and go when the content is"
            " replaced, as they are left out of the text a reader reads. A place inside the"
            " element that the page's own links lead to, an element with an id, goes too: those"
            " links then lead nowhere. Where a block's text is only part of an element, the"
            " marked element is an infonium-run element added around that text, laid out"
            " inline."
        )
    )
    cell_attribute: str = Field(
        description=(
            "The name of the attribute that marks, in the served page, each cell of a table"
            " (a td, th or caption element) that holds one cell of a table block's text. Its"
            " value is three numbers parted by commas: the block's number, then the row and"
            " the cell within the row, counted from 0 as the block's text gives them when it is"
            " split at its row separator and each row at its cell separator. A cell of the"
            " block's text that says nothing has no element. A marked cell's content may be"
            " replaced by the translation of that cell of the block's text; it holds no"
            " picture, control, formula, image map or stylesheet, and no words for a browser"
            " without scripts. The translation of a table block is one text, which is split at"
            " the block's own row and cell separators into the cells that the marks name. The"
            " table itself carries no mark: a table block is scrolled to by its first marked"
            " cell."
        )
    )
    placed: list[BlockNumber] = Field(
        description=(
            "The numbers of the blocks that the page shows in elements of their own: for a"
            " block that is not a table, the element that carries the block attribute; for a"
            " table, every cell of its text that says something, each in an element that"
            " carries the cell attribute. A block not listed has no element of its own in the"
            " page; a figure never has, since the words that describe a picture are not shown"
            " in a page."
        )
    )


class ItemRevisions(BaseModel):
    # The revisions of an item's source, oldest first, and the number of the one that holds
    # the document the item shows now, which is the latest reading and may be a document read
    # before.
    current: RevisionNumber
    revisions: list[Revision]


class HighlightView(BaseModel):
    # A passage a reader marked in an item's source, with what the reader noted on it. The
    # passage is as it was marked, in the revision of that number; the standing says where it
    # lies in the document the item holds now, or that this document does not hold it.
    id: str
    item: str
    revision: RevisionNumber
    passage: Passage
    standing: Standing
    comment: str
    made: AwareDatetime


class ItemHighlights(BaseModel):
    # The highlights of one item, with what the item is called.
    item: str
    title: str
    highlights: list[HighlightView]


class NoteEntry(BaseModel):
    # A note as a list of notes shows it: what it is called, when it was first written and
    # last changed, and the items its passages lie in.
    id: str
    title: str
    made: AwareDatetime
    changed: AwareDatetime
    items: list[str]


class LinkedPassage(BaseModel):
    # A passage a note links to, under the id the note's Markdown names it by: the item it
    # lies in and what that item is called now, the passage as it was linked, in the revision
    # of that number, and where it stands in the document the item holds now.
    id: str
    item: str
    title: str
    revision: RevisionNumber
    passage: Passage
    standing: Standing


class NoteView(BaseModel):
    id: str
    title: str
    markdown: str
    passages: list[LinkedPassage]
    made: AwareDatetime
    changed: AwareDatetime


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
app.add_exception_handler(UnusableTranslation, explained(HTTPStatus.BAD_GATEWAY))
app.add_exception_handler(TranslationNotAsked, explained(HTTPStatus.CONFLICT))
app.add_exception_handler(PassageNotHeld, explained(HTTPStatus.CONFLICT))

NOT_IN_LIBRARY: dict[int | str, dict[str, Any]] = {HTTPStatus.NOT_FOUND: {"model": Failure}}
NAMELESS: dict[int | str, dict[str, Any]] = {HTTPStatus.BAD_REQUEST: {"model": Failure}}
SERVED_PAGE: dict[int | str, dict[str, Any]] = {
    HTTPStatus.OK: {"content": {HTML_MEDIA_TYPE: {"schema": {"type": "string"}}}}
}
UPSTREAM_FAILED: dict[int | str, dict[str, Any]] = {HTTPStatus.BAD_GATEWAY: {"model": Failure}}
ANSWERING: dict[int | str, dict[str, Any]] = {HTTPStatus.CONFLICT: {"model": Failure}}
NOT_ASKED: dict[int | str, dict[str, Any]] = {HTTPStatus.CONFLICT: {"model": Failure}}
# A passage that its own document does not hold as it was given: a block it names is not
# there, or the words at a range are not its quote.
NOT_HELD: dict[int | str, dict[str, Any]] = {HTTPStatus.CONFLICT: {"model": Failure}}
# What a model's failure to answer is told as, once the lines of an answer have begun.


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
    with answering().claimed(ItemConversation(owner=owner, item_id=item_id), ITEM_BUSY):
        with library_store().transaction(owner) as transaction:
            questioned = Questioned(
                holding=Library(transaction).holding(item_id),
                turns=item_turns(transaction, item_id).turns(),
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


def translation_view(standing: TranslationStanding) -> TranslationView:
    return TranslationView(
        document=standing.document,
        language=standing.language,
        progress=standing.progress,
        input_tokens=standing.input_tokens,
        output_tokens=standing.output_tokens,
        blocks=standing.blocks,
        blocks_to_translate=standing.blocks_to_translate,
    )


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


@app.post("/api/items/{item_id}/reading", responses=NOT_IN_LIBRARY)
def read_again(item_id: str, owner: Owner) -> ItemEntry:
    # Starts a new reading of the item's source: a link is fetched again, and an upload is
    # read again from the file the library keeps of it. Where a reading of the item is under
    # way already, that one goes on and no other starts. The document the item holds stays
    # until the new reading has given another.
    raise NotImplementedError(UNBUILT)


@app.get("/api/items/{item_id}/revisions", responses=NOT_IN_LIBRARY)
def item_revisions(item_id: str, owner: Owner) -> ItemRevisions:
    # An item without a document has no revisions.
    raise NotImplementedError(UNBUILT)


@app.get("/api/items/{item_id}/revisions/{number}/document", responses=NOT_IN_LIBRARY)
def revision_document(item_id: str, number: RevisionNumber, owner: Owner) -> DocumentView:
    # The document of one revision of the item, the one it holds now or an earlier one.
    raise NotImplementedError(UNBUILT)


@app.post("/api/items/{item_id}/standing", responses=NOT_IN_LIBRARY | NOT_HELD)
def passage_standing(item_id: str, passage: Passage, owner: Owner) -> Standing:
    # Where a passage of any revision of the item stands in the document the item holds now.
    # A passage of a document that is no revision of the item is not in the library.
    raise NotImplementedError(UNBUILT)


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
    return translation_view(translator().standing(owner, item_id))


@app.put("/api/items/{item_id}/translation", responses=NOT_IN_LIBRARY)
def ensure_translation(item_id: str, owner: Owner) -> TranslationView:
    # Starts the translation unless it is under way, done or unneeded. One that stopped goes
    # on from the blocks it has.
    return translation_view(translator().ensure(owner, item_id))


@app.post("/api/items/{item_id}/translation", responses=NOT_IN_LIBRARY)
def translate_anew(item_id: str, owner: Owner) -> TranslationView:
    # Starts the translation from its first block, in place of what was translated before.
    return translation_view(translator().anew(owner, item_id))


@app.post(
    "/api/items/{item_id}/translation/blocks/{block_number}",
    responses=NOT_IN_LIBRARY | UPSTREAM_FAILED | NOT_ASKED,
)
def retranslate_block(item_id: str, block_number: BlockNumber, owner: Owner) -> TranslatedBlock:
    # Translates one block again with the model that writes digests, and keeps the result in
    # place of the block's earlier translation. A translation that was never asked for, or
    # that is not needed, has no block to translate again.
    return translator().retranslated(owner, item_id, block_number)


@cache
def kept_pages() -> KeptPages:
    return KeptPages(room=PAGES_KEPT)


def framed_item_page(item_id: str, owner: str) -> FramedPage:
    # The page of an item that was read from a web page; any other item has none. The page is
    # built from the very document read here, and kept under that document's name.
    store = library_store()
    with store.library(owner) as library:
        origin = library.item(item_id).origin
        document = library.holding(item_id).document
    if document.media_type != HTML_MEDIA_TYPE:
        raise NotInLibrary(f"item {item_id} was not read from a web page")
    return kept_pages().page(owner, document, origin, store.snapshot(document.snapshot))


@app.get(
    "/api/items/{item_id}/page",
    responses=NOT_IN_LIBRARY | SERVED_PAGE,
    response_class=Response,
)
def item_page(item_id: str, owner: Owner) -> Response:
    page = framed_item_page(item_id, owner).html
    return Response(content=page, media_type=HTML_MEDIA_TYPE, headers=PAGE_HEADERS)


@app.get("/api/items/{item_id}/page/blocks", responses=NOT_IN_LIBRARY)
def item_page_blocks(item_id: str, owner: Owner) -> PageBlocks:
    return PageBlocks(
        page_attribute=PAGE_ATTRIBUTE,
        block_attribute=BLOCK_ATTRIBUTE,
        cell_attribute=CELL_ATTRIBUTE,
        placed=framed_item_page(item_id, owner).placed,
    )


def told(
    progress: Iterator[Searched | Drafted | Turn],
    owner: str,
    kept: Callable[[OwnerTransaction], KeptTurns],
) -> Iterator[ChatEvent]:
    # What an answer sends as it is written, and last the turn, kept before it is sent, or
    # why no answer came: the model's failure, or a search of the library that failed with
    # the index. A reader who leaves before then leaves nothing kept. Any other failure of
    # infonium's own is not told as an answer: it ends the lines without a last event.
    try:
        for step in progress:
            match step:
                case Searched() | Drafted():
                    yield step
                case Turn():
                    with library_store().transaction(owner) as transaction:
                        kept(transaction).keep_turn(step)
                    yield Answered(turn=step)
    except MODEL_FAILURES as failure:
        yield Unanswered(reason=failure_reason(failure))
    except IndexFailed:
        yield Unanswered(reason=SEARCH_FAILED)


@app.get("/api/items/{item_id}/conversation", responses=NOT_IN_LIBRARY)
def item_conversation(item_id: str, owner: Owner) -> Conversation:
    # An item without a document has no conversation.
    with library_store().transaction(owner) as transaction:
        Library(transaction).holding(item_id)
        return Conversation(turns=item_turns(transaction, item_id).turns())


@app.post("/api/items/{item_id}/conversation", responses=NOT_IN_LIBRARY | ANSWERING)
def ask_about_item(
    item_id: str,
    question: Question,
    owner: Owner,
    questioned: QuestionedItem,
    ending: AnswerEnding,
) -> Iterator[ChatEvent]:
    # Answers a question about the item's document, after the questions asked of it before.
    # What happens on the way is sent as it happens, each event a line of JSON.
    yield from told(
        answer_progress(
            answering_model(), questioned.holding, questioned.turns, question.text, ending
        ),
        owner,
        lambda transaction: item_turns(transaction, item_id),
    )


@app.delete(
    "/api/items/{item_id}/conversation",
    responses=NOT_IN_LIBRARY | ANSWERING,
    status_code=HTTPStatus.NO_CONTENT,
)
def forget_item_conversation(item_id: str, owner: Owner) -> None:
    # A conversation that is answering is not forgotten: the answer would be kept after it.
    with (
        answering().claimed(ItemConversation(owner=owner, item_id=item_id), ITEM_BUSY),
        library_store().transaction(owner) as transaction,
    ):
        Library(transaction).holding(item_id)
        item_turns(transaction, item_id).forget()


def questioned_library(owner: Owner) -> Iterator[list[Turn]]:
    # The owner's conversation about the library, held for a question until the question's
    # response has been sent, with its turns before the question. A conversation that is
    # answering is refused before a line of the answer is sent.
    with answering().claimed(LibraryConversation(owner=owner), LIBRARY_BUSY):
        with library_store().transaction(owner) as transaction:
            turns = library_turns(transaction).turns()
        yield turns


QuestionedLibrary = Annotated[list[Turn], Depends(questioned_library)]


@app.get("/api/conversation")
def library_conversation(owner: Owner) -> Conversation:
    with library_store().transaction(owner) as transaction:
        return Conversation(turns=library_turns(transaction).turns())


@app.post("/api/conversation", responses=ANSWERING)
def ask_about_library(
    question: Question, owner: Owner, turns: QuestionedLibrary, ending: AnswerEnding
) -> Iterator[ChatEvent]:
    # Answers a question about everything the library holds, after the questions asked of
    # the library before, and sends its events the same way. The model searches and reads
    # the library's blocks with tools that change nothing.
    shelf = library_shelf(library_store(), block_index(), owner)
    yield from told(
        library_answer_progress(answering_model(), shelf, turns, question.text, ending),
        owner,
        library_turns,
    )


@app.delete("/api/conversation", responses=ANSWERING, status_code=HTTPStatus.NO_CONTENT)
def forget_library_conversation(owner: Owner) -> None:
    # A conversation that is answering is not forgotten: the answer would be kept after it.
    with (
        answering().claimed(LibraryConversation(owner=owner), LIBRARY_BUSY),
        library_store().transaction(owner) as transaction,
    ):
        library_turns(transaction).forget()


@app.get("/api/items/{item_id}/highlights", responses=NOT_IN_LIBRARY)
def item_highlights(item_id: str, owner: Owner) -> list[HighlightView]:
    # The highlights of an item: first those that stand in the document it holds now, in the
    # order of that document, then those it has lost, in the order they were made.
    raise NotImplementedError(UNBUILT)


@app.post("/api/items/{item_id}/highlights", responses=NOT_IN_LIBRARY | NOT_HELD)
def make_highlight(item_id: str, draft: HighlightDraft, owner: Owner) -> HighlightView:
    # Marks a passage of a revision of the item. The passage is refused unless its document
    # is a revision of the item and holds it as given.
    raise NotImplementedError(UNBUILT)


@app.put("/api/items/{item_id}/highlights/{highlight_id}/comment", responses=NOT_IN_LIBRARY)
def comment_highlight(
    item_id: str, highlight_id: str, comment: Comment, owner: Owner
) -> HighlightView:
    # Puts what the reader notes on a highlight in place of what was noted before.
    raise NotImplementedError(UNBUILT)


@app.delete(
    "/api/items/{item_id}/highlights/{highlight_id}",
    responses=NOT_IN_LIBRARY,
    status_code=HTTPStatus.NO_CONTENT,
)
def remove_highlight(item_id: str, highlight_id: str, owner: Owner) -> None:
    raise NotImplementedError(UNBUILT)


@app.get("/api/highlights")
def library_highlights(owner: Owner) -> list[ItemHighlights]:
    # Every item of the library that has highlights, with them, the item marked last first;
    # an item's highlights are in the order its own list gives them.
    raise NotImplementedError(UNBUILT)


@app.get("/api/notes")
def list_notes(owner: Owner) -> list[NoteEntry]:
    # The notes of the library, the one changed last first.
    raise NotImplementedError(UNBUILT)


@app.post("/api/notes", responses=NOT_IN_LIBRARY | NOT_HELD)
def write_note(draft: NoteDraft, owner: Owner) -> NoteView:
    # Keeps a new note. A passage is refused unless its document is in the library and holds
    # it as given.
    raise NotImplementedError(UNBUILT)


@app.get("/api/notes/{note_id}", responses=NOT_IN_LIBRARY)
def read_note(note_id: str, owner: Owner) -> NoteView:
    raise NotImplementedError(UNBUILT)


@app.put("/api/notes/{note_id}", responses=NOT_IN_LIBRARY | NOT_HELD)
def rewrite_note(note_id: str, draft: NoteDraft, owner: Owner) -> NoteView:
    # Puts the draft in place of what the note held: its title, its Markdown and its passages.
    raise NotImplementedError(UNBUILT)


@app.delete("/api/notes/{note_id}", responses=NOT_IN_LIBRARY, status_code=HTTPStatus.NO_CONTENT)
def remove_note(note_id: str, owner: Owner) -> None:
    raise NotImplementedError(UNBUILT)
