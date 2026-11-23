from typing import Final

import openai
from openai import OpenAI
from pydantic import BaseModel, SecretStr

# Of each text, the engine is given at most this much: 2,000 characters came to about 1,700
# tokens in Chinese and Japanese with the model that engines/embedding serves, and to fewer in
# other writings.
EMBEDDED_CHARACTERS = 2000
FLOAT_ENCODING: Final = "float"
# The ways an engine is away, for now: it cannot be reached, it does not answer in time, it
# asks to be asked later, or it fails in itself. Any other answer, such as one that refuses a
# key, an address or a model, says that a setting is wrong.
ENGINE_AWAY = (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)

type Vector = list[float]


class EmbeddingEngine(BaseModel):
    # Where an engine behind OpenAI's embeddings API is reached, the key it asks for, and the
    # model it embeds with.
    base_url: str
    api_key: SecretStr
    model: str


def embedded(engine: EmbeddingEngine, texts: list[str], timeout: float) -> list[Vector]:
    # The vectors of texts, in their order, as the engine makes them; each text is cut to what
    # the engine is given of it.
    with OpenAI(
        base_url=engine.base_url,
        api_key=engine.api_key.get_secret_value(),
        timeout=timeout,
        max_retries=0,
    ) as endpoint:
        reply = endpoint.embeddings.create(
            model=engine.model,
            input=[text[:EMBEDDED_CHARACTERS] for text in texts],
            encoding_format=FLOAT_ENCODING,
        )
    return [row.embedding for row in sorted(reply.data, key=lambda row: row.index)]
