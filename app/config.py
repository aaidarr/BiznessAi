from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET = "dev-insecure-secret-change-me"  # noqa: S105 - dev default, rejected in prod


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"  # dev | test | prod
    database_url: str = "sqlite:///./otvet.db"
    secret_key: str = _INSECURE_SECRET
    # Fernet key (urlsafe base64, 32 bytes). Empty in dev -> derived from secret_key.
    encryption_key: str = ""
    public_base_url: str = "http://localhost:8000"

    session_ttl_hours: int = 24 * 14
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    min_password_length: int = 10

    require_site_verification: bool = True
    run_worker: bool = True
    message_retention_days: int = 180

    # LLM: extractive | yandex | openai
    llm_provider: str = "extractive"
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yandex_model: str = "yandexgpt-lite"
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""
    llm_timeout_seconds: float = 8.0

    # Payments (YooKassa)
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""

    # SMTP (optional owner notifications)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # Crawler
    crawl_max_pages: int = 25
    crawl_max_depth: int = 2
    crawl_max_bytes: int = 1_500_000
    crawl_timeout_seconds: float = 10.0
    crawl_total_seconds: float = 60.0
    crawl_allow_private_hosts: bool = False  # tests only

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def cookie_secure(self) -> bool:
        return self.is_prod or self.public_base_url.startswith("https://")

    @model_validator(mode="after")
    def _check_prod(self) -> Settings:
        if self.app_env == "prod":
            problems = []
            if self.secret_key == _INSECURE_SECRET or len(self.secret_key) < 32:
                problems.append("SECRET_KEY must be set (>=32 chars)")
            if not self.encryption_key:
                problems.append("ENCRYPTION_KEY must be set")
            if self.database_url.startswith("sqlite"):
                problems.append("DATABASE_URL must point to PostgreSQL in prod")
            if not self.public_base_url.startswith("https://"):
                problems.append("PUBLIC_BASE_URL must be https in prod")
            if self.crawl_allow_private_hosts:
                problems.append("CRAWL_ALLOW_PRIVATE_HOSTS must be false in prod")
            if problems:
                raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
