from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INFONIUM_")

    data_dir: Path = Path("data")
    model: str = "gpt-6.1-terra"
    language: str = "简体中文"
