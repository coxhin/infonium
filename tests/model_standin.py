import asyncio
import itertools
import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from http import HTTPStatus
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from starlette.types import Receive, Scope, Send

from infonium.language_model import WireApi
from tests.unix_server import unix_server

# A model endpoint answered in this process, speaking what the real ones speak: the Chat
# Completions API and the Responses API, streamed as server-sent events. It is reached over a
# real HTTP connection: every connection that infonium's clients open to MODEL_HOST is made
# to the stand-in's Unix socket instead. A test tells it what to answer by setting its
# responder; it keeps every request as it arrived, and when each reply ended and how.
MODEL_HOST = "model.test"
MODEL_BASE_URL = f"http://{MODEL_HOST}/v1"
MODEL_KEY = "stand-in-key"
# GLM's endpoint, which the stand-in answers too, at an address of its own.
GLM_BASE_URL = f"http://{MODEL_HOST}/glm/v4"
GLM_KEY = "stand-in-glm-key"
STANDIN_MODEL = "stand-in-model"
TRANSLATION_MODEL = "stand-in-translation-model"
CHAT_PATH = "/v1/chat/completions"
RESPONSES_PATH = "/v1/responses"
GLM_CHAT_PATH = "/glm/v4/chat/completions"
WIRE_PATHS = {
    CHAT_PATH: WireApi.CHAT_COMPLETIONS,
    RESPONSES_PATH: WireApi.RESPONSES,
    GLM_CHAT_PATH: WireApi.CHAT_COMPLETIONS,
}
AUTHORIZATION = b"authorization"
BEARER = "Bearer "
INPUT_TOKENS = 100
OUTPUT_TOKENS = 50
GATE_POLL_SECONDS = 0.01
JSON_TYPE = (b"content-type", b"application/json")
EVENT_STREAM_TYPE = (b"content-type", b"text/event-stream")
CHAT_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
# A line of a stream that is not JSON, and the frames a stream begins with before its first
# piece on either wire API.
GARBLED_LINE = "data: {this is not json\n\n"
BEGINNING_FRAMES = 2
SEVERED_CONNECTION = "the stand-in broke the connection off"
NO_REPLY = "the test told the stand-in model nothing to answer to this request"


class Finish(StrEnum):
    # How a reply's stream ends: as written, at the endpoint's limit of output tokens, stopped
    # by its filter, failed by the endpoint, or broken off with an error event (the Chat
    # Completions API tells a failure only this way). Or it goes wrong as an endpoint or the
    # way to it may: a line that is not JSON after the pieces, or the connection broken off
    # after them.
    STOPPED = "stopped"
    CUT_SHORT = "cut_short"
    FILTERED = "filtered"
    FAILED = "failed"
    BROKEN_OFF = "broken_off"
    GARBLED = "garbled"
    SEVERED = "severed"


class Severed(Exception):
    # Raised to the server, which breaks the connection off in the middle of the response.
    pass


class Call(BaseModel):
    # A call of a tool, with its arguments as the model writes them.
    name: str
    arguments: str


class Reply(BaseModel):
    # What the stand-in streams: text (or a refusal) written piece by piece, then calls of
    # tools, then the end. After the first piece it waits until the gate is open, if it has
    # one; a metered reply says the tokens it took.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    pieces: list[str] = []
    refused: bool = False
    calls: list[Call] = []
    finish: Finish = Finish.STOPPED
    metered: bool = True
    gate: threading.Event | None = None


class Rejection(BaseModel):
    # The endpoint answers the request with a status and a body instead of a stream.
    status: HTTPStatus
    body: str


