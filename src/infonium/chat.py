from collections.abc import Callable, Generator, Hashable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from threading import Lock
from typing import Annotated, Any, Final, Literal

import sqlalchemy
from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from infonium.citing import Statement
from infonium.document import Tagged
from infonium.language_model import Called, Message, Replied, ReplyEvent, Side, Written
from infonium.library import OwnerTransaction

# What the conversations about an item and about the whole library have alike: what they tell
# the one who asks, how their turns are kept, that each answers one question at a time, how a
# draft is followed, how earlier turns are given again and the rules of an answer's grounding.

# Stored with every turn, so that a later change to the models can tell which form a row has.
TURN_FORMAT = 1
# The most characters of earlier turns that a question gives the model again; the oldest
# turns are left out first, and the newest is cut where it alone is longer. The conversation
# that is kept is not cut.
EARLIER_CHARACTERS = 12_000
ANSWER_CUT = "\n[The rest of this answer is left out here.]"
TURNS_LEFT_OUT = (
    "{turns} earlier questions of this conversation, with their answers, are left out here; "
    "the questions that follow are the latest."
)
TURN_LEFT_OUT = (
    "1 earlier question of this conversation, with its answer, is left out here; the "
    "questions that follow are the latest."
)
# The rules of an answer's grounding, for the instructions of either conversation: what it
# rests on, and the rules of citing that its citations keep.
ANSWER_RULES = """\
Grounding rules:
- Answer in statements of one or two sentences each, as few as the question needs.
- Give every statement that tells what {material} says one to three citations.
{citing_rules}\
- When {material} does not say what the question asks, or says only part of it, give one \
statement that says only that, with no citations. What {material} does say on the matter \
goes into statements of their own, each with its citations.
- An earlier question may tell what the question to answer now refers to. The answer still \
rests on {material} alone.
"""
# The JSON of an answer that is still being written ends in the middle; a string it breaks
# off in is read as far as it goes.
UNFINISHED_JSON: Final = "trailing-strings"


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


class WrittenStatement(BaseModel):
    # A statement of an answer that is still being written. Its text is empty until the
    # model has begun it.
    text: str = ""


class WrittenAnswer(BaseModel):
    statements: list[WrittenStatement] = Field(default_factory=list)


WRITTEN_ANSWER = TypeAdapter(WrittenAnswer)


class KeptTurns:
    # The turns of one conversation, kept in a table of the conversation's own whose rows the
    # key's columns pick, in the order they were asked. Every table of turns has the columns
    # position, format and body besides those of its key, the owner's among them.
    def __init__(
        self,
        transaction: OwnerTransaction,
        table: sqlalchemy.Table,
        key: dict[sqlalchemy.Column[Any], str],
    ) -> None:
        self.connection = transaction.connection
        self.table = table
        self.key = key

    def where(self) -> list[sqlalchemy.ColumnElement[bool]]:
        return [column == value for column, value in self.key.items()]

    def turns(self) -> list[Turn]:
        bodies = self.connection.execute(
            sqlalchemy.select(self.table.c.body)
            .where(*self.where())
            .order_by(self.table.c.position)
        ).scalars()
        return [Turn.model_validate_json(body) for body in bodies]

    def keep_turn(self, turn: Turn) -> None:
        # A turn goes after the last one kept, counting from 0.
        following = (
            sqlalchemy.select(
                sqlalchemy.func.coalesce(sqlalchemy.func.max(self.table.c.position) + 1, 0)
            )
            .where(*self.where())
            .scalar_subquery()
        )
        self.connection.execute(
            sqlalchemy.insert(self.table).values(
                {
                    **self.key,
                    self.table.c.position: following,
                    self.table.c.format: TURN_FORMAT,
                    self.table.c.body: turn.model_dump_json(),
                }
            )
        )

    def forget(self) -> None:
        self.connection.execute(sqlalchemy.delete(self.table).where(*self.where()))


class ConversationBusy(Exception):
    pass


class Answering:
    # The conversations that are answering a question now, each by a key of its own kind. A
    # conversation answers one question at a time and is not cleared meanwhile: a second
    # question or a clearing is refused, not queued.
    def __init__(self) -> None:
        self.turn = Lock()
        self.answering: set[Hashable] = set()

    @contextmanager
    def claimed(self, conversation: Hashable, busy: str) -> Iterator[None]:
        with self.turn:
            if conversation in self.answering:
                raise ConversationBusy(busy)
            self.answering.add(conversation)
        try:
            yield
        finally:
            with self.turn:
                self.answering.remove(conversation)


def answer_rules(material: str, citing_rules: str) -> str:
    return ANSWER_RULES.format(material=material, citing_rules=citing_rules)


def drafted_statements(written: str) -> list[str]:
    # The texts of the statements in an answer the model is still writing, each as far as it
    # goes; a statement whose text has not begun is left out. Text that is not the beginning
    # of an answer, as a model writes before it calls a tool, is no draft.
    try:
        answer = WRITTEN_ANSWER.validate_json(written, experimental_allow_partial=UNFINISHED_JSON)
    except ValidationError:
        return []
    return [statement.text for statement in answer.statements if statement.text]


def followed[Output: BaseModel](
    replies: Iterator[ReplyEvent[Output]],
) -> Generator[Drafted | Called | Replied[Output]]:
    # A reply as a reader follows it: a draft each time the texts of its statements grow, and
    # the calls of tools and the whole reply as they come.
    shown: list[str] = []
    for event in replies:
        match event:
            case Written(text=text):
                statements = drafted_statements(text)
                if statements != shown:
                    shown = statements
                    yield Drafted(statements=shown)
            case Called() | Replied():
                yield event


def answered_text(answer: Answer) -> str:
    return "\n".join(statement.text for statement in answer.statements)


def earlier_messages(turns: list[Turn], given_again: Callable[[Answer], str]) -> list[Message]:
    # The questions asked before and their answers as the model is given them again, newest
    # last, within EARLIER_CHARACTERS. The newest turn is always given, its answer cut to what
    # fits, with a mark that says so; older turns are given while they fit; and where turns
    # are left out, the model is told how many.
    pairs = [(turn.question, given_again(turn.answer)) for turn in turns]
    if not pairs:
        return []
    *older, (question, answer) = pairs
    room = EARLIER_CHARACTERS - len(question)
    newest = answer if len(answer) <= room else f"{answer[: max(room, 0)]}{ANSWER_CUT}"
    room -= len(newest)
    kept = [(question, newest)]
    for earlier_question, earlier_answer in reversed(older):
        room -= len(earlier_question) + len(earlier_answer)
        if room < 0:
            break
        kept.insert(0, (earlier_question, earlier_answer))
    left_out = len(pairs) - len(kept)
    return [
        *(
            [
                Message(
                    side=Side.ASKER,
                    text=TURN_LEFT_OUT if left_out == 1 else TURNS_LEFT_OUT.format(turns=left_out),
                )
            ]
            if left_out
            else []
        ),
        *(
            message
            for kept_question, kept_answer in kept
            for message in (
                Message(side=Side.ASKER, text=kept_question),
                Message(side=Side.MODEL, text=kept_answer),
            )
        ),
    ]
