from typing import Annotated, Any, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

from infonium.citing import GivenBlock, given_block
from infonium.document import Document
from infonium.language_model import MOST_CALLS_PER_ROUND, Tool
from infonium.library import Library, LibraryStore, Read
from infonium.search import BlockIndex, FoundBy

# The two tools over the library's blocks that a model answering about the whole library is
# given, and the handles by which it names the library's sources. What a tool answers is
# JSON, in which a block's text is one string: nothing a source holds can end that string or
# pass for another result.

# A source's handle is the start of its document's id, this long at least, and longer where
# another document of the library starts the same way.
HANDLE_LENGTH = 8
# How many blocks one search returns and one reading may ask for, how much of a block's text
# either shows (a longer block is shown cut, and the model quotes from what it is shown), and
# how much of a title the outline and the results show.
SEARCH_RESULTS = 8
READ_BLOCKS = 12
SHOWN_CHARACTERS = 1500
TITLE_CHARACTERS = 120
CUT_MARK = " \N{HORIZONTAL ELLIPSIS}"
QUERY_CHARACTERS = 300
HANDLE_CHARACTERS = 64
SEARCH_TOOL = "search_blocks"
READ_TOOL = "read_blocks"
TOO_MANY_BLOCKS = f"read at most {READ_BLOCKS} blocks at a time"
BACKWARDS = "last_block comes before first_block"
NO_SUCH_SOURCE = (
    "No source of the library has the handle {handle!r}. The handles are those that the "
    "outline of the library and the search results give."
)
PAST_THE_END = "The source has {blocks} blocks, numbered from 0 to {last}."
FOUND_BY_TOLD = {
    FoundBy.ALL_WORDS: "every word of the query",
    FoundBy.SOME_WORDS: "some of the query's words, which may be only a common word such as 'of'",
    FoundBy.MEANING: "meaning alone: it may have nothing to do with the query",
    FoundBy.ALL_WORDS_AND_MEANING: "every word of the query, and meaning",
}
SEARCH_DESCRIPTION = f"""\
Searches the blocks of every source in the library for a query and returns the best \
{SEARCH_RESULTS}, best first, as JSON: a list "results" of objects, each with the source's \
handle ("document"), the block's number ("block"), the title of the source ("title"), how \
the block was found ("found_by") and the block's text ("text"), cut after \
{SHOWN_CHARACTERS} characters. An empty list means that nothing was found. A block is found \
by its words, in the language they are written in, and by its meaning. A block found by \
meaning alone may have nothing to do with the query: when no block holds the query's words, \
the nearest blocks are returned all the same. A block found by some of the query's words may \
hold only a common one. At most {MOST_CALLS_PER_ROUND} calls run in one round.\
"""
READ_DESCRIPTION = f"""\
Reads a run of consecutive blocks of one source, at most {READ_BLOCKS}, by the source's \
handle and the numbers of its first and last block, and returns them as JSON: the source's \
handle ("document"), its title ("title"), how many blocks it has ("blocks_in_source") and \
a list "blocks" of objects, each with the block's number ("block"), whether it is a \
heading ("heading") and its text ("text"). Use it to read around a block a search found, or \
a source from its start.\
"""


class Source(BaseModel):
    # A document an item of the library holds, by the handle the model knows it by, and the
    # item's whole title, which only what the model is given cuts.
    handle: str
    document: str
    item: str
    title: str


class Outline(BaseModel):
    # What the library held when a question began to be answered: how many items, and the
    # sources of those that have been read, newest first. The handles hold for the whole
    # answer, and name the documents the items held then.
    items: int
    sources: list[Source]

    def named(self, handle: str) -> list[Source]:
        # The source a handle names: its own handle, or, in any case, the start of exactly
        # one source's handle.
        exact = [source for source in self.sources if source.handle == handle]
        started = [
            source
            for source in self.sources
            if handle and source.handle.lower().startswith(handle.lower())
        ]
        return exact or (started if len(started) == 1 else [])

    def of_document(self, document: str) -> list[Source]:
        return [source for source in self.sources if source.document == document]


class OutlineSource(BaseModel):
    document: str
    title: str


class OutlineMaterial(BaseModel):
    # The outline as the model is given it.
    items: int
    unread_items: int
    sources: list[OutlineSource]
    sources_not_listed: int


class SearchArguments(BaseModel):
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=QUERY_CHARACTERS)
    ]


class ReadArguments(BaseModel):
    document: Annotated[str, StringConstraints(min_length=1, max_length=HANDLE_CHARACTERS)]
    first_block: Annotated[int, Field(ge=0)]
    last_block: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.last_block < self.first_block:
            raise ValueError(BACKWARDS)
        if self.last_block - self.first_block >= READ_BLOCKS:
            raise ValueError(TOO_MANY_BLOCKS)
        return self


