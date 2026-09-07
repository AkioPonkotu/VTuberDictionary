"""Non-secret configuration loaded from environment variables."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"
    youtube_api_key: SecretStr | None = None
    twitch_client_id: str | None = None
    twitch_client_secret: SecretStr | None = None
    youtube_min_subscribers: int = Field(default=10_000, ge=0)
    twitch_min_followers: int = Field(default=5_000, ge=0)
    audience_threshold_mode: str = "any"
    reverify_after_days: int = Field(default=180, ge=1)
    twitch_discovery_enabled: bool = True
    twitch_discovery_language: str = "ja"
    twitch_discovery_tag: str = "VTuber"
    twitch_discovery_max_pages: int = Field(default=20, ge=1, le=100)
    data_dir: Path = Path("data")
    dist_dir: Path = Path("dist")

    @field_validator("audience_threshold_mode")
    @classmethod
    def known_threshold_mode(cls, value: str) -> str:
        if value not in {"any", "all"}:
            raise ValueError("AUDIENCE_THRESHOLD_MODE must be 'any' or 'all'")
        return value
