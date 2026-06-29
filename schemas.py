from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")

    serpapi_api_key: str = Field(default="", alias="SERPAPI_API_KEY")
    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_service_role_key: str = Field(default="", alias="SUPABASE_SERVICE_ROLE_KEY")
    email_api_key: str = Field(default="", alias="EMAIL_API_KEY")
    email_from: str = Field(default="jobs@example.com", alias="EMAIL_FROM")
    email_to: str = Field(default="", alias="EMAIL_TO")
    cors_origins: str = Field(default="http://localhost:5173", alias="CORS_ORIGINS")
    batch_size: int = Field(default=10, alias="BATCH_SIZE")
    max_jobs_per_combo: int = Field(default=3, alias="MAX_JOBS_PER_COMBO")

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
