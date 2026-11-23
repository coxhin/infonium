from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Annotated, Final, Literal

import sqlalchemy
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from infonium.anchor import anchors_of
from infonium.digest import HEADING_LABEL, DraftStatement, Statement, digest_prompt
from infonium.document import Tagged
from infonium.language_model import (
    LanguageModel,
    Message,
    Replied,
    Side,
    StructuredCompletion,
    Written,
)
from infonium.library import Holding, Library, item_table, metadata

# Stored with every turn, so that a later change to the models can tell which form a row has.
TURN_FORMAT = 1
BUSY = "a question about this item is being answered already"
INSTRUCTIONS = """\
You answer a reader's questions about one document. The reader checks every claim against \
the source.

The document arrives in the first message as numbered blocks. Each block starts with its \
number in square brackets. A block whose brackets also say "{heading_label}" is a heading. \
The questions asked before follow, each with the statements you answered it with, and last \
the question to answer now.

Write the answer in the language the question is asked in.

Grounding rules:
- State only what the document says. Add no outside knowledge and no opinion of your own.
- Answer in statements of one or two sentences each, as few as the question needs.
- Give every statement that tells what the document says one to three citations. A citation \
names one block by its number and quotes the words in that block that support the statement.
- Copy each quote from the block character for character, in the document's own language. \
Do not translate it, correct it, or shorten it with an ellipsis.
- Keep a quote to one sentence or a part of one, within a single block. Choose the passage \
where the document makes the point most directly.
- When the document does not say what the question asks, or says only part of it, give one \
statement that says only that, with no citations. What the document does say on the matter \
goes into statements of their own, each with its citations.
- An earlier question may tell what the question to answer now refers to. The answer still \
rests on the document alone.
"""


