from collections.abc import Generator
from datetime import UTC, datetime

import sqlalchemy
from pydantic import BaseModel, ConfigDict

from infonium.chat import (
    Answer,
    CitedSource,
    Drafted,
    KeptTurns,
    Turn,
    answer_rules,
    answered_text,
    earlier_messages,
    followed,
)
from infonium.citing import (
    BLOCKS_EXPLAINED,
    CITING_RULES,
    DraftStatement,
    grounded,
    numbered_blocks,
)
from infonium.language_model import (
    LanguageModel,
    Message,
    Replied,
    Side,
    StructuredCompletion,
)
from infonium.library import Holding, OwnerTransaction, item_table, metadata
from infonium.model_connection import Ending

ITEM_BUSY = "a question about this item is being answered already"
INSTRUCTIONS = """\
You answer a reader's questions about one document. The reader checks every claim against \
the source.

{blocks_explained}\
The document comes in the first message. The questions asked before follow, each with the \
statements you answered it with, and last the question to answer now.

Write the answer in the language the question is asked in.

{answer_rules}"""

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


class ItemConversation(BaseModel):
    # Which conversation a question is asked in: the one of an owner's item.
    model_config = ConfigDict(frozen=True)

    owner: str
    item_id: str


class Questioned(BaseModel):
    # What a question about an item is asked of: its document, and the turns of its
    # conversation before the question.
    holding: Holding
    turns: list[Turn]


class DraftAnswer(BaseModel):
    # An answer as the model writes it.
    statements: list[DraftStatement]


def item_turns(transaction: OwnerTransaction, item_id: str) -> KeptTurns:
    return KeptTurns(
        transaction,
        item_turn_table,
        {item_turn_table.c.owner: transaction.owner, item_turn_table.c.item_id: item_id},
    )


def answer_instructions() -> str:
    return INSTRUCTIONS.format(
        blocks_explained=BLOCKS_EXPLAINED,
        answer_rules=answer_rules("the document", CITING_RULES),
    )


def conversation_messages(holding: Holding, turns: list[Turn], question: str) -> list[Message]:
    # The document comes first and stays the same from one question to the next, so that an
    # endpoint that caches the beginning of a request finds it again.
    return [
        Message(side=Side.ASKER, text=numbered_blocks(holding.title, holding.document)),
        *earlier_messages(turns, answered_text),
        Message(side=Side.ASKER, text=question),
    ]


def answered_turn(
    holding: Holding,
    question: str,
    model: str,
    completion: StructuredCompletion[DraftAnswer],
    written: datetime,
) -> Turn:
    statements = [
        grounded(holding.document, statement) for statement in completion.output.statements
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
    model: LanguageModel, holding: Holding, turns: list[Turn], question: str, ending: Ending
) -> Generator[Drafted | Turn]:
    # The answer to a question about an item's document as it is written, a draft each time
    # the texts of its statements grow, and last the whole turn.
    replies = model.replying(
        answer_instructions(),
        conversation_messages(holding, turns, question),
        DraftAnswer,
        [],
        ending,
    )
    for event in followed(replies):
        match event:
            case Drafted():
                yield event
            case Replied(completion=completion):
                yield answered_turn(holding, question, model.name, completion, datetime.now(UTC))
