import json
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Generator, Sequence
from enum import StrEnum
from functools import partial
from typing import Any, Final, Literal

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    Omit,
    OpenAI,
    OpenAIError,
    Stream,
    omit,
    pydantic_function_tool,
    pydantic_responses_function_tool,
)
from openai.types.chat import ChatCompletionMessageParam
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
from openai.types.chat.completion_create_params import ResponseFormat
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseError,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionToolCall,
    ResponseIncompleteEvent,
    ResponseInputItemParam,
    ResponseOutputItemDoneEvent,
    ResponseRefusalDoneEvent,
    ResponseTextDeltaEvent,
    ResponseTextDoneEvent,
)
from openai.types.responses.response import IncompleteDetails
from openai.types.shared import ReasoningEffort
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from infonium.model_connection import (
    Ending,
    Pace,
    endable_http_client,
    endable_paced_http_client,
    paced_http_client,
)

logger = logging.getLogger(__name__)

# The longest silence the endpoint is allowed: before a whole reply that is not streamed, and
# between two events of one that is.
MODEL_TIMEOUT_SECONDS = 180.0
MODEL_RETRIES = 1
# The most requests one reply may take: each round of tool calls costs one request more.
REPLY_ROUNDS = 8
NO_PARSED_OUTPUT = "the model returned no parsed output"
NO_TOKEN_USAGE = "the endpoint reported no token usage"
INCOMPLETE_REPLY = "the endpoint cut the reply short: {reason}"
UNEXPECTED_REPLY = "the model's reply is not of the form asked for: {error}"
UNREADABLE_EVENT = "the endpoint sent a part of the reply that is not JSON: {error}"
# A reply held to JSON that writes this many white-space characters in a row outside its
# strings has run away: between two of its tokens, before the first or after the last.
# Through the relay, gpt-6.1-terra and gpt-6-luna at times wrote white space without end
# where a field fell due after the list that held a section's statements, until the relay
# ended the reply after about 15 minutes. A reply that has closed its value and then writes
# white space without end would run on as long, so it is ended and sent again too. JSON set
# out with indents writes a few dozen in a row at most.
RUNAWAY_WHITE_SPACE = 300
# The white space JSON allows outside its strings. Inside a string only the space stands
# unescaped, and a run of spaces there is the string's text.
JSON_WHITE_SPACE = frozenset(" \t\n\r")
RAN_AWAY = (
    "the model wrote white space without end: its reply was ended after {characters} "
    "white-space characters in a row"
)
RUNAWAY_ENDED = (
    "%s wrote %d white-space characters in a row outside the strings of its JSON, after %d "
    "characters in %.1f s: it is ended, %s"
)
SENT_AGAIN = "and sent again"
NO_ATTEMPT_LEFT = "with no attempt left"
# Where a request tells the endpoint how many times the openai client has sent it again.
RETRY_COUNT_HEADER = "x-stainless-retry-count"
# The most calls of tools run in one round; the model is told so of the calls beyond.
MOST_CALLS_PER_ROUND = 4
ENDLESS_TOOL_USE = "the model gave no answer within the {rounds} requests a reply may take"
CALL_NOT_RUN = (
    "This call was not run: at most {most} calls run in one round. Make it again in the next "
    "round if you still need it."
)
# The last request of a reply with tools offers none, so that the model answers with what it
# has; it is told so in a message of its own.
LAST_REQUEST = (
    "This is the last request of your reply: no tool can be called any more. Answer now with "
    "what you have found."
)
TOOLS_OFFERED: Final = "auto"
NO_TOOLS: Final = "none"
REQUEST_NAME = "request {request} of a reply from {model}"
REQUEST_BEGAN = "%s began"
REQUEST_OPENED = "%s: the endpoint answered after %.1f s"
REQUEST_FIRST_EVENT = "%s: its first event came after %.1f s"
OPENED_STEP = "answered after {seconds:.1f} s"
FIRST_EVENT_STEP = "first event after {seconds:.1f} s"
UNANSWERED_STEP = "no answer from the endpoint"
STEPS_GAP = ", "
REQUEST_ENDED = (
    "%s ended after %.1f s (%s), %d events, the longest silence between two of them %.1f s: "
    "%s, %d input and %d output tokens"
)
REQUEST_FAILED = (
    "%s ended after %.1f s (%s), %d events, the longest silence between two of them %.1f s, "
    "without a reply: %s"
)
REPLY_CAME = "the reply"
CALLS_CAME = "{calls} calls of tools"
NO_SUCH_TOOL = "There is no tool named {name}. The tools are: {names}."
WRONG_ARGUMENTS = "The arguments do not fit the tool {name}: {error}"
# The finishes of a Chat Completions reply that mean it was not written whole.
CUT_FINISHES = frozenset({"length", "content_filter"})
SYSTEM_ROLE: Final = "system"
USER_ROLE: Final = "user"
ASSISTANT_ROLE: Final = "assistant"
TOOL_ROLE: Final = "tool"
FUNCTION_TOOL: Final = "function"
JSON_SCHEMA: Final = "json_schema"
JSON_OBJECT: Final = "json_object"
# GLM's endpoint holds a reply to JSON but not to a schema: asked for a reply to a schema alone,
# glm-5.3 answered [1]. So its model is told the reply's form after its instructions, by the
# form's name and schema.
FORM_TOLD = (
    "\n\nReply with one JSON object of the form {name}, and nothing else. It follows this "
    "JSON schema:\n{schema}"
)
INPUT_TEXT: Final = "input_text"
MESSAGE_ITEM: Final = "message"
FUNCTION_CALL_ITEM: Final = "function_call"
FUNCTION_OUTPUT_ITEM: Final = "function_call_output"


