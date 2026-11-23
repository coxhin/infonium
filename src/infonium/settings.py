from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from infonium.language_model import WireApi


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INFONIUM_")

    data_dir: Path = Path("data")
    model: str = "gpt-6.1-terra"
    wire_api: WireApi = WireApi.CHAT_COMPLETIONS
    language: str = "简体中文"
    translation_model: str = "gpt-6-luna"
    pdf_engine: Path = Path("mineru")
    browser: Path = Path("google-chrome")
    speech_base_url: str = "http://127.0.0.1:8750/v1"
    speech_api_key: SecretStr = SecretStr("unused")
    speech_model: str = "Systran/faster-whisper-large-v3"
    embedding_base_url: str = "http://127.0.0.1:8751/v1"
    embedding_api_key: SecretStr = SecretStr("unused")
    embedding_model: str = "ibm-granite/granite-embedding-97m-multilingual-r2"
