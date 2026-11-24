import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from tests.model_standin import Asked, ModelStandin, Reply

# How a reader talks with infonium about an item or about the library: a question's response
# read as its lines arrive, the turns kept, and a model's reply held open while a test looks.


class Asking(BaseModel):
    # What a question's response was: its status and, for a stream, its events in order.
    status: int
    events: list[dict[str, Any]]
    body: str


def asking(client: httpx2.Client, path: str, question: str) -> Asking:
    with client.stream("POST", path, json={"text": question}) as response:
        lines = [line for line in response.iter_lines() if line.strip()]
    streamed = response.status_code == HTTPStatus.OK
    events = [json.loads(line) for line in lines] if streamed else []
    return Asking(status=response.status_code, events=events, body="\n".join(lines))


def kept_turns(client: httpx2.Client, path: str) -> list[dict[str, Any]]:
    answered = client.get(path)
    assert answered.status_code == HTTPStatus.OK, f"a conversation was not served: {answered.text}"
    found: list[dict[str, Any]] = answered.json()["turns"]
    return found


def answered_turn(questioned: Asking) -> dict[str, Any]:
    assert questioned.status == HTTPStatus.OK, f"a question was refused: {questioned.body}"
    assert questioned.events and questioned.events[-1]["kind"] == "answered", (
        f"a question the model answered did not end with the answer: {questioned.events}"
    )
    turn: dict[str, Any] = questioned.events[-1]["turn"]
    return turn


def statement_texts(turn: dict[str, Any]) -> list[str]:
    return [statement["text"] for statement in turn["answer"]["statements"]]


@contextmanager
def held(model: ModelStandin, reply: Reply) -> Iterator[threading.Event]:
    # The model writes the first piece of the reply and waits until the test opens the gate,
    # which it does at the latest when the context ends; a server the reply goes through is
    # stopped after that, or it would wait for the reply.
    gate = threading.Event()
    model.reset(lambda asked: reply.model_copy(update={"gate": gate}))
    try:
        yield gate
    finally:
        gate.set()


def lines_of(response: httpx2.Response) -> Iterator[dict[str, Any]]:
    return (json.loads(line) for line in response.iter_lines() if line.strip())


def first_line(lines: Iterator[dict[str, Any]]) -> dict[str, Any]:
    # The first line of an answer that the model has begun and not finished, which a reader
    # waits for as long as the client's timeout of reading.
    try:
        first = next(lines, None)
    except httpx2.ReadTimeout:
        pytest.fail("no line of the answer was sent while the model was writing it")
    if first is None:
        pytest.fail("a question's response ended before its first line")
    return first


def first_asker_message(asked: Asked) -> str:
    messages = [text for role, text in asked.messages() if role == "user"]
    assert messages, "a request of the library's conversation holds no message of the asker"
    return messages[0]


def outline_of(asked: Asked) -> dict[str, Any]:
    # The outline, as the first message of the asker's side gives it: JSON after its label.
    message = first_asker_message(asked)
    assert "{" in message, f"the asker's first message holds no JSON outline: {message[:300]!r}"
    outline: dict[str, Any] = json.loads(message[message.index("{") :])
    return outline