# How much a model of GLM's endpoint may be asked to reason, as the endpoint names it.
type GlmReasoning = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class WireApi(StrEnum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


class Side(StrEnum):
    ASKER = "asker"
    MODEL = "model"


class UnstructuredCompletion(Exception):
    pass


class UnmeteredCompletion(Exception):
    pass


class RunawayReply(UnstructuredCompletion):
    # A reply ended for writing white space without end, with how many characters it had
    # written, and how many times the openai client had sent its request again before.
    def __init__(self, written: int, retries_taken: int) -> None:
        super().__init__(RAN_AWAY.format(characters=RUNAWAY_WHITE_SPACE))
        self.written = written
        self.retries_taken = retries_taken


class WhiteSpaceRun:
    # Reads a reply held to JSON as it streams, piece by piece wherever the pieces are cut,
    # and tells when it has written RUNAWAY_WHITE_SPACE white-space characters in a row
    # outside its strings. A quote opens or closes a string unless a backslash escapes it.
    def __init__(self) -> None:
        self.length = 0
        self.in_string = False
        self.escaped = False

    def ran_away(self, piece: str) -> bool:
        for character in piece:
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif character == "\\":
                    self.escaped = True
                elif character == '"':
                    self.in_string = False
            elif character in JSON_WHITE_SPACE:
                self.length += 1
                if self.length >= RUNAWAY_WHITE_SPACE:
                    return True
                continue
            else:
                self.in_string = character == '"'
            self.length = 0
        return False


# What a call of the model raises when the model or its endpoint fails it: a request that
# failed, as the openai package raises it, and a reply that was refused, cut short, failed,
# not of the form asked for, not readable, or not metered. Anything else that a call raises is
# a failure of infonium's own.
MODEL_FAILURES = (OpenAIError, UnstructuredCompletion, UnmeteredCompletion)


class Message(BaseModel):
    # One message of a conversation with a model, and which side wrote it.
    side: Side
    text: str


class Tool[Arguments: BaseModel](BaseModel):
    # A function the model may call while it replies. The description and the schema of the
    # arguments are all the model knows of it; what the function returns goes back to the
    # model as the call's result.
    name: str
    description: str
    arguments: type[Arguments]
    run: Callable[[Arguments], str]


class StructuredCompletion[Output: BaseModel](BaseModel):
    output: Output
    input_tokens: int
    output_tokens: int


class Written(BaseModel):
    # What the model has written so far in one request of a reply, counted from 0. Text of a
    # request in which the model goes on to call tools is not the reply; only that of the
    # last request is, and for a structured reply it is unfinished JSON. A request sent again
    # after its reply ran away is written anew from its beginning.
    request: int
    text: str


class Called(BaseModel):
    # The model called a tool with these arguments. It is handed on before the tool runs.
    tool: str
    arguments: BaseModel


class Replied[Output: BaseModel](BaseModel):
    # The whole reply, with the tokens of every request it took. It is the last event of a
    # reply.
    completion: StructuredCompletion[Output]


type ReplyEvent[Output: BaseModel] = Written | Called | Replied[Output]


class Miscalled(BaseModel):
    # A call the model got wrong: a tool that does not exist, or arguments that do not fit.
    # What went wrong is the call's result, so that the model may call again.
    result: str


class ToolCall(BaseModel):
    # A call of a tool as the endpoint gives it.
    call_id: str
    name: str
    arguments: str


class Spoke[Output: BaseModel](BaseModel):
    # A request that brought the reply itself.
    output: Output


class CalledTools(BaseModel):
    # A request that brought calls of tools in place of the reply; any text beside them is
    # not the reply.
    calls: list[ToolCall]


class Answered[Output: BaseModel](BaseModel):
    reply: Spoke[Output] | CalledTools
    input_tokens: int
    output_tokens: int


class Tokens(BaseModel):
    input_tokens: int
    output_tokens: int


NO_TOKENS = Tokens(input_tokens=0, output_tokens=0)


def tokens_after(spent: Tokens, input_tokens: int, output_tokens: int) -> Tokens:
    return Tokens(
        input_tokens=spent.input_tokens + input_tokens,
        output_tokens=spent.output_tokens + output_tokens,
    )


def replied[Output: BaseModel](output: Output, spent: Tokens) -> Replied[Output]:
    return Replied(
        completion=StructuredCompletion(
            output=output, input_tokens=spent.input_tokens, output_tokens=spent.output_tokens
        )
    )


def reply_schema(output: type[BaseModel]) -> dict[str, object]:
    # The openai package offers the strict schema it derives from a model only for a tool's
    # arguments; a reply of that form takes the same schema.
    return pydantic_function_tool(output)["function"]["parameters"]


def parsed_reply[Output: BaseModel](output: type[Output], text: str) -> Output:
    # A reply that is not of the form asked for is the model's failure, as a refusal is.
    try:
        return output.model_validate_json(text)
    except ValidationError as error:
        raise UnstructuredCompletion(UNEXPECTED_REPLY.format(error=error)) from error


def tool_call(tools: Sequence[Tool[Any]], call: ToolCall) -> Called | Miscalled:
    named = [tool for tool in tools if tool.name == call.name]
    if not named:
        names = ", ".join(tool.name for tool in tools)
        return Miscalled(result=NO_SUCH_TOOL.format(name=call.name, names=names))
    try:
        arguments = named[0].arguments.model_validate_json(call.arguments)
    except ValidationError as error:
        return Miscalled(result=WRONG_ARGUMENTS.format(name=call.name, error=error))
    return Called(tool=call.name, arguments=arguments)


def tool_results(
    tools: Sequence[Tool[Any]], calls: list[ToolCall]
) -> Generator[Called, None, list[str]]:
    # Runs the calls in order, as many as one round runs, handing on each before its tool runs,
    # and gives their results. A tool that raises is infonium's failure, not the model's, and
    # ends the reply.
    results: list[str] = []
    for call in calls[:MOST_CALLS_PER_ROUND]:
        match tool_call(tools, call):
            case Called() as called:
                yield called
                tool = next(tool for tool in tools if tool.name == called.tool)
                results.append(tool.run(called.arguments))
            case Miscalled(result=result):
                results.append(result)
    not_run = CALL_NOT_RUN.format(most=MOST_CALLS_PER_ROUND)
    return [*results, *(not_run for _ in calls[MOST_CALLS_PER_ROUND:])]


class RequestClock:
    # What one request of a reply took, told to the log in one line when it ends: when the
    # endpoint answered and when its first event came, how long it took, how many events came
    # and the longest silence between two of them, and the tokens it used or the failure it
    # ended with. Its beginning and its first steps are told as they come at DEBUG. The openai
    # client's own retries are told to the log by the client.
    def __init__(self, model: str, request: int) -> None:
        self.request = REQUEST_NAME.format(request=request, model=model)
        self.began = time.monotonic()
        self.events = 0
        self.last_event = self.began
        self.longest_silence = 0.0
        self.steps: list[str] = []
        logger.debug(REQUEST_BEGAN, self.request)

    def since(self) -> float:
        return time.monotonic() - self.began

    def opened(self) -> None:
        self.steps.append(OPENED_STEP.format(seconds=self.since()))
        logger.debug(REQUEST_OPENED, self.request, self.since())

    def summary(self) -> str:
        return STEPS_GAP.join(self.steps) or UNANSWERED_STEP

    def came(self) -> None:
        now = time.monotonic()
        self.events += 1
        if self.events == 1:
            self.steps.append(FIRST_EVENT_STEP.format(seconds=now - self.began))
            logger.debug(REQUEST_FIRST_EVENT, self.request, now - self.began)
        else:
            self.longest_silence = max(self.longest_silence, now - self.last_event)
        self.last_event = now

    def timed[Output: BaseModel](
        self, steps: Generator[Written, None, Answered[Output]]
    ) -> Generator[Written, None, Answered[Output]]:
        try:
            answered = yield from steps
        except BaseException as failure:
            logger.info(
                REQUEST_FAILED,
                self.request,
                self.since(),
                self.summary(),
                self.events,
                self.longest_silence,
                type(failure).__name__,
            )
            raise
        match answered.reply:
            case Spoke():
                outcome = REPLY_CAME
            case CalledTools(calls=calls):
                outcome = CALLS_CAME.format(calls=len(calls))
        logger.info(
            REQUEST_ENDED,
            self.request,
            self.since(),
            self.summary(),
            self.events,
            self.longest_silence,
            outcome,
            answered.input_tokens,
            answered.output_tokens,
        )
        return answered


def is_last_request(request: int, tools: Sequence[Tool[Any]]) -> bool:
    return bool(tools) and request == REPLY_ROUNDS - 1


def retries_taken(stream: Stream[Any]) -> int:
    # How many times the openai client sent a request again, after failures, before the
    # endpoint took it, as the client told the endpoint.
    return int(stream.response.request.headers.get(RETRY_COUNT_HEADER, 0))


# One sending of a request of a reply by the given client, timed by the given clock.
type Sending[Output: BaseModel] = Callable[
    [OpenAI, RequestClock], Generator[Written, None, Answered[Output]]
]


class LanguageModel(ABC):
    def __init__(self, client: OpenAI, name: str) -> None:
        self.client = client
        self.name = name

    @abstractmethod
    def replying[Output: BaseModel](
        self,
        instructions: str,
        messages: Sequence[Message],
        output: type[Output],
        tools: Sequence[Tool[Any]],
        ending: Ending,
    ) -> Generator[ReplyEvent[Output]]:
        # A reply of the given form to a conversation whose messages are in the order they
        # were written. The reply is handed on as far as it is written, as often as more
        # arrives; each call of a tool before the tool runs, its result going back to the
        # model; and last the whole reply. Every failure of the model or of its reply raises
        # UnstructuredCompletion or UnmeteredCompletion, or the openai package's error of a
        # request that failed.
        ...

    def attempted[Output: BaseModel](
        self, request: int, sending: Sending[Output]
    ) -> Generator[Written, None, Answered[Output]]:
        # One request of a reply. A reply that runs away is ended, its stream closed, and its
        # request sent again for as long as the request has attempts left: those of the
        # client, whose own sending again of a request that failed spends them too.
        client = self.client
        while True:
            clock = RequestClock(self.name, request)
            try:
                return (yield from clock.timed(sending(client, clock)))
            except RunawayReply as runaway:
                left = client.max_retries - runaway.retries_taken
                logger.warning(
                    RUNAWAY_ENDED,
                    clock.request,
                    RUNAWAY_WHITE_SPACE,
                    runaway.written,
                    clock.since(),
                    SENT_AGAIN if left > 0 else NO_ATTEMPT_LEFT,
                )
                if left <= 0:
                    raise
                client = client.with_options(max_retries=left - 1)

    def structured[Output: BaseModel](
        self, instructions: str, prompt: str, output: type[Output]
    ) -> StructuredCompletion[Output]:
        events = self.replying(
            instructions, [Message(side=Side.ASKER, text=prompt)], output, [], Ending()
        )
        return next(event.completion for event in events if isinstance(event, Replied))


def chat_message(message: Message) -> ChatCompletionMessageParam:
    match message.side:
        case Side.ASKER:
            return {"role": USER_ROLE, "content": message.text}
        case Side.MODEL:
            return {"role": ASSISTANT_ROLE, "content": message.text}


def chat_tool_calls(pieces: dict[int, list[ChoiceDeltaToolCall]]) -> list[ToolCall]:
    # A call arrives in pieces under its index: the first names it, the rest add arguments.
    return [
        ToolCall(
            call_id="".join(piece.id or "" for piece in parts),
            name="".join(piece.function.name or "" for piece in parts if piece.function),
            arguments="".join(piece.function.arguments or "" for piece in parts if piece.function),
        )
        for _, parts in sorted(pieces.items())
    ]


class ChatCompletionsLanguageModel(LanguageModel):
    # How much the model is asked to reason before it replies; by default, as much as the
    # endpoint has it reason.
    reasoning_effort: ReasoningEffort | Omit = omit

    def system_text(self, instructions: str, output: type[BaseModel]) -> str:
        return instructions

    def reply_format(self, output: type[BaseModel], searching: bool) -> ResponseFormat | Omit:
        # How the request holds the reply to its form; searching, it is the first request of
        # a reply that lets the model call tools.
        return {
            "type": JSON_SCHEMA,
            "json_schema": {
                "name": output.__name__,
                "schema": reply_schema(output),
                "strict": True,
            },
        }

    def replying[Output: BaseModel](
        self,
        instructions: str,
        messages: Sequence[Message],
        output: type[Output],
        tools: Sequence[Tool[Any]],
        ending: Ending,
    ) -> Generator[ReplyEvent[Output]]:
        sent: list[ChatCompletionMessageParam] = [
            {"role": SYSTEM_ROLE, "content": self.system_text(instructions, output)},
            *(chat_message(message) for message in messages),
        ]
        spent = NO_TOKENS
        for request in range(REPLY_ROUNDS):
            last = is_last_request(request, tools)
            if last:
                sent.append(chat_message(Message(side=Side.ASKER, text=LAST_REQUEST)))
            offered: Literal["auto", "none"] = NO_TOOLS if last else TOOLS_OFFERED
            answered = yield from self.attempted(
                request, partial(self.round, request, sent, output, tools, offered, ending)
            )
            spent = tokens_after(spent, answered.input_tokens, answered.output_tokens)
            match answered.reply:
                case Spoke(output=parsed):
                    yield replied(parsed, spent)
                    return
                case CalledTools() if last:
                    raise UnstructuredCompletion(ENDLESS_TOOL_USE.format(rounds=REPLY_ROUNDS))
                case CalledTools(calls=calls):
                    sent.append(
                        {
                            "role": ASSISTANT_ROLE,
                            "tool_calls": [
                                {
                                    "id": call.call_id,
                                    "type": FUNCTION_TOOL,
                                    "function": {"name": call.name, "arguments": call.arguments},
                                }
                                for call in calls
                            ],
                        }
                    )
                    results = yield from tool_results(tools, calls)
                    sent.extend(
                        {"role": TOOL_ROLE, "tool_call_id": call.call_id, "content": result}
                        for call, result in zip(calls, results, strict=True)
                    )
        raise UnstructuredCompletion(ENDLESS_TOOL_USE.format(rounds=REPLY_ROUNDS))

    def round[Output: BaseModel](
        self,
        request: int,
        sent: list[ChatCompletionMessageParam],
        output: type[Output],
        tools: Sequence[Tool[Any]],
        offered: Literal["auto", "none"],
        ending: Ending,
        client: OpenAI,
        clock: RequestClock,
    ) -> Generator[Written, None, Answered[Output]]:
        content = ""
        refusal = ""
        finish = ""
        pieces: dict[int, list[ChoiceDeltaToolCall]] = defaultdict(list)
        usage = None
        form = self.reply_format(output, bool(tools) and offered == TOOLS_OFFERED and request == 0)
        # A reply that the request does not hold to JSON may be prose, and is not read for
        # white space without end.
        held_to_json = not isinstance(form, Omit)
        white_space = WhiteSpaceRun()
        # The reply is parsed here, not by the openai package, so that a reply cut short or
        # not of the form asked for fails as the model's failure.
        with ending.requesting():
            stream = client.chat.completions.create(
                model=self.name,
                messages=sent,
                response_format=form,
                reasoning_effort=self.reasoning_effort,
                tools=[
                    pydantic_function_tool(
                        tool.arguments, name=tool.name, description=tool.description
                    )
                    for tool in tools
                ]
                or omit,
                tool_choice=offered if tools else omit,
                stream=True,
                stream_options={"include_usage": True},
            )
        clock.opened()
        # An endpoint that fails the reply in the middle of its stream says so in a data line
        # that holds an error object, which the openai package raises as its bare APIError:
        # the model's failure, as a failed event of the Responses API is. Its subclasses are
        # failures of the request, and stay what they are. A data line that is not JSON at all,
        # which the package raises as the decoding error, is the endpoint's failure too.
        try:
            with stream:
                for chunk in stream:
                    clock.came()
                    usage = chunk.usage or usage
                    for choice in chunk.choices[:1]:
                        refusal += choice.delta.refusal or ""
                        finish = choice.finish_reason or finish
                        for piece in choice.delta.tool_calls or []:
                            pieces[piece.index].append(piece)
                        if choice.delta.content:
                            content += choice.delta.content
                            if held_to_json and white_space.ran_away(choice.delta.content):
                                raise RunawayReply(len(content), retries_taken(stream))
                            yield Written(request=request, text=content)
        except (APIStatusError, APIConnectionError):
            raise
        except APIError as failed:
            raise UnstructuredCompletion(failed.message) from failed
        except json.JSONDecodeError as failed:
            raise UnstructuredCompletion(UNREADABLE_EVENT.format(error=failed)) from failed
        if refusal:
            raise UnstructuredCompletion(refusal)
        if finish in CUT_FINISHES:
            raise UnstructuredCompletion(INCOMPLETE_REPLY.format(reason=finish))
        reply = (
            CalledTools(calls=chat_tool_calls(pieces))
            if pieces
            else Spoke(output=parsed_reply(output, content))
        )
        if usage is None:
            raise UnmeteredCompletion(NO_TOKEN_USAGE)
        return Answered(
            reply=reply,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
        )


class GlmLanguageModel(ChatCompletionsLanguageModel):
    # A model of GLM's endpoint, which speaks the Chat Completions API. The reply is held to
    # its form as it is read, as every reply is. GLM's models always reason.
    def __init__(self, client: OpenAI, name: str, reasoning_effort: GlmReasoning | Omit) -> None:
        super().__init__(client=client, name=name)
        self.reasoning_effort = reasoning_effort

    def system_text(self, instructions: str, output: type[BaseModel]) -> str:
        schema = json.dumps(reply_schema(output), ensure_ascii=False)
        return instructions + FORM_TOLD.format(name=output.__name__, schema=schema)

    def reply_format(self, output: type[BaseModel], searching: bool) -> ResponseFormat | Omit:
        # Held to JSON, glm-5.3-flash called no tool in 3 tries of 3, and wrote what it would
        # search for instead; not held, it called tools, but after their results answered in
        # prose in 2 replies of 3. So the first request of a reply that offers tools leaves the
        # model to the form it is told, and every later one holds it to JSON.
        return omit if searching else {"type": JSON_OBJECT}


def responses_message(message: Message) -> ResponseInputItemParam:
    match message.side:
        case Side.ASKER:
            return {
                "type": MESSAGE_ITEM,
                "role": USER_ROLE,
                "content": [{"type": INPUT_TEXT, "text": message.text}],
            }
        case Side.MODEL:
            return {"type": MESSAGE_ITEM, "role": ASSISTANT_ROLE, "content": message.text}


class ResponsesLanguageModel(LanguageModel):
    def replying[Output: BaseModel](
        self,
        instructions: str,
        messages: Sequence[Message],
        output: type[Output],
        tools: Sequence[Tool[Any]],
        ending: Ending,
    ) -> Generator[ReplyEvent[Output]]:
        sent = [responses_message(message) for message in messages]
        spent = NO_TOKENS
        for request in range(REPLY_ROUNDS):
            last = is_last_request(request, tools)
            if last:
                sent.append(responses_message(Message(side=Side.ASKER, text=LAST_REQUEST)))
            offered: Literal["auto", "none"] = NO_TOOLS if last else TOOLS_OFFERED
            answered = yield from self.attempted(
                request,
                partial(self.round, request, instructions, sent, output, tools, offered, ending),
            )
            spent = tokens_after(spent, answered.input_tokens, answered.output_tokens)
            match answered.reply:
                case Spoke(output=parsed):
                    yield replied(parsed, spent)
                    return
                case CalledTools() if last:
                    raise UnstructuredCompletion(ENDLESS_TOOL_USE.format(rounds=REPLY_ROUNDS))
                case CalledTools(calls=calls):
                    results = yield from tool_results(tools, calls)
                    for call, result in zip(calls, results, strict=True):
                        sent.append(
                            {
                                "type": FUNCTION_CALL_ITEM,
                                "call_id": call.call_id,
                                "name": call.name,
                                "arguments": call.arguments,
                            }
                        )
                        sent.append(
                            {
                                "type": FUNCTION_OUTPUT_ITEM,
                                "call_id": call.call_id,
                                "output": result,
                            }
                        )
        raise UnstructuredCompletion(ENDLESS_TOOL_USE.format(rounds=REPLY_ROUNDS))

    def round[Output: BaseModel](
        self,
        request: int,
        instructions: str,
        sent: list[ResponseInputItemParam],
        output: type[Output],
        tools: Sequence[Tool[Any]],
        offered: Literal["auto", "none"],
        ending: Ending,
        client: OpenAI,
        clock: RequestClock,
    ) -> Generator[Written, None, Answered[Output]]:
        texts: dict[str, str] = defaultdict(str)
        # Each message of the reply is read for white space without end by itself: every one
        # is held to the reply's schema.
        white_space: dict[str, WhiteSpaceRun] = defaultdict(WhiteSpaceRun)
        done: list[str] = []
        calls: list[ToolCall] = []
        usage = None
        # A relay may forward the request to ChatGPT's Codex backend, whose clients stream
        # and send the input as a list, so it takes that form. Nothing reads the reply at the
        # endpoint again, so it is not stored there, and a call of a tool goes back to the
        # model with the conversation sent again in full. The reply is parsed here, after
        # the events that say it was refused, failed or cut short.
        with ending.requesting():
            stream = client.responses.create(
                model=self.name,
                instructions=instructions,
                input=sent,
                text={
                    "format": {
                        "type": JSON_SCHEMA,
                        "name": output.__name__,
                        "schema": reply_schema(output),
                        "strict": True,
                    }
                },
                tools=[
                    pydantic_responses_function_tool(
                        tool.arguments, name=tool.name, description=tool.description
                    )
                    for tool in tools
                ]
                or omit,
                tool_choice=offered if tools else omit,
                store=False,
                stream=True,
            )
        clock.opened()
        # A data line that the openai package cannot read as JSON is the endpoint's failure.
        try:
            with stream:
                # The reply is read from the events that deliver it: such a backend may end the
                # stream with a response whose output is empty.
                for event in stream:
                    clock.came()
                    match event:
                        case ResponseTextDeltaEvent(item_id=item, delta=delta):
                            texts[item] += delta
                            if white_space[item].ran_away(delta):
                                raise RunawayReply(len(texts[item]), retries_taken(stream))
                            yield Written(request=request, text=texts[item])
                        case ResponseTextDoneEvent(text=text):
                            done.append(text)
                        case ResponseOutputItemDoneEvent(item=ResponseFunctionToolCall() as call):
                            calls.append(
                                ToolCall(
                                    call_id=call.call_id, name=call.name, arguments=call.arguments
                                )
                            )
                        case ResponseCompletedEvent():
                            usage = event.response.usage
                        case ResponseRefusalDoneEvent():
                            raise UnstructuredCompletion(event.refusal)
                        case ResponseErrorEvent():
                            raise UnstructuredCompletion(event.message)
                        case ResponseFailedEvent(
                            response=Response(error=ResponseError(message=reason))
                        ):
                            raise UnstructuredCompletion(reason)
                        case ResponseIncompleteEvent(
                            response=Response(
                                incomplete_details=IncompleteDetails(reason=str(reason))
                            )
                        ):
                            raise UnstructuredCompletion(INCOMPLETE_REPLY.format(reason=reason))
                        case ResponseFailedEvent() | ResponseIncompleteEvent():
                            raise UnstructuredCompletion(NO_PARSED_OUTPUT)
        except json.JSONDecodeError as failed:
            raise UnstructuredCompletion(UNREADABLE_EVENT.format(error=failed)) from failed
        if not calls and not done:
            raise UnstructuredCompletion(NO_PARSED_OUTPUT)
        # The text a reply ends with is the reply; a message written before it, as a model
        # that comments on its work writes, is not.
        reply = CalledTools(calls=calls) if calls else Spoke(output=parsed_reply(output, done[-1]))
        if usage is None:
            raise UnmeteredCompletion(NO_TOKEN_USAGE)
        return Answered(
            reply=reply, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
        )


WIRE_API_MODELS: dict[WireApi, type[LanguageModel]] = {
    WireApi.CHAT_COMPLETIONS: ChatCompletionsLanguageModel,
    WireApi.RESPONSES: ResponsesLanguageModel,
}


def openai_model(name: str, wire_api: WireApi) -> LanguageModel:
    client = OpenAI(timeout=MODEL_TIMEOUT_SECONDS, max_retries=MODEL_RETRIES)
    return WIRE_API_MODELS[wire_api](client=client, name=name)


def endable_openai_model(name: str, wire_api: WireApi) -> LanguageModel:
    # A model whose replies an Ending cuts at once. Its requests are not sent again after a
    # failure: the openai client waits before it does, and a reader who left would wait for
    # the conversation that long. A digest's or a translation's requests keep the retry, and
    # their connections, through openai_model.
    client = OpenAI(timeout=MODEL_TIMEOUT_SECONDS, max_retries=0, http_client=endable_http_client())
    return WIRE_API_MODELS[wire_api](client=client, name=name)


class GlmEndpoint(BaseModel):
    # Where GLM's models are reached, with GLM's own key alone: never with the address or the
    # key that a client of the openai package finds by itself. How much each model is asked
    # to reason, by name; a model not named reasons as much as the endpoint has it. And the
    # pace that every request to the endpoint keeps, of whichever model.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    base_url: str
    api_key: SecretStr
    reasoning_efforts: dict[str, GlmReasoning]
    pace: Pace

    def reasoning_effort(self, name: str) -> GlmReasoning | Omit:
        return self.reasoning_efforts.get(name, omit)


def glm_model(name: str, endpoint: GlmEndpoint) -> LanguageModel:
    client = OpenAI(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key.get_secret_value(),
        timeout=MODEL_TIMEOUT_SECONDS,
        max_retries=MODEL_RETRIES,
        http_client=paced_http_client(endpoint.pace),
    )
    return GlmLanguageModel(
        client=client, name=name, reasoning_effort=endpoint.reasoning_effort(name)
    )


def endable_glm_model(name: str, endpoint: GlmEndpoint) -> LanguageModel:
    # A model of GLM's endpoint whose replies an Ending cuts at once, as endable_openai_model
    # makes one of the other endpoint.
    client = OpenAI(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key.get_secret_value(),
        timeout=MODEL_TIMEOUT_SECONDS,
        max_retries=0,
        http_client=endable_paced_http_client(endpoint.pace),
    )
    return GlmLanguageModel(
        client=client, name=name, reasoning_effort=endpoint.reasoning_effort(name)
    )
