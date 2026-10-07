from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="URLSHORT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./urlshortener.db"
    sqlite_busy_timeout_ms: int = 5000
    api_key_pepper: str = "change-me-in-production"
    api_v1_prefix: str = "/api/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
