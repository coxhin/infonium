import asyncio
import base64
import errno
import json
import math
import os
import re
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from http import HTTPStatus
from typing import Any

import pytest
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from tests.model_standin import JSON_TYPE, LocalSocket
from tests.unix_server import unix_server

# An embedding engine answered in this process, speaking OpenAI's embeddings API over a real
# HTTP connection, as the model's stand-in is reached: every connection that infonium opens to
# EMBEDDING_HOST is made to the stand-in's Unix socket instead. It is away until a test brings
# it up: a connection to it is then refused, as one to a stopped engine is. A test may also hold
# the requests that carry a text, which slows the engine down as far as it likes, and make it
# answer every request with an error.
EMBEDDING_HOST = "embedding.test"
EMBEDDING_BASE_URL = f"http://{EMBEDDING_HOST}/v1"
EMBEDDING_KEY = "stand-in-embedding-key"
EMBEDDINGS_PATH = "/v1/embeddings"
HOLD_POLL_SECONDS = 0.01
# What a socket's file number is once it is closed.
CLOSED = -1
# What a real endpoint refuses: OpenAI's takes at most 2048 texts a request, and a text of at
# most 8192 tokens. A token is counted here as a Chinese or Japanese character, a run of other
# letters and digits, or another character that is not a space, which a real tokenizer never
# counts as fewer.
MOST_TEXTS = 2048
MOST_TOKENS = 8192
# The letters of Chinese and Japanese: the kana and the unified ideographs.
UNSPACED_LETTERS = f"{chr(0x3040)}-{chr(0x30FF)}{chr(0x3400)}-{chr(0x9FFF)}"
TOKENS = re.compile(rf"[{UNSPACED_LETTERS}]|[^\W{UNSPACED_LETTERS}]+|\S")
WORDS = re.compile(r"\w+")
UNSPACED = re.compile(f"[{UNSPACED_LETTERS}]")
FLOAT = "float"
BASE64 = "base64"
LITTLE_ENDIAN_FLOATS = "<{}f"


class Concept(StrEnum):
    # The made-up concepts the stand-in's vectors are made of: one dimension each.
    FERRY = "ferry"
    SNOW = "snow"
    TEA = "tea"
    CLOCK = "clock"


# The words that name each concept, in English, French, Spanish, Finnish, Chinese and
# Japanese. A word of spaced writing counts where it stands as a whole word; a word of Chinese
# or Japanese wherever its characters stand side by side. The Chinese and Japanese words of a
# concept share no character.
CONCEPT_WORDS = {
    Concept.FERRY: ("ferry", "traversier", "transbordador", "lautta", "渡轮", "フェリー"),
    Concept.SNOW: ("snow", "neige", "nieve", "lumi", "雪", "ゆき"),
    Concept.TEA: ("tea", "thé", "té", "tee", "茶", "おちゃ"),
    Concept.CLOCK: ("clock", "horloge", "reloj", "kello", "时钟", "とけい"),
}


def concept_counts(text: str) -> list[int]:
    # How often each concept is named in the text, in the order of Concept.
    words = WORDS.findall(text.casefold())
    return [
        sum(
            text.count(word) if UNSPACED.search(word) else words.count(word)
            for word in CONCEPT_WORDS[concept]
        )
        for concept in Concept
    ]


def unit(values: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in values))
    return [value / length for value in values]


def first_vector(text: str) -> list[float]:
    # One dimension for each concept, by how often the text names it, and one more for a text
    # that names none, so that no vector is zero; of length one, as a real engine's are.
    counts = [float(count) for count in concept_counts(text)]
    return unit([*counts, 0.0 if any(counts) else 1.0])


def second_vector(text: str) -> list[float]:
    # Another model's vectors: the same nearness, in other dimensions and more of them.
    return [*reversed(first_vector(text)), 0.0, 0.0, 0.0]


EMBEDDING_MODEL = "stand-in-embedder"
OTHER_EMBEDDING_MODEL = "stand-in-embedder-2"
MODELS: dict[str, Callable[[str], list[float]]] = {
    EMBEDDING_MODEL: first_vector,
    OTHER_EMBEDDING_MODEL: second_vector,
}


