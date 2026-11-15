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
    pdf_engine: Path = Path("mineru")
    speech_base_url: str = "http://127.0.0.1:8750/v1"
    speech_api_key: SecretStr = SecretStr("unused")
    speech_model: str = "Systran/faster-whisper-large-v3"
