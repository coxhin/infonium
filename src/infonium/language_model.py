from openai import OpenAI
from pydantic import BaseModel

MODEL_TIMEOUT_SECONDS = 180.0
MODEL_RETRIES = 1


class UnstructuredCompletion(Exception):
    pass


class UnmeteredCompletion(Exception):
    pass


class StructuredCompletion[Output: BaseModel](BaseModel):
    output: Output
    input_tokens: int
    output_tokens: int


class LanguageModel:
    def __init__(self, client: OpenAI, name: str) -> None:
        self.client = client
        self.name = name

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
            raise UnstructuredCompletion(message.refusal or "the model returned no parsed output")
        if completion.usage is None:
            raise UnmeteredCompletion("the endpoint reported no token usage")
        return StructuredCompletion(
            output=message.parsed,
            input_tokens=completion.usage.prompt_tokens,
            output_tokens=completion.usage.completion_tokens,
        )


def openai_model(name: str) -> LanguageModel:
    return LanguageModel(
        client=OpenAI(timeout=MODEL_TIMEOUT_SECONDS, max_retries=MODEL_RETRIES), name=name
    )
