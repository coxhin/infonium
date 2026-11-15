from abc import ABC, abstractmethod
from enum import StrEnum

from openai import OpenAI
from openai.lib.streaming.responses import ResponseTextDoneEvent
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseError,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
    ResponseRefusalDoneEvent,
    ResponseUsage,
)
from openai.types.responses.response import IncompleteDetails
from pydantic import BaseModel

# The longest silence the endpoint is allowed: before a whole reply that is not streamed, and
# between two events of one that is.
MODEL_TIMEOUT_SECONDS = 180.0
MODEL_RETRIES = 1
NO_PARSED_OUTPUT = "the model returned no parsed output"
NO_TOKEN_USAGE = "the endpoint reported no token usage"
INCOMPLETE_REPLY = "the endpoint cut the reply short: {reason}"


class WireApi(StrEnum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


class UnstructuredCompletion(Exception):
    pass


class UnmeteredCompletion(Exception):
    pass


class StructuredCompletion[Output: BaseModel](BaseModel):
    output: Output
    input_tokens: int
    output_tokens: int


class LanguageModel(ABC):
    def __init__(self, client: OpenAI, name: str) -> None:
        self.client = client
        self.name = name

    @abstractmethod
    def structured[Output: BaseModel](
        self, instructions: str, prompt: str, output: type[Output]
    ) -> StructuredCompletion[Output]: ...


class ChatCompletionsLanguageModel(LanguageModel):
    def structured[Output: BaseModel](
        self, instructions: str, prompt: str, output: type[Output]
    ) -> StructuredCompletion[Output]:
        completion = self.client.chat.completions.parse(
            model=self.name,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": prompt},
            ],
            response_format=output,
        )
        message = completion.choices[0].message
        if message.parsed is None:
            raise UnstructuredCompletion(message.refusal or NO_PARSED_OUTPUT)
        if completion.usage is None:
            raise UnmeteredCompletion(NO_TOKEN_USAGE)
        return StructuredCompletion(
            output=message.parsed,
            input_tokens=completion.usage.prompt_tokens,
            output_tokens=completion.usage.completion_tokens,
        )


class ResponsesLanguageModel(LanguageModel):
    def structured[Output: BaseModel](
        self, instructions: str, prompt: str, output: type[Output]
    ) -> StructuredCompletion[Output]:
        parsed: Output | None = None
        usage: ResponseUsage | None = None
        # A relay may forward the request to ChatGPT's Codex backend, whose clients stream
        # and send the input as a list, so it takes that form although only the whole reply
        # is used. Nothing reads the reply at the endpoint again, so it is not stored there.
        with self.client.responses.stream(
            model=self.name,
            instructions=instructions,
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            text_format=output,
            store=False,
        ) as stream:
            # The reply is read from the events that deliver it: such a backend may end the
            # stream with a response whose output is empty.
            for event in stream:
                match event:
                    case ResponseTextDoneEvent(parsed=output_read) if output_read is not None:
                        parsed = output_read
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
        if parsed is None:
            raise UnstructuredCompletion(NO_PARSED_OUTPUT)
        if usage is None:
            raise UnmeteredCompletion(NO_TOKEN_USAGE)
        return StructuredCompletion(
            output=parsed, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
        )


WIRE_API_MODELS: dict[WireApi, type[LanguageModel]] = {
    WireApi.CHAT_COMPLETIONS: ChatCompletionsLanguageModel,
    WireApi.RESPONSES: ResponsesLanguageModel,
}


def openai_model(name: str, wire_api: WireApi) -> LanguageModel:
    client = OpenAI(timeout=MODEL_TIMEOUT_SECONDS, max_retries=MODEL_RETRIES)
    return WIRE_API_MODELS[wire_api](client=client, name=name)
