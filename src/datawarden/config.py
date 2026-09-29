"""Central configuration loaded from environment variables / `.env`.

Secrets are only ever read from the environment. They are never written to the database,
logs, prompts, or artifacts.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(os.environ.get("DW_REPO_ROOT") or Path(__file__).resolve().parents[2])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_prefix="DW_", extra="ignore", env_ignore_empty=True
    )

    env: str = "local"
    demo_mode: bool = True
    seed: int = 42
    demo_start_date: str = "2026-08-01"
    demo_days: int = 30
    business_tz: str = "Asia/Kolkata"
    mart_lookback_days: int = 2
    artifact_dir: Path = REPO_ROOT / "artifacts"

    # PostgreSQL (application database)
    app_db_host: str = "localhost"
    app_db_port: int = 5432
    app_db_name: str = "datawarden_app"
    app_db_user: str = "dw_app"
    app_db_password: SecretStr = SecretStr("")

    # PostgreSQL (warehouse)
    wh_host: str = "localhost"
    wh_port: int = 5432
    wh_db_name: str = "datawarden_wh"

    # Bootstrap/admin credential; used only by setup commands, never by services at runtime.
    pg_admin_user: str = "postgres"
    pg_admin_password: SecretStr = SecretStr("")

    wh_pipeline_password: SecretStr = SecretStr("")
    wh_agent_ro_password: SecretStr = SecretStr("")
    wh_shadow_password: SecretStr = SecretStr("")
    wh_executor_password: SecretStr = SecretStr("")
    wh_validator_password: SecretStr = SecretStr("")

    # Service-to-service signing (event ingestion callbacks from pipeline / Airflow)
    ingest_hmac_secret: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    api_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:5173"

    # Models
    model_provider: str = "fixture"  # fixture | openai_compatible | azure_openai
    model_name: str = ""
    model_endpoint: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_api_version: str = ""
    model_timeout_s: float = 60.0
    model_token_budget: int = 200_000
    model_price_input_per_1k: float | None = None
    model_price_output_per_1k: float | None = None

    # Investigation graph: "multi" (product) or "single" (single-agent evaluation baseline)
    graph_variant: str = "multi"

    # Budgets
    budget_investigation_rounds: int = 3
    budget_repair_attempts: int = 2
    budget_tool_calls: int = 25
    budget_active_seconds: int = 600
    budget_concurrent_specialists: int = 3
    approval_expiry_hours: int = 24

    # Optional integrations (disabled unless configured)
    github_token: SecretStr = SecretStr("")
    github_repo: str = ""
    airflow_base_url: str = ""
    airflow_username: str = ""
    airflow_password: SecretStr = SecretStr("")
    webhook_url: str = ""
    langfuse_host: str = ""
    otel_exporter_otlp_endpoint: str = ""

    # Test-only chaos hooks (only honoured when demo_mode is true)
    chaos: str = Field(default="")

    @property
    def sources_dir(self) -> Path:
        return self.artifact_dir / "sources"

    @property
    def runtime_dir(self) -> Path:
        return self.artifact_dir / "runtime"

    @property
    def workspace_dir(self) -> Path:
        return self.runtime_dir / "workspace"

    @property
    def contracts_registry_dir(self) -> Path:
        return self.runtime_dir / "contracts"

    def _url(self, user: str, password: SecretStr, host: str, port: int, db: str, driver: str) -> str:
        pw = quote(password.get_secret_value(), safe="")
        return f"{driver}://{quote(user, safe='')}:{pw}@{host}:{port}/{db}"

    def wh_dsn(self, role: str) -> str:
        """libpq DSN for a warehouse role: admin, pipeline, agent_ro, shadow, executor, validator."""
        if role == "admin":
            return self._url(
                self.pg_admin_user, self.pg_admin_password, self.wh_host, self.wh_port, self.wh_db_name, "postgresql"
            )
        password = getattr(self, f"wh_{role}_password")
        return self._url(f"dw_{role}", password, self.wh_host, self.wh_port, self.wh_db_name, "postgresql")

    def admin_dsn(self, db: str = "postgres", host: str | None = None, port: int | None = None) -> str:
        return self._url(
            self.pg_admin_user, self.pg_admin_password, host or self.wh_host, port or self.wh_port, db, "postgresql"
        )

    @property
    def app_dsn(self) -> str:
        return self._url(
            self.app_db_user, self.app_db_password, self.app_db_host, self.app_db_port, self.app_db_name, "postgresql"
        )

    @property
    def app_sqlalchemy_url(self) -> str:
        return self._url(
            self.app_db_user,
            self.app_db_password,
            self.app_db_host,
            self.app_db_port,
            self.app_db_name,
            "postgresql+psycopg",
        )

    def chaos_enabled(self, flag: str) -> bool:
        return self.demo_mode and flag in {f.strip() for f in self.chaos.split(",") if f.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
