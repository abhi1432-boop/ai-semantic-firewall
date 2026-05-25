"""
config.py — Centralized Configuration

All environment variables, tunable thresholds, and service constants live here.
Every other module imports from this file — never reads os.environ directly.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM Provider ──────────────────────────────────────────────────────────
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # Model used by the Chaos Agent (primary, intentionally unreliable)
    agent_model: str = Field(default="gpt-4o", alias="AGENT_MODEL")

    # Model used by the Semantic Auditor — must be DIFFERENT from agent model
    # to reduce correlated failure modes. Use a cheaper/faster model here.
    auditor_model: str = Field(default="gpt-4o-mini", alias="AUDITOR_MODEL")

    # Which LLM provider to use: "openai" | "anthropic"
    llm_provider: str = Field(default="openai", alias="LLM_PROVIDER")

    # ── Firewall Thresholds ───────────────────────────────────────────────────

    # Confidence below this → REJECT (auditor is too uncertain to allow)
    semantic_confidence_threshold: float = Field(
        default=0.75, alias="SEMANTIC_CONFIDENCE_THRESHOLD"
    )

    # Confidence above this AND a corrected payload present → attempt AUTO-CORRECT
    auto_correct_threshold: float = Field(
        default=0.85, alias="AUTO_CORRECT_THRESHOLD"
    )

    # How many times the gateway will attempt auto-correction before giving up
    max_correction_retries: int = Field(default=2, alias="MAX_CORRECTION_RETRIES")

    # ── Feature Flags ────────────────────────────────────────────────────────
    enable_semantic_auditor: bool = Field(
        default=True, alias="ENABLE_SEMANTIC_AUDITOR"
    )
    enable_auto_correction: bool = Field(
        default=True, alias="ENABLE_AUTO_CORRECTION"
    )
    enable_escalation: bool = Field(
        default=False, alias="ENABLE_ESCALATION"  # Phase 5 feature
    )

    # ── Service Ports ────────────────────────────────────────────────────────
    mock_api_port: int = Field(default=8001, alias="MOCK_API_PORT")
    gateway_port: int = Field(default=8000, alias="GATEWAY_PORT")

    mock_api_base_url: str = Field(
        default="http://localhost:8001", alias="MOCK_API_BASE_URL"
    )

    # ── Audit / Logging ──────────────────────────────────────────────────────
    audit_log_path: str = Field(
        default="reports/audit_log.jsonl", alias="AUDIT_LOG_PATH"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── Business Rules (mirrors mock_api.py constants) ────────────────────────
    # Kept here so the validation engine can enforce them without calling the API.
    refund_approval_threshold: float = Field(
        default=100.00, alias="REFUND_APPROVAL_THRESHOLD"
    )
    max_refund_amount: float = Field(default=10_000.00, alias="MAX_REFUND_AMOUNT")
    max_credit_amount: float = Field(default=10_000.00, alias="MAX_CREDIT_AMOUNT")

    # ── HTTP Client ───────────────────────────────────────────────────────────
    http_timeout_seconds: float = Field(default=30.0, alias="HTTP_TIMEOUT_SECONDS")
    http_max_retries: int = Field(default=3, alias="HTTP_MAX_RETRIES")


# Module-level singleton — import this everywhere
settings = Settings()
