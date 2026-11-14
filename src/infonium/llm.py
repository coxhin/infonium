from openai import OpenAI
from pydantic import BaseModel


class UnstructuredCompletion(Exception):
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
        usage = completion.usage
        if message.parsed is None or usage is None:
            raise UnstructuredCompletion(message.refusal or "the model returned no parsed output")
        return StructuredCompletion(
            output=message.parsed,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
        )
