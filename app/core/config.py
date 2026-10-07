from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_BLOCKED_DOMAINS_FILE = Path(__file__).with_name("blocked_domains.txt")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="URLSHORT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./urlshortener.db"
    sqlite_busy_timeout_ms: int = 5000
    api_key_pepper: str = "change-me-in-production"
    api_v1_prefix: str = "/api/v1"
    redirect_cache_size: int = Field(default=1024, ge=1)

    # Public URL of this service. Used for short_url and to detect self-referencing links;
    # set it in production because the request Host header is client-controlled.
    public_base_url: str | None = None
    blocked_domains_file: str = str(DEFAULT_BLOCKED_DOMAINS_FILE)

    # Token buckets: the limit is both the burst size and the per-minute refill rate.
    rate_limit_enabled: bool = True
    rate_limit_register_ip_per_minute: int = Field(default=10, ge=1)
    rate_limit_create_ip_per_minute: int = Field(default=30, ge=1)
    rate_limit_create_key_per_minute: int = Field(default=60, ge=1)
    rate_limit_redirect_ip_per_minute: int = Field(default=600, ge=1)

    # JSON list, e.g. URLSHORT_CORS_ALLOW_ORIGINS='["https://app.example.com"]'.
    # An empty list disables CORS.
    cors_allow_origins: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