class Asked(BaseModel):
    # One request as the endpoint received it, counted from 0 since the test began, and the
    # key it was sent with.
    number: int
    path: str
    key: str
    body: dict[str, Any]

    @property
    def wire_api(self) -> WireApi:
        return WIRE_PATHS[self.path]

    def messages(self) -> list[tuple[str, str]]:
        # Every message the request holds, in order, as its role and its text: the
        # instructions, the messages of both sides, and the results of tools as "tool".
        match self.wire_api:
            case WireApi.CHAT_COMPLETIONS:
                return [
                    (message["role"], content_text(message.get("content")))
                    for message in self.body["messages"]
                ]
            case WireApi.RESPONSES:
                instructions = self.body.get("instructions")
                given = [("system", instructions)] if instructions else []
                return [*given, *(response_input_message(entry) for entry in self.body["input"])]

    def text(self) -> str:
        return "\n".join(text for _, text in self.messages())

    def tool_results(self) -> dict[str, str]:
        # The result the request carries for each call of a tool, by the call's identifier.
        match self.wire_api:
            case WireApi.CHAT_COMPLETIONS:
                return {
                    message["tool_call_id"]: content_text(message["content"])
                    for message in self.body["messages"]
                    if message["role"] == "tool"
                }
            case WireApi.RESPONSES:
                return {
                    entry["call_id"]: entry["output"]
                    for entry in self.body["input"]
                    if entry.get("type") == "function_call_output"
                }


type Responder = Callable[[Asked], Reply | Rejection]


def by_request(replies: list[Reply]) -> Callable[[Asked], Reply]:
    # The stand-in answers each request of a reply with the next of the replies, and the last
    # one to every request after.
    return lambda asked: replies[min(asked.number, len(replies) - 1)]


def pieces_of(text: str, cuts: list[int]) -> list[str]:
    # The text cut into pieces at the given places, taken modulo its length; none is empty.
    places = sorted({0, len(text), *(cut % len(text) for cut in cuts)})
    return [text[start:end] for start, end in itertools.pairwise(places)]


class Served(BaseModel):
    # A request and how its reply went: ended once the stand-in sent all it would or the
    # client left, and left if the client closed the connection before the reply's end.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    asked: Asked
    ended: threading.Event
    left: bool = False


def content_text(content: object) -> str:
    match content:
        case str():
            return content
        case list():
            return "".join(part.get("text", "") for part in content)
        case _:
            return ""


def response_input_message(entry: dict[str, Any]) -> tuple[str, str]:
    match entry.get("type", "message"):
        case "message":
            return entry["role"], content_text(entry["content"])
        case "function_call":
            return "assistant", f"{entry['name']}({entry['arguments']})"
        case "function_call_output":
            return "tool", str(entry["output"])
        case other:
            return other, ""


def unstrict_objects(schema: object) -> list[str]:
    # The objects of a schema that a strict schema may not hold: one that allows properties
    # it does not name, or does not require every property it names.
    match schema:
        case dict():
            properties = schema.get("properties")
            named = set(properties) if isinstance(properties, dict) else set()
            loose = bool(named) and (
                schema.get("additionalProperties") is not False
                or set(schema.get("required", [])) != named
            )
            own = [json.dumps(sorted(named))] if loose else []
            return own + [found for value in schema.values() for found in unstrict_objects(value)]
        case list():
            return [found for value in schema for found in unstrict_objects(value)]
        case _:
            return []


def chat_refusal(body: dict[str, Any]) -> str | None:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "'messages' must be a non-empty array"
    unanswered: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role not in CHAT_ROLES:
            return f"invalid role {role!r}"
        if role == "tool":
            if message.get("tool_call_id") not in unanswered:
                return "messages with role 'tool' must be a response to a preceding 'tool_calls'"
            unanswered.discard(message["tool_call_id"])
            continue
        if unanswered:
            return "an assistant message with 'tool_calls' must be followed by tool messages"
        unanswered = {call["id"] for call in message.get("tool_calls") or []}
    form = body.get("response_format", {})
    strict = form.get("json_schema", {})
    return strict_refusal(strict.get("schema"), strict.get("strict"))


def responses_refusal(body: dict[str, Any]) -> str | None:
    entries = body.get("input")
    if not isinstance(entries, list):
        return "this endpoint takes 'input' as a list of items"
    if body.get("store") is not False:
        return "this endpoint keeps no responses: 'store' must be false"
    calls = [entry["call_id"] for entry in entries if entry.get("type") == "function_call"]
    outputs = [entry["call_id"] for entry in entries if entry.get("type") == "function_call_output"]
    if set(outputs) - set(calls):
        return "No tool call found for function call output"
    if set(calls) - set(outputs):
        return "No tool output found for function call"
    form = body.get("text", {}).get("format", {})
    return strict_refusal(form.get("schema"), form.get("strict"))


def strict_refusal(schema: object, strict: object) -> str | None:
    loose = unstrict_objects(schema) if strict else []
    return f"Invalid schema for strict output: {loose}" if loose else None


