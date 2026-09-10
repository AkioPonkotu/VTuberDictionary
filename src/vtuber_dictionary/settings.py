"""Non-secret configuration loaded from environment variables."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    openai_api_key: SecretStr | None = None
    openai_model: str | None = None
    youtube_api_key: SecretStr | None = None
    twitch_client_id: str | None = None
    twitch_client_secret: SecretStr | None = None
    youtube_min_subscribers: int = Field(default=10_000, ge=0)
    twitch_min_followers: int = Field(default=5_000, ge=0)
    audience_threshold_mode: str = "any"
    twitch_discovery_enabled: bool = True
    twitch_discovery_language: str = "ja"
    twitch_discovery_tag: str = "VTuber"
    twitch_discovery_max_pages: int = Field(default=20, ge=1, le=100)
    twitch_crawler_enabled: bool = True
    twitch_crawler_max_results: int = Field(default=3, ge=1, le=5)
    twitch_crawler_delay_seconds: float = Field(default=1.0, ge=0.1, le=60)
    # A Twitch checkpoint can contain hundreds of independent candidates.  The
    # source-specific clients retain their own limits, so this is intentionally
    # allowed to be higher than the normal worker count for a catch-up run.
    processing_concurrency: int = Field(default=6, ge=1, le=128)
    openai_concurrency: int = Field(default=2, ge=1, le=128)
    openai_min_request_interval_seconds: float = Field(default=0.0, ge=0.0, le=60.0)
    data_dir: Path = Path("data")
    dist_dir: Path = Path("dist")

    @field_validator("audience_threshold_mode")
    @classmethod
    def known_threshold_mode(cls, value: str) -> str:
        if value not in {"any", "all"}:
            raise ValueError("AUDIENCE_THRESHOLD_MODE must be 'any' or 'all'")
        return value