class Question(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CitedSource(BaseModel):
    # A document that an answer cites, the item it was read from, and what that item is
    # called.
    document: str
    item: str
    title: str


class Answer(BaseModel):
    # What the model answered: statements, each with the passages it rests on, and the
    # documents those passages lie in.
    model: str
    written: AwareDatetime
    input_tokens: int
    output_tokens: int
    statements: list[Statement]
    sources: list[CitedSource]


class Turn(BaseModel):
    question: str
    answer: Answer


class Conversation(BaseModel):
    # The questions asked about one item, or about the whole library, each with its answer,
    # in the order they were asked.
    turns: list[Turn]


class ChatEventKind(StrEnum):
    DRAFTED = "drafted"
    SEARCHED = "searched"
    ANSWERED = "answered"
    UNANSWERED = "unanswered"


class Drafted(Tagged):
    # The statements of the answer as far as the model has written them. Each event of this
    # kind holds the whole draft, so the latest one alone is what a reader shows.
    kind: Literal[ChatEventKind.DRAFTED] = ChatEventKind.DRAFTED
    statements: list[str]


class Searched(Tagged):
    # The model searched the library with this query.
    kind: Literal[ChatEventKind.SEARCHED] = ChatEventKind.SEARCHED
    query: str


class Answered(Tagged):
    # The answer is whole and kept in the conversation. It is the last event of its question.
    kind: Literal[ChatEventKind.ANSWERED] = ChatEventKind.ANSWERED
    turn: Turn


class Unanswered(Tagged):
    # No answer came, for this reason, and the question is not kept. It is the last event of
    # its question.
    kind: Literal[ChatEventKind.UNANSWERED] = ChatEventKind.UNANSWERED
    reason: str


# What happens while a question is answered, as it is told to the one who asked.
ChatEvent = Annotated[Drafted | Searched | Answered | Unanswered, Field(discriminator="kind")]


class DraftAnswer(BaseModel):
    # An answer as the model writes it.
    statements: list[DraftStatement]


class WrittenStatement(BaseModel):
    # A statement of an answer that is still being written. Its text is empty until the
    # model has begun it.
    text: str = ""


class WrittenAnswer(BaseModel):
    statements: list[WrittenStatement] = Field(default_factory=list)


WRITTEN_ANSWER = TypeAdapter(WrittenAnswer)
# The JSON of an answer that is still being written ends in the middle; a string it breaks
# off in is read as far as it goes.
UNFINISHED_JSON: Final = "trailing-strings"


class ItemConversation(BaseModel):
    # Which conversation a question is asked in: the one of an owner's item.
    model_config = ConfigDict(frozen=True)

    owner: str
    item_id: str


class ConversationBusy(Exception):
    pass


item_turn_table = sqlalchemy.Table(
    "item_turn",
    metadata,
    sqlalchemy.Column(
        "item_id", sqlalchemy.String, sqlalchemy.ForeignKey(item_table.c.id), primary_key=True
    ),
    sqlalchemy.Column("position", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)


class ItemConversations:
    # The conversations about an owner's items, kept in the library beside the items.
    def __init__(self, library: Library) -> None:
        self.connection = library.connection
        self.owner = library.owner

    def turns(self, item_id: str) -> list[Turn]:
        bodies = self.connection.execute(
            sqlalchemy.select(item_turn_table.c.body)
            .where(item_turn_table.c.owner == self.owner, item_turn_table.c.item_id == item_id)
            .order_by(item_turn_table.c.position)
        ).scalars()
        return [Turn.model_validate_json(body) for body in bodies]

    def keep_turn(self, item_id: str, turn: Turn) -> None:
        # A turn goes after the last one kept, counting from 0.
        following = (
            sqlalchemy.select(
                sqlalchemy.func.coalesce(sqlalchemy.func.max(item_turn_table.c.position) + 1, 0)
            )
            .where(item_turn_table.c.owner == self.owner, item_turn_table.c.item_id == item_id)
            .scalar_subquery()
        )
        self.connection.execute(
            sqlalchemy.insert(item_turn_table).values(
                {
                    item_turn_table.c.item_id: item_id,
                    item_turn_table.c.position: following,
                    item_turn_table.c.owner: self.owner,
                    item_turn_table.c.format: TURN_FORMAT,
                    item_turn_table.c.body: turn.model_dump_json(),
                }
            )
        )

    def forget(self, item_id: str) -> None:
        self.connection.execute(
            sqlalchemy.delete(item_turn_table).where(
                item_turn_table.c.owner == self.owner, item_turn_table.c.item_id == item_id
            )
        )


class Answering:
    # The conversations that are answering a question now. A conversation answers one
    # question at a time: a second one is refused, not queued.
    def __init__(self) -> None:
        self.turn = Lock()
        self.answering: set[ItemConversation] = set()

    @contextmanager
    def claimed(self, conversation: ItemConversation) -> Iterator[None]:
        with self.turn:
            if conversation in self.answering:
                raise ConversationBusy(BUSY)
            self.answering.add(conversation)
        try:
            yield
        finally:
            with self.turn:
                self.answering.remove(conversation)


def answer_instructions() -> str:
    return INSTRUCTIONS.format(heading_label=HEADING_LABEL)


def answered_text(answer: Answer) -> str:
    return "\n".join(statement.text for statement in answer.statements)


def conversation_messages(holding: Holding, turns: list[Turn], question: str) -> list[Message]:
    # The document comes first and stays the same from one question to the next, so that an
    # endpoint that caches the beginning of a request finds it again.
    earlier = [
        message
        for turn in turns
        for message in (
            Message(side=Side.ASKER, text=turn.question),
            Message(side=Side.MODEL, text=answered_text(turn.answer)),
        )
    ]
    return [
        Message(side=Side.ASKER, text=digest_prompt(holding.title, holding.document)),
        *earlier,
        Message(side=Side.ASKER, text=question),
    ]


def drafted_statements(written: str) -> list[str]:
    # The texts of the statements in an answer the model is still writing, each as far as it
    # goes; a statement whose text has not begun is left out.
    if not written.strip():
        return []
    answer = WRITTEN_ANSWER.validate_json(written, experimental_allow_partial=UNFINISHED_JSON)
    return [statement.text for statement in answer.statements if statement.text]


def answered_turn(
    holding: Holding,
    question: str,
    model: str,
    completion: StructuredCompletion[DraftAnswer],
    written: datetime,
) -> Turn:
    statements = [
        Statement(text=statement.text, anchors=anchors_of(holding.document, statement.citations))
        for statement in completion.output.statements
    ]
    cited = [
        CitedSource(
            document=holding.document.id, item=holding.document.item_id, title=holding.title
        )
    ]
    return Turn(
        question=question,
        answer=Answer(
            model=model,
            written=written,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            statements=statements,
            sources=cited if any(statement.anchors for statement in statements) else [],
        ),
    )


def answer_progress(
    model: LanguageModel, holding: Holding, turns: list[Turn], question: str
) -> Iterator[Drafted | Turn]:
    # The answer to a question about an item's document as it is written, a draft each time
    # the texts of its statements grow, and last the whole turn.
    shown: list[str] = []
    replies = model.replying(
        answer_instructions(),
        conversation_messages(holding, turns, question),
        DraftAnswer,
        tools=[],
    )
    for event in replies:
        match event:
            case Written(text=text):
                statements = drafted_statements(text)
                if statements != shown:
                    shown = statements
                    yield Drafted(statements=shown)
            case Replied(completion=completion):
                yield answered_turn(holding, question, model.name, completion, datetime.now(UTC))
