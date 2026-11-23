from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterator, Sequence
from enum import StrEnum
from typing import Any, Final

from openai import OpenAI, omit, pydantic_function_tool, pydantic_responses_function_tool
from openai.lib.streaming.chat import ContentDeltaEvent
from openai.lib.streaming.responses import ResponseTextDeltaEvent, ResponseTextDoneEvent
from openai.types.chat import ChatCompletionMessageParam
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
    ResponseUsage,
)
from openai.types.responses.response import IncompleteDetails
from pydantic import BaseModel

# The longest silence the endpoint is allowed: before a whole reply that is not streamed, and
# between two events of one that is.
MODEL_TIMEOUT_SECONDS = 180.0
MODEL_RETRIES = 1
# The most requests one reply may take: each round of tool calls costs one request more.
REPLY_ROUNDS = 8
NO_PARSED_OUTPUT = "the model returned no parsed output"
NO_TOKEN_USAGE = "the endpoint reported no token usage"
INCOMPLETE_REPLY = "the endpoint cut the reply short: {reason}"
ENDLESS_TOOL_USE = "the model still called tools after {rounds} requests"
UNFINISHED_REPLY = "a reply ended without its parsed output"
SYSTEM_ROLE: Final = "system"
USER_ROLE: Final = "user"
ASSISTANT_ROLE: Final = "assistant"
TOOL_ROLE: Final = "tool"
FUNCTION_TOOL: Final = "function"
INPUT_TEXT: Final = "input_text"
MESSAGE_ITEM: Final = "message"
FUNCTION_CALL_ITEM: Final = "function_call"
FUNCTION_OUTPUT_ITEM: Final = "function_call_output"


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
    # The reply as far as the model has written it: for a structured reply, unfinished JSON.
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


def tool_named(tools: Sequence[Tool[Any]], name: str) -> Tool[Any]:
    # The model is told of these tools alone, each with a strict schema, so a call names one
    # of them and its arguments fit that tool's schema.
    return next(tool for tool in tools if tool.name == name)


def tool_called(tools: Sequence[Tool[Any]], name: str, arguments: str) -> Called:
    return Called(
        tool=name, arguments=tool_named(tools, name).arguments.model_validate_json(arguments)
    )


def tool_result(tools: Sequence[Tool[Any]], called: Called) -> str:
    return tool_named(tools, called.tool).run(called.arguments)


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
    ) -> Iterator[ReplyEvent[Output]]:
        # A reply of the given form to a conversation whose messages are in the order they
        # were written. The reply is handed on as far as it is written, as often as more
        # arrives; each call of a tool before the tool runs, its result going back to the
        # model; and last the whole reply.
        ...

    def structured[Output: BaseModel](
        self, instructions: str, prompt: str, output: type[Output]
    ) -> StructuredCompletion[Output]:
        *_, last = self.replying(
            instructions, [Message(side=Side.ASKER, text=prompt)], output, tools=[]
        )
        if not isinstance(last, Replied):
            raise TypeError(UNFINISHED_REPLY)
        return last.completion


def chat_message(message: Message) -> ChatCompletionMessageParam:
    match message.side:
        case Side.ASKER:
            return {"role": USER_ROLE, "content": message.text}
        case Side.MODEL:
            return {"role": ASSISTANT_ROLE, "content": message.text}