def request_refusal(asked: Asked) -> str | None:
    # What a real endpoint, or the relay in front of the one infonium uses, refuses with 400.
    if asked.body.get("stream") is not True:
        return "this endpoint answers only streamed requests"
    match asked.wire_api:
        case WireApi.CHAT_COMPLETIONS:
            return chat_refusal(asked.body)
        case WireApi.RESPONSES:
            return responses_refusal(asked.body)


class Frame(BaseModel):
    # One event of a stream; a piece is one that carries a piece of the reply's text.
    event: str
    piece: bool = False


def call_id(asked: Asked, index: int) -> str:
    # Unique among the calls of every request of a test, as a real endpoint's are.
    return f"call_{asked.number}_{index}"


def sse(data: dict[str, Any], name: str | None = None) -> str:
    named = f"event: {name}\n" if name else ""
    return f"{named}data: {json.dumps(data)}\n\n"


def chat_frames(asked: Asked, reply: Reply) -> list[Frame]:
    head = {"id": "chatcmpl-standin", "object": "chat.completion.chunk", "created": 0}
    head["model"] = asked.body["model"]

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        choice = {"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}
        return sse({**head, "choices": [choice]})

    said = "refusal" if reply.refused else "content"
    frames = [Frame(event=chunk({"role": "assistant", "content": ""}))]
    frames += [Frame(event=chunk({said: piece}), piece=True) for piece in reply.pieces]
    for index, call in enumerate(reply.calls):
        opened = {"index": index, "id": call_id(asked, index), "type": "function"}
        opened["function"] = {"name": call.name, "arguments": ""}
        written = {"index": index, "function": {"arguments": call.arguments}}
        frames += [Frame(event=chunk({"tool_calls": [opened]}))]
        frames += [Frame(event=chunk({"tool_calls": [written]}))]
    match reply.finish:
        case Finish.FAILED | Finish.BROKEN_OFF:
            failure = {"message": "The server had an error while processing your request."}
            return [*frames, Frame(event=sse({"error": {**failure, "type": "server_error"}}))]
        case Finish.CUT_SHORT:
            frames += [Frame(event=chunk({}, "length"))]
        case Finish.FILTERED:
            frames += [Frame(event=chunk({}, "content_filter"))]
        case Finish.STOPPED | Finish.GARBLED | Finish.SEVERED:
            frames += [Frame(event=chunk({}, "tool_calls" if reply.calls else "stop"))]
    usage = {"prompt_tokens": INPUT_TOKENS, "completion_tokens": OUTPUT_TOKENS}
    usage["total_tokens"] = INPUT_TOKENS + OUTPUT_TOKENS
    counted = asked.body.get("stream_options", {}).get("include_usage") and reply.metered
    if counted:
        frames += [Frame(event=sse({**head, "choices": [], "usage": usage}))]
    return [*frames, Frame(event="data: [DONE]\n\n")]


class ResponseEvents:
    # The events of one streamed response of the Responses API, numbered as they are sent.
    def __init__(self, asked: Asked) -> None:
        self.asked = asked
        self.sequence = 0
        self.output: list[dict[str, Any]] = []

    def event(self, kind: str, **fields: Any) -> str:
        sent = sse({"type": kind, "sequence_number": self.sequence, **fields}, kind)
        self.sequence += 1
        return sent

    def response(self, status: str, reply: Reply) -> dict[str, Any]:
        usage = {
            "input_tokens": INPUT_TOKENS,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": OUTPUT_TOKENS,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
        }
        return {
            "id": "resp_standin",
            "object": "response",
            "created_at": 0,
            "model": self.asked.body["model"],
            "status": status,
            "output": self.output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": self.asked.body.get("tools", []),
            "instructions": self.asked.body.get("instructions"),
            "text": self.asked.body.get("text"),
            "store": False,
            "error": None,
            "incomplete_details": None,
            "usage": usage if reply.metered and status == "completed" else None,
        }

    def message(self, reply: Reply) -> list[Frame]:
        written = "".join(reply.pieces)
        said, kind = ("refusal", "refusal") if reply.refused else ("text", "output_text")
        part: dict[str, Any] = {"type": kind, said: ""}
        if not reply.refused:
            part["annotations"] = []
        item: dict[str, Any] = {"type": "message", "id": "msg_standin", "role": "assistant"}
        place = {"item_id": "msg_standin", "output_index": 0, "content_index": 0}
        delta = "response.refusal.delta" if reply.refused else "response.output_text.delta"
        done = "response.refusal.done" if reply.refused else "response.output_text.done"
        extra: dict[str, Any] = {} if reply.refused else {"logprobs": []}
        frames = [
            Frame(
                event=self.event(
                    "response.output_item.added",
                    output_index=0,
                    item={**item, "status": "in_progress", "content": []},
                )
            ),
            Frame(event=self.event("response.content_part.added", **place, part=part)),
        ]
        frames += [
            Frame(event=self.event(delta, **place, delta=piece, **extra), piece=True)
            for piece in reply.pieces
        ]
        whole = {**part, said: written}
        finished = {**item, "status": "completed", "content": [whole]}
        self.output.append(finished)
        return [
            *frames,
            Frame(event=self.event(done, **place, **{said: written}, **extra)),
            Frame(event=self.event("response.content_part.done", **place, part=whole)),
            Frame(event=self.event("response.output_item.done", output_index=0, item=finished)),
        ]

    def calls(self, reply: Reply) -> list[Frame]:
        frames: list[Frame] = []
        for number, call in enumerate(reply.calls):
            index = len(self.output)
            item = {"type": "function_call", "id": f"fc_{index}"}
            item["call_id"] = call_id(self.asked, number)
            item["name"] = call.name
            place = {"item_id": f"fc_{index}", "output_index": index}
            finished = {**item, "arguments": call.arguments, "status": "completed"}
            self.output.append(finished)
            frames += [
                Frame(
                    event=self.event(
                        "response.output_item.added",
                        output_index=index,
                        item={**item, "arguments": "", "status": "in_progress"},
                    )
                ),
                Frame(
                    event=self.event(
                        "response.function_call_arguments.delta", **place, delta=call.arguments
                    )
                ),
                Frame(
                    event=self.event(
                        "response.function_call_arguments.done",
                        **place,
                        name=call.name,
                        arguments=call.arguments,
                    )
                ),
                Frame(
                    event=self.event("response.output_item.done", output_index=index, item=finished)
                ),
            ]
        return frames

    def frames(self, reply: Reply) -> list[Frame]:
        created = self.response("in_progress", reply)
        frames = [
            Frame(event=self.event("response.created", response=created)),
            Frame(event=self.event("response.in_progress", response=created)),
        ]
        if reply.pieces:
            frames += self.message(reply)
        frames += self.calls(reply)
        match reply.finish:
            # A garbled or severed reply is first written as it would stop; reply_frames
            # makes it go wrong.
            case Finish.STOPPED | Finish.GARBLED | Finish.SEVERED:
                ended = self.response("completed", reply)
                return [*frames, Frame(event=self.event("response.completed", response=ended))]
            case Finish.CUT_SHORT | Finish.FILTERED:
                reason = (
                    "max_output_tokens" if reply.finish == Finish.CUT_SHORT else "content_filter"
                )
                ended = self.response("incomplete", reply)
                ended["incomplete_details"] = {"reason": reason}
                return [*frames, Frame(event=self.event("response.incomplete", response=ended))]
            case Finish.FAILED:
                ended = self.response("failed", reply)
                ended["error"] = {"code": "server_error", "message": "The model failed."}
                return [*frames, Frame(event=self.event("response.failed", response=ended))]
            case Finish.BROKEN_OFF:
                broken = {
                    "code": "server_error",
                    "message": "The server had an error.",
                    "param": None,
                }
                return [*frames, Frame(event=self.event("error", **broken))]


def wire_frames(asked: Asked, reply: Reply) -> list[Frame]:
    match asked.wire_api:
        case WireApi.CHAT_COMPLETIONS:
            return chat_frames(asked, reply)
        case WireApi.RESPONSES:
            return ResponseEvents(asked).frames(reply)


def reply_frames(asked: Asked, reply: Reply) -> list[Frame]:
    # A reply that goes wrong is the reply as written as far as its last piece, then a line
    # that is not JSON where it is garbled; a severed one is broken off by the stream.
    match reply.finish:
        case Finish.GARBLED | Finish.SEVERED:
            whole = wire_frames(asked, reply.model_copy(update={"finish": Finish.STOPPED}))
            pieces = [number for number, frame in enumerate(whole) if frame.piece]
            begun = whole[: pieces[-1] + 1 if pieces else BEGINNING_FRAMES]
            garbled = [Frame(event=GARBLED_LINE)] if reply.finish == Finish.GARBLED else []
            return [*begun, *garbled]
        case _:
            return wire_frames(asked, reply)


def refused_reply(asked: Asked) -> Rejection:
    return Rejection(status=HTTPStatus.INTERNAL_SERVER_ERROR, body=NO_REPLY)


class ModelStandin:
    def __init__(self) -> None:
        self.responder: Responder = refused_reply
        self.served: list[Served] = []
        self.lock = threading.Lock()

    def reset(self, responder: Responder) -> None:
        with self.lock:
            self.responder = responder
            self.served = []

    def asked(self) -> list[Asked]:
        with self.lock:
            return [served.asked for served in self.served]

    def requests(self) -> list[Served]:
        with self.lock:
            return list(self.served)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        authorization = dict(scope["headers"]).get(AUTHORIZATION, b"").decode()
        with self.lock:
            asked = Asked(
                number=len(self.served),
                path=scope["path"],
                key=authorization.removeprefix(BEARER),
                body=json.loads(body),
            )
            served = Served(asked=asked, ended=threading.Event())
            self.served.append(served)
            responder = self.responder
        refusal = request_refusal(asked)
        answer = (
            Rejection(
                status=HTTPStatus.BAD_REQUEST,
                body=json.dumps({"error": {"message": refusal, "type": "invalid_request_error"}}),
            )
            if refusal
            else responder(asked)
        )
        try:
            match answer:
                case Rejection():
                    await send(
                        {
                            "type": "http.response.start",
                            "status": answer.status,
                            "headers": [JSON_TYPE],
                        }
                    )
                    await send({"type": "http.response.body", "body": answer.body.encode()})
                case Reply():
                    served.left = not await self.stream(
                        answer, reply_frames(asked, answer), receive, send
                    )
        finally:
            served.ended.set()

    async def stream(self, reply: Reply, frames: list[Frame], receive: Receive, send: Send) -> bool:
        # Sends the frames, and tells whether all were sent before the client left.
        left = asyncio.Event()

        async def watch() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass
            left.set()

        watching = asyncio.create_task(watch())
        await send(
            {"type": "http.response.start", "status": HTTPStatus.OK, "headers": [EVENT_STREAM_TYPE]}
        )
        first_piece = next((frame for frame in frames if frame.piece), None)
        for frame in frames:
            if left.is_set():
                break
            await send(
                {"type": "http.response.body", "body": frame.event.encode(), "more_body": True}
            )
            while frame is first_piece and reply.gate and not reply.gate.is_set():
                if left.is_set():
                    break
                await asyncio.sleep(GATE_POLL_SECONDS)
        if reply.finish == Finish.SEVERED and not left.is_set():
            watching.cancel()
            raise Severed(SEVERED_CONNECTION)
        whole = not left.is_set()
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        watching.cancel()
        return whole


class LocalSocket(socket.socket):
    # A Unix socket in the place of a TCP connection: the options of TCP mean nothing to it.
    def setsockopt(self, level: int, option: int, value: Any, *rest: Any) -> None:
        if level != socket.IPPROTO_TCP:
            super().setsockopt(level, option, value, *rest)


@contextmanager
def model_standin() -> Iterator[ModelStandin]:
    # The stand-in served, and every connection made to MODEL_HOST made to it, for as long as
    # the context lasts. A connection to any other host is made as before, which the default
    # run refuses.
    standin = ModelStandin()
    made = socket.create_connection
    with unix_server(standin, lifespan="off") as path, pytest.MonkeyPatch.context() as patch:

        def connection(address: tuple[str, int], *arguments: Any, **options: Any) -> socket.socket:
            if address[0] != MODEL_HOST:
                return made(address, *arguments, **options)
            local = LocalSocket(socket.AF_UNIX, socket.SOCK_STREAM)
            local.settimeout(arguments[0] if arguments else options.get("timeout"))
            local.connect(str(path))
            return local

        patch.setattr(socket, "create_connection", connection)
        yield standin
