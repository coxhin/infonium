from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from infonium.language_model import GlmReasoning, WireApi


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INFONIUM_")

    data_dir: Path = Path("data")
    model: str = "gpt-6.1-terra"
    wire_api: WireApi = WireApi.CHAT_COMPLETIONS
    language: str = "简体中文"
    # The model a document is translated with unless the reader chooses another. For a
    # translation, and for digests and answers, the reader may choose it, the model that
    # writes digests, or one of GLM's models, which are reached at GLM's endpoint with GLM's
    # key and asked to reason as much as is set for each, by name.
    translation_model: str = "gpt-6-luna"
    glm_models: list[str] = ["glm-5.3", "glm-5.3-flash"]
    glm_base_url: str = "https://open.bigmodel.cn/api/coding/paas/v4"
    glm_api_key: SecretStr | None = None
    glm_reasoning_efforts: dict[str, GlmReasoning] = {"glm-5.3": "low"}
    pdf_engine: Path = Path("mineru")
    browser: Path = Path("google-chrome")
    speech_base_url: str = "http://127.0.0.1:8750/v1"
    speech_api_key: SecretStr = SecretStr("unused")
    speech_model: str = "Systran/faster-whisper-large-v3"
    embedding_base_url: str = "http://127.0.0.1:8751/v1"
    embedding_api_key: SecretStr = SecretStr("unused")
    embedding_model: str = "ibm-granite/granite-embedding-97m-multilingual-r2"
