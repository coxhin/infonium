from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

from infonium.digest import Statement
from infonium.document import Tagged


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