class EmbeddingAsked(BaseModel):
    # One request as the stand-in received it, and the status it answered with.
    model: str
    texts: list[str]
    status: HTTPStatus


class Refusal(Exception):
    def __init__(self, status: HTTPStatus, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    def body(self) -> bytes:
        kind = "invalid_request_error"
        error = {"message": str(self), "type": kind, "param": None, "code": self.code}
        return json.dumps({"error": error}).encode()


def token_count(text: str) -> int:
    return len(TOKENS.findall(text))


def asked_texts(body: dict[str, Any]) -> list[str]:
    # The texts of a request, refused as a real endpoint refuses them.
    texts = body.get("input")
    texts = [texts] if isinstance(texts, str) else texts
    if not isinstance(texts, list) or not texts or len(texts) > MOST_TEXTS:
        raise Refusal(HTTPStatus.BAD_REQUEST, "'$.input' is invalid")
    for text in texts:
        if not isinstance(text, str) or not text:
            raise Refusal(HTTPStatus.BAD_REQUEST, "'$.input' is invalid")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
            raise Refusal(HTTPStatus.BAD_REQUEST, "lone surrogate in a string of the body")
        if token_count(text) > MOST_TOKENS:
            raise Refusal(
                HTTPStatus.BAD_REQUEST,
                f"This model's maximum context length is {MOST_TOKENS} tokens",
            )
    return texts


def encoded(vector: list[float], encoding: str) -> list[float] | str:
    if encoding == BASE64:
        packed = struct.pack(LITTLE_ENDIAN_FLOATS.format(len(vector)), *vector)
        return base64.b64encode(packed).decode()
    return vector


def parsed_body(received: bytes) -> dict[str, Any]:
    try:
        body = json.loads(received)
    except ValueError as error:
        raise Refusal(HTTPStatus.BAD_REQUEST, "the body of the request is not JSON") from error
    if not isinstance(body, dict):
        raise Refusal(HTTPStatus.BAD_REQUEST, "the body of the request is not a JSON object")
    return body


def embeddings_answer(body: dict[str, Any]) -> bytes:
    model = body.get("model")
    if not isinstance(model, str):
        raise Refusal(HTTPStatus.BAD_REQUEST, "you must provide a model parameter")
    if model not in MODELS:
        raise Refusal(
            HTTPStatus.NOT_FOUND, f"The model `{model}` does not exist", "model_not_found"
        )
    encoding = body.get("encoding_format", FLOAT)
    if encoding not in (FLOAT, BASE64):
        raise Refusal(HTTPStatus.BAD_REQUEST, "'encoding_format' must be 'float' or 'base64'")
    texts = asked_texts(body)
    data = [
        {"object": "embedding", "index": index, "embedding": encoded(MODELS[model](text), encoding)}
        for index, text in enumerate(texts)
    ]
    tokens = sum(token_count(text) for text in texts)
    answer = {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
    return json.dumps(answer).encode()


class EmbeddingStandin:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reachable = False
        self.failure: HTTPStatus | None = None
        self.held_words: str | None = None
        self.released = threading.Event()
        self.held_texts: list[str] = []
        self.answered: list[EmbeddingAsked] = []
        self.connections: list[socket.socket] = []

    def reset(self) -> None:
        # Away, answering when brought up, with nothing held and nothing asked; a request that
        # was held is let go.
        self.take_away()
        with self.lock:
            self.failure = None
            self.held_words = None
            self.released.set()
            self.held_texts = []
            self.answered = []

    def bring_up(self) -> None:
        with self.lock:
            self.reachable = True

    def take_away(self) -> None:
        # The connections a client holds open are closed, as a stopped engine's are.
        with self.lock:
            self.reachable = False
            for connection in self.connections:
                if connection.fileno() != CLOSED:
                    connection.shutdown(socket.SHUT_RDWR)
            self.connections = []

    def connected(self, connection: socket.socket) -> None:
        with self.lock:
            self.connections.append(connection)

    def fail_with(self, status: HTTPStatus | None) -> None:
        # Every request is answered with this status, or as it should be when it is None.
        with self.lock:
            self.failure = status

    def hold(self, words: str) -> None:
        # A request that carries a text holding these words waits until it is let go.
        with self.lock:
            self.held_words = words
            self.released = threading.Event()

    def let_go(self) -> None:
        with self.lock:
            self.held_words = None
            self.released.set()

    def asked(self) -> list[EmbeddingAsked]:
        with self.lock:
            return list(self.answered)

    def embedded(self, model: str = EMBEDDING_MODEL) -> list[str]:
        # Every text the stand-in gave a vector for with the model, in the order asked.
        return [
            text
            for asked in self.asked()
            if asked.status == HTTPStatus.OK and asked.model == model
            for text in asked.texts
        ]

    def is_reachable(self) -> bool:
        with self.lock:
            return self.reachable

    def holding(self) -> list[str]:
        # Every text of the requests that were held, whether or not they were let go since.
        with self.lock:
            return list(self.held_texts)

    async def held(self, texts: list[str]) -> None:
        with self.lock:
            words, released = self.held_words, self.released
            holds = words is not None and any(words in text for text in texts)
            if holds:
                self.held_texts += texts
        while holds and not released.is_set():
            await asyncio.sleep(HOLD_POLL_SECONDS)

    def reply(self, scope: Scope, headers: dict[bytes, bytes], body: dict[str, Any]) -> bytes:
        with self.lock:
            failure = self.failure
        if failure is not None:
            raise Refusal(failure, f"the engine failed with {failure.value}")
        if scope["path"] != EMBEDDINGS_PATH or scope["method"] != "POST":
            raise Refusal(HTTPStatus.NOT_FOUND, f"no route {scope['method']} {scope['path']}")
        if headers.get(b"authorization") != f"Bearer {EMBEDDING_KEY}".encode():
            raise Refusal(HTTPStatus.UNAUTHORIZED, "Incorrect API key provided", "invalid_api_key")
        return embeddings_answer(body)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        received = b""
        while True:
            message = await receive()
            received += message.get("body", b"")
            if not message.get("more_body"):
                break
        try:
            body = parsed_body(received)
        except Refusal as refusal:
            await self.respond(
                send, EmbeddingAsked(model="", texts=[], status=refusal.status), refusal.body()
            )
            return
        sent = body.get("input")
        texts = [
            text for text in (sent if isinstance(sent, list) else [sent]) if isinstance(text, str)
        ]
        await self.held(texts)
        try:
            content = self.reply(scope, dict(scope["headers"]), body)
            status = HTTPStatus.OK
        except Refusal as refusal:
            status, content = refusal.status, refusal.body()
        asked = EmbeddingAsked(model=str(body.get("model")), texts=texts, status=status)
        await self.respond(send, asked, content)

    async def respond(self, send: Send, asked: EmbeddingAsked, content: bytes) -> None:
        with self.lock:
            self.answered.append(asked)
        await send({"type": "http.response.start", "status": asked.status, "headers": [JSON_TYPE]})
        await send({"type": "http.response.body", "body": content})


@contextmanager
def embedding_standin() -> Iterator[EmbeddingStandin]:
    # The stand-in served, and every connection made to EMBEDDING_HOST made to it while it is up
    # and refused while it is away, for as long as the context lasts.
    standin = EmbeddingStandin()
    made = socket.create_connection
    with unix_server(standin, lifespan="off") as path, pytest.MonkeyPatch.context() as patch:

        def connection(address: tuple[str, int], *arguments: Any, **options: Any) -> socket.socket:
            if address[0] != EMBEDDING_HOST:
                return made(address, *arguments, **options)
            if not standin.is_reachable():
                raise ConnectionRefusedError(errno.ECONNREFUSED, os.strerror(errno.ECONNREFUSED))
            local = LocalSocket(socket.AF_UNIX, socket.SOCK_STREAM)
            local.settimeout(arguments[0] if arguments else options.get("timeout"))
            local.connect(str(path))
            standin.connected(local)
            return local

        patch.setattr(socket, "create_connection", connection)
        yield standin
