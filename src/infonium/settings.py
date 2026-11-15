from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from infonium.language_model import WireApi


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INFONIUM_")

    data_dir: Path = Path("data")
    model: str = "gpt-6.1-terra"
    wire_api: WireApi = WireApi.CHAT_COMPLETIONS
    language: str = "简体中文"
    pdf_engine: Path = Path("mineru")