class ChatCompletionsLanguageModel(LanguageModel):
    def replying[Output: BaseModel](
        self,
        instructions: str,
        messages: Sequence[Message],
        output: type[Output],
        tools: Sequence[Tool[Any]],
    ) -> Iterator[ReplyEvent[Output]]:
        sent: list[ChatCompletionMessageParam] = [
            {"role": SYSTEM_ROLE, "content": instructions},
            *(chat_message(message) for message in messages),
        ]
        declared = [
            pydantic_function_tool(tool.arguments, name=tool.name, description=tool.description)
            for tool in tools
        ]
        spent = NO_TOKENS
        for _ in range(REPLY_ROUNDS):
            with self.client.chat.completions.stream(
                model=self.name,
                messages=sent,
                response_format=output,
                tools=declared or omit,
                stream_options={"include_usage": True},
            ) as stream:
                for event in stream:
                    if isinstance(event, ContentDeltaEvent):
                        yield Written(text=event.snapshot)
                completion = stream.get_final_completion()
            if completion.usage is None:
                raise UnmeteredCompletion(NO_TOKEN_USAGE)
            spent = tokens_after(
                spent, completion.usage.prompt_tokens, completion.usage.completion_tokens
            )
            message = completion.choices[0].message
            if not message.tool_calls:
                if message.parsed is None:
                    raise UnstructuredCompletion(message.refusal or NO_PARSED_OUTPUT)
                yield replied(message.parsed, spent)
                return
            sent.append(
                {
                    "role": ASSISTANT_ROLE,
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": FUNCTION_TOOL,
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )
            for call in message.tool_calls:
                called = tool_called(tools, call.function.name, call.function.arguments)
                yield called
                sent.append(
                    {
                        "role": TOOL_ROLE,
                        "tool_call_id": call.id,
                        "content": tool_result(tools, called),
                    }
                )
        raise UnstructuredCompletion(ENDLESS_TOOL_USE.format(rounds=REPLY_ROUNDS))


class ResponsesSpoke[Output: BaseModel](BaseModel):
    # A request of a reply that brought the reply itself.
    output: Output
    usage: ResponseUsage


class ResponsesCalled(BaseModel):
    # A request of a reply that brought calls of tools in place of the reply.
    calls: list[ResponseFunctionToolCall]
    usage: ResponseUsage


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
    ) -> Iterator[ReplyEvent[Output]]:
        sent = [responses_message(message) for message in messages]
        spent = NO_TOKENS
        for _ in range(REPLY_ROUNDS):
            answered = yield from self.round(instructions, sent, output, tools)
            spent = tokens_after(spent, answered.usage.input_tokens, answered.usage.output_tokens)
            match answered:
                case ResponsesSpoke(output=parsed):
                    yield replied(parsed, spent)
                    return
                case ResponsesCalled(calls=calls):
                    for call in calls:
                        called = tool_called(tools, call.name, call.arguments)
                        yield called
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
                                "output": tool_result(tools, called),
                            }
                        )
        raise UnstructuredCompletion(ENDLESS_TOOL_USE.format(rounds=REPLY_ROUNDS))

    def round[Output: BaseModel](
        self,
        instructions: str,
        sent: list[ResponseInputItemParam],
        output: type[Output],
        tools: Sequence[Tool[Any]],
    ) -> Generator[Written, None, ResponsesSpoke[Output] | ResponsesCalled]:
        parsed: Output | None = None
        calls: list[ResponseFunctionToolCall] = []
        usage: ResponseUsage | None = None
        # A relay may forward the request to ChatGPT's Codex backend, whose clients stream
        # and send the input as a list, so it takes that form. Nothing reads the reply at the
        # endpoint again, so it is not stored there, and a call of a tool goes back to the
        # model with the conversation sent again in full.
        with self.client.responses.stream(
            model=self.name,
            instructions=instructions,
            input=sent,
            text_format=output,
            tools=[
                pydantic_responses_function_tool(
                    tool.arguments, name=tool.name, description=tool.description
                )
                for tool in tools
            ]
            or omit,
            store=False,
        ) as stream:
            # The reply is read from the events that deliver it: such a backend may end the
            # stream with a response whose output is empty.
            for event in stream:
                match event:
                    case ResponseTextDeltaEvent(snapshot=snapshot):
                        yield Written(text=snapshot)
                    case ResponseTextDoneEvent(parsed=output_read) if output_read is not None:
                        parsed = output_read
                    case ResponseOutputItemDoneEvent(item=ResponseFunctionToolCall() as call):
                        calls.append(call)
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
                        response=Response(incomplete_details=IncompleteDetails(reason=str(reason)))
                    ):
                        raise UnstructuredCompletion(INCOMPLETE_REPLY.format(reason=reason))
                    case ResponseFailedEvent() | ResponseIncompleteEvent():
                        raise UnstructuredCompletion(NO_PARSED_OUTPUT)
        if parsed is None and not calls:
            raise UnstructuredCompletion(NO_PARSED_OUTPUT)
        if usage is None:
            raise UnmeteredCompletion(NO_TOKEN_USAGE)
        if parsed is None:
            return ResponsesCalled(calls=calls, usage=usage)
        return ResponsesSpoke(output=parsed, usage=usage)


WIRE_API_MODELS: dict[WireApi, type[LanguageModel]] = {
    WireApi.CHAT_COMPLETIONS: ChatCompletionsLanguageModel,
    WireApi.RESPONSES: ResponsesLanguageModel,
}


def openai_model(name: str, wire_api: WireApi) -> LanguageModel:
    client = OpenAI(timeout=MODEL_TIMEOUT_SECONDS, max_retries=MODEL_RETRIES)
    return WIRE_API_MODELS[wire_api](client=client, name=name)
