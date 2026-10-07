from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="URLSHORT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./urlshortener.db"
    sqlite_busy_timeout_ms: int = 5000
    api_key_pepper: str = "change-me-in-production"
    api_v1_prefix: str = "/api/v1"
    redirect_cache_size: int = Field(default=1024, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