class FoundResult(BaseModel):
    document: str
    block: int
    title: str
    found_by: str
    text: str


class SearchAnswer(BaseModel):
    query: str
    results: list[FoundResult]


class ReadAnswer(BaseModel):
    document: str
    title: str
    blocks_in_source: int
    blocks: list[GivenBlock]


class ToolRefusal(BaseModel):
    error: str


def shared_start(first: str, second: str) -> int:
    return next(
        (
            index
            for index, (one, other) in enumerate(zip(first, second, strict=False))
            if one != other
        ),
        min(len(first), len(second)),
    )


def document_handles(documents: list[str]) -> dict[str, str]:
    # The shortest start of each id, of HANDLE_LENGTH at least, that no other id begins with:
    # an id shares its longest start with one of its neighbours in sorted order.
    ordered = sorted(set(documents))
    # The list of those before is one longer, by the id after the last, which no id follows.
    neighbours = list(zip(["", *ordered], ordered, [*ordered[1:], ""], strict=False))
    return {
        document: document[
            : max(
                HANDLE_LENGTH,
                shared_start(before, document) + 1,
                shared_start(document, after) + 1,
            )
        ]
        for before, document, after in neighbours
    }


def cut(text: str, length: int) -> str:
    return f"{text[:length]}{CUT_MARK}" if len(text) > length else text


def library_outline(library: Library) -> Outline:
    items = library.items()
    read = [(item.id, item.reading) for item in items if isinstance(item.reading, Read)]
    handles = document_handles([reading.document for _, reading in read])
    return Outline(
        items=len(items),
        sources=[
            Source(
                handle=handles[reading.document],
                document=reading.document,
                item=item,
                title=reading.title,
            )
            for item, reading in read
        ],
    )


def outline_material(outline: Outline, listed: int) -> str:
    return OutlineMaterial(
        items=outline.items,
        unread_items=outline.items - len(outline.sources),
        sources=[
            OutlineSource(document=source.handle, title=cut(source.title, TITLE_CHARACTERS))
            for source in outline.sources[:listed]
        ],
        sources_not_listed=max(0, len(outline.sources) - listed),
    ).model_dump_json()


class Shelf:
    # What the tools of an owner's library conversation read, and nothing they change: the
    # documents the owner's items held when the answer began, and the index of their blocks.
    def __init__(self, store: LibraryStore, index: BlockIndex, owner: str, outline: Outline):
        self.store = store
        self.index = index
        self.owner = owner
        self.outline = outline

    def search_blocks(self, arguments: SearchArguments) -> str:
        # The index may hold a document that an item has been read again into since the
        # answer began; its blocks are not among the results.
        found = self.index.search(self.owner, arguments.query, SEARCH_RESULTS)
        return SearchAnswer(
            query=arguments.query,
            results=[
                FoundResult(
                    document=source.handle,
                    block=block.block_number,
                    title=cut(source.title, TITLE_CHARACTERS),
                    found_by=FOUND_BY_TOLD[block.found_by],
                    text=cut(block.text, SHOWN_CHARACTERS),
                )
                for block in found
                for source in self.outline.of_document(block.document)
            ],
        ).model_dump_json()

    def documents(self, handles: set[str]) -> dict[str, Document]:
        # The documents that these handles name, by the handle as it was given.
        named = {handle: source for handle in handles for source in self.outline.named(handle)}
        with self.store.transaction(self.owner) as transaction:
            library = Library(transaction)
            return {
                handle: library.owned_document(source.document) for handle, source in named.items()
            }

    def read_blocks(self, arguments: ReadArguments) -> str:
        sources = self.outline.named(arguments.document)
        documents = self.documents({arguments.document})
        if not documents:
            return ToolRefusal(
                error=NO_SUCH_SOURCE.format(handle=arguments.document)
            ).model_dump_json()
        blocks = documents[arguments.document].blocks
        last = min(arguments.last_block, len(blocks) - 1)
        if arguments.first_block > last:
            return ToolRefusal(
                error=PAST_THE_END.format(blocks=len(blocks), last=len(blocks) - 1)
            ).model_dump_json()
        return ReadAnswer(
            document=sources[0].handle,
            title=cut(sources[0].title, TITLE_CHARACTERS),
            blocks_in_source=len(blocks),
            blocks=[
                given_block(number, blocks[number], cut(blocks[number].text, SHOWN_CHARACTERS))
                for number in range(arguments.first_block, last + 1)
            ],
        ).model_dump_json()

    def tools(self) -> list[Tool[Any]]:
        return [
            Tool(
                name=SEARCH_TOOL,
                description=SEARCH_DESCRIPTION,
                arguments=SearchArguments,
                run=self.search_blocks,
            ),
            Tool(
                name=READ_TOOL,
                description=READ_DESCRIPTION,
                arguments=ReadArguments,
                run=self.read_blocks,
            ),
        ]
