"""Runtime settings, read from the environment (and an optional .env file) once per process."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEMO_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "demo"

ThinkingLevel = Literal["low", "medium", "high"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "production"] = "development"
    database_path: Path = Path("data/triage.db")
    public_url: str = "http://localhost:8000"

    # Which mailbox and which model back the pipeline.
    mail_provider: Literal["demo", "gmail", "graph"] = "demo"
    ai_provider: Literal["gemini", "mock"] = "mock"

    # Gemini (the SDK also reads GEMINI_API_KEY from the environment)
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"
    # Thinking level per step: quick classification, more careful drafting.
    triage_thinking: ThinkingLevel = "low"
    draft_thinking: ThinkingLevel = "medium"

    # Business context and rules
    business_profile_file: Path = DEMO_DIR / "business_profile.md"
    clients_file: Path = DEMO_DIR / "clients.csv"
    kb_dir: Path = DEMO_DIR / "kb"
    routing_file: Path = DEMO_DIR / "routing.yaml"
    privacy_file: Path = DEMO_DIR / "privacy.yaml"
    demo_emails_file: Path = DEMO_DIR / "emails.json"
    demo_responses_file: Path = DEMO_DIR / "scripted_responses.json"

    # Optional external knowledge base (e.g. the Project 4 knowledge API) instead of kb_dir.
    kb_url: str | None = None
    kb_api_key: str | None = None
    kb_top_k: int = 4

    # The shared inbox and the team's own domains (mail from these is ours, not a client's).
    mailbox_address: str = "support@tidewater.example"
    team_domains: str = "tidewater.example"
    reply_signature: str = "Best regards,\nTidewater Payroll Client Support"
    # Put a "review, then delete this block" note with sources at the top of each draft.
    draft_review_note: bool = True

    # Polling and scheduling
    lookback_days: int = Field(default=3, ge=1, le=30)
    max_messages_per_poll: int = Field(default=50, ge=1, le=500)
    scheduler_enabled: bool = False
    poll_seconds: int = Field(default=120, ge=30)
    digest_time: str = "08:45"
    digest_days: str = "mon-fri"
    timezone: str = "Europe/London"

    # Notifications
    notify: Literal["log", "slack", "teams"] = "log"
    slack_webhook_url: str | None = None
    teams_webhook_url: str | None = None

    # Dashboard login (HTTP Basic) and API key for n8n / scripts
    basic_auth_user: str | None = None
    basic_auth_password: str | None = None
    api_key: str | None = None
    auth_disabled: bool = False
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(32))

    # Gmail
    gmail_client_secrets_file: Path = Path("secrets/gmail-client.json")
    gmail_token_file: Path = Path("secrets/gmail-token.json")
    gmail_service_account_file: Path | None = None
    gmail_delegated_user: str | None = None

    # Microsoft Graph (Microsoft 365 shared mailbox)
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_client_secret: str | None = None
    graph_mailbox: str | None = None

    @property
    def team_domain_list(self) -> list[str]:
        return [d.strip().lower() for d in self.team_domains.split(",") if d.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def business_profile(self) -> str:
        path = self.business_profile_file
        return path.read_text(encoding="utf-8") if path.exists() else ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
