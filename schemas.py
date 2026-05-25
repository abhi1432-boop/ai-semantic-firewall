"""
schemas.py — Shared Pydantic Type System

Single source of truth for all data models exchanged between the Chaos Agent,
Semantic Firewall Gateway, Validation Engine, Mock API, and Audit Tracker.

All models use strict mode to prevent silent coercion of incorrect types.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────


class AccountStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"
    PENDING_VERIFICATION = "pending_verification"


class SubscriptionTier(str, Enum):
    FREE = "free"
    STARTER = "starter"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    PAST_DUE = "past_due"
    TRIAL = "trial"


class InvoiceStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    VOID = "void"
    DISPUTED = "disputed"


class ActionType(str, Enum):
    PROCESS_REFUND = "process_refund"
    ISSUE_CREDIT = "issue_credit"
    CANCEL_SUBSCRIPTION = "cancel_subscription"
    UPDATE_SUBSCRIPTION = "update_subscription"
    LOOKUP_CUSTOMER = "lookup_customer"
    LOOKUP_INVOICE = "lookup_invoice"


class CancellationTiming(str, Enum):
    IMMEDIATE = "immediate"
    END_OF_PERIOD = "end_of_period"


class GatewayDecision(str, Enum):
    ALLOW = "allow"
    REJECT = "reject"
    AUTO_CORRECT = "auto_correct"
    REQUEST_REGENERATION = "request_regeneration"
    ESCALATE = "escalate"


class AuditVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"


class OutcomeCategory(str, Enum):
    SUCCESS = "Success"
    BLOCKED_RULES = "Blocked-Rules"
    BLOCKED_SEMANTIC = "Blocked-Semantic"
    AUTO_CORRECTED = "Auto-Corrected"
    ESCALATED = "Escalated"
    SILENT_FAILURE = "Silent-Failure"


# ─────────────────────────────────────────────────────────────────────────────
# Domain Models (Enterprise Data)
# ─────────────────────────────────────────────────────────────────────────────


class CustomerRecord(BaseModel):
    """Represents a customer in the CRM."""

    model_config = ConfigDict(strict=True)

    customer_id: str = Field(..., description="Unique customer identifier")
    name: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., description="Primary contact email")
    account_status: AccountStatus = AccountStatus.ACTIVE
    account_balance: float = Field(
        default=0.0, ge=0.0, description="Current credit balance in USD"
    )
    subscription_tier: SubscriptionTier = SubscriptionTier.FREE
    subscription_status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Invoice(BaseModel):
    """Represents a billing invoice."""

    model_config = ConfigDict(strict=True)

    invoice_id: str = Field(..., description="Unique invoice identifier")
    customer_id: str = Field(..., description="Owning customer ID")
    amount: float = Field(..., gt=0.0, description="Original invoice amount in USD")
    status: InvoiceStatus = InvoiceStatus.PENDING
    description: str = Field(default="", max_length=1000)
    refunded_amount: float = Field(
        default=0.0, ge=0.0, description="Total amount already refunded"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    paid_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def refunded_amount_cannot_exceed_invoice(self) -> Invoice:
        if self.refunded_amount > self.amount:
            raise ValueError(
                f"refunded_amount ({self.refunded_amount}) exceeds "
                f"invoice amount ({self.amount})"
            )
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Agent Action Payloads (what the Chaos Agent submits to the Gateway)
# ─────────────────────────────────────────────────────────────────────────────


class RefundRequest(BaseModel):
    """Agent-generated refund request payload."""

    model_config = ConfigDict(strict=True)

    customer_id: str = Field(..., description="Customer to refund")
    invoice_id: str = Field(..., description="Invoice to refund against")
    refund_amount: float = Field(..., gt=0.0, le=10_000.0, description="USD amount")
    reason: str = Field(..., min_length=1, max_length=500, description="Refund reason")
    manager_approval: bool = Field(
        default=False, description="Whether manager has pre-approved this refund"
    )

    @field_validator("refund_amount")
    @classmethod
    def amount_must_be_positive_and_reasonable(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("refund_amount must be positive")
        return round(v, 2)


class CreditRequest(BaseModel):
    """Agent-generated account credit request payload."""

    model_config = ConfigDict(strict=True)

    customer_id: str = Field(..., description="Customer to credit")
    credit_amount: float = Field(..., gt=0.0, le=10_000.0, description="USD amount")
    reason: str = Field(..., min_length=1, max_length=500)
    expires_at: datetime | None = Field(
        default=None, description="Optional credit expiry date"
    )


class SubscriptionCancellationRequest(BaseModel):
    """Agent-generated subscription cancellation request."""

    model_config = ConfigDict(strict=True)

    customer_id: str
    timing: CancellationTiming = CancellationTiming.END_OF_PERIOD
    reason: str = Field(default="", max_length=500)
    confirm: bool = Field(
        ..., description="Explicit confirmation flag required to prevent accidents"
    )

    @field_validator("confirm")
    @classmethod
    def confirm_must_be_true(cls, v: bool) -> bool:
        if not v:
            raise ValueError("confirm must be true to proceed with cancellation")
        return v


class SubscriptionUpdateRequest(BaseModel):
    """Agent-generated subscription tier change request."""

    model_config = ConfigDict(strict=True)

    customer_id: str
    new_tier: SubscriptionTier
    reason: str = Field(default="", max_length=500)


# ─────────────────────────────────────────────────────────────────────────────
# Gateway / Firewall Models
# ─────────────────────────────────────────────────────────────────────────────


class AgentAction(BaseModel):
    """
    The envelope the Chaos Agent wraps around any payload before submitting
    to the Firewall Gateway. Carries the original customer context alongside
    the generated payload, enabling semantic validation.
    """

    model_config = ConfigDict(strict=False)  # Payload field is polymorphic

    request_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique transaction trace ID",
    )
    action_type: ActionType
    customer_request: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Original natural-language customer request",
    )
    payload: RefundRequest | CreditRequest | SubscriptionCancellationRequest | SubscriptionUpdateRequest = Field(
        ..., description="Typed action payload generated by the agent"
    )
    tool_history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Previous tool calls in this session for context",
    )
    agent_reasoning: str = Field(
        default="",
        max_length=2000,
        description="Agent's internal reasoning/chain-of-thought (for audit)",
    )
    submitted_at: datetime = Field(default_factory=datetime.utcnow)


class RuleViolation(BaseModel):
    """A single deterministic rule violation."""

    rule_id: str = Field(..., description="Machine-readable rule identifier")
    rule_name: str = Field(..., description="Human-readable rule name")
    description: str = Field(..., description="Why this rule was violated")
    field: str | None = Field(default=None, description="Specific field that failed")
    severity: str = Field(default="error", description="error | warning | info")


class LayerAResult(BaseModel):
    """Result from Layer A deterministic rule validation."""

    passed: bool
    violations: list[RuleViolation] = Field(default_factory=list)
    latency_ms: float = Field(default=0.0)


class LayerBResult(BaseModel):
    """Result from Layer B semantic auditor."""

    verdict: AuditVerdict
    confidence: float = Field(..., ge=0.0, le=1.0, description="0.0 = certain fail, 1.0 = certain pass")
    failure_reason: str | None = Field(
        default=None, description="Human-readable explanation if verdict is FAIL"
    )
    corrected_payload: dict[str, Any] | None = Field(
        default=None, description="Suggested corrected payload if applicable"
    )
    tokens_used: int = Field(default=0)
    latency_ms: float = Field(default=0.0)
    model_used: str = Field(default="")


class GatewayEvent(BaseModel):
    """
    Full audit record for a single transaction. Written to the audit log
    by the tracker after every gateway decision.
    """

    request_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    action_type: ActionType
    customer_request: str
    generated_payload: dict[str, Any]
    layer_a_result: LayerAResult
    layer_b_result: LayerBResult | None = None
    gateway_decision: GatewayDecision
    correction_applied: dict[str, Any] | None = None
    correction_attempts: int = Field(default=0)
    api_response: dict[str, Any] | None = None
    api_status_code: int | None = None
    total_latency_ms: float = Field(default=0.0)
    outcome_category: OutcomeCategory
    escalation_reason: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# API Response Models (Mock Enterprise API)
# ─────────────────────────────────────────────────────────────────────────────


class APIError(BaseModel):
    """Structured error response from the Mock Enterprise API."""

    error_code: str
    message: str
    field: str | None = None
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class RefundResponse(BaseModel):
    """Successful refund operation response."""

    refund_id: str = Field(default_factory=lambda: f"ref_{uuid.uuid4().hex[:12]}")
    customer_id: str
    invoice_id: str
    refund_amount: float
    new_invoice_status: InvoiceStatus
    processed_at: datetime = Field(default_factory=datetime.utcnow)
    message: str = "Refund processed successfully"


class CreditResponse(BaseModel):
    """Successful credit operation response."""

    credit_id: str = Field(default_factory=lambda: f"crd_{uuid.uuid4().hex[:12]}")
    customer_id: str
    credit_amount: float
    new_balance: float
    processed_at: datetime = Field(default_factory=datetime.utcnow)
    message: str = "Credit issued successfully"


class CancellationResponse(BaseModel):
    """Successful subscription cancellation response."""

    customer_id: str
    timing: CancellationTiming
    effective_date: datetime
    processed_at: datetime = Field(default_factory=datetime.utcnow)
    message: str = "Subscription cancellation scheduled"


class CustomerResponse(BaseModel):
    """Customer lookup response (read-only, safe to return in full)."""

    customer_id: str
    name: str
    email: str
    account_status: AccountStatus
    account_balance: float
    subscription_tier: SubscriptionTier
    subscription_status: SubscriptionStatus


class InvoiceResponse(BaseModel):
    """Invoice lookup response."""

    invoice_id: str
    customer_id: str
    amount: float
    status: InvoiceStatus
    description: str
    refunded_amount: float
    created_at: datetime
    paid_at: datetime | None


# ─────────────────────────────────────────────────────────────────────────────
# Gateway → Agent Response
# ─────────────────────────────────────────────────────────────────────────────


class FirewallResponse(BaseModel):
    """
    What the Firewall Gateway returns to the Chaos Agent after processing
    an AgentAction. The agent must never see the raw API response directly.
    """

    request_id: str
    decision: GatewayDecision
    allowed: bool
    outcome_category: OutcomeCategory
    rejection_reasons: list[str] = Field(default_factory=list)
    api_response: dict[str, Any] | None = None
    corrected_payload: dict[str, Any] | None = None
    escalation_ticket: str | None = None
    message: str = ""
    total_latency_ms: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Metrics Summary (for tracker reporting)
# ─────────────────────────────────────────────────────────────────────────────


class EscalationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class EscalationItem(BaseModel):
    """A transaction flagged for human review."""

    request_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    customer_request: str
    action_type: ActionType
    generated_payload: dict[str, Any]
    rejection_reasons: list[str] = Field(default_factory=list)
    layer_b_confidence: float = 0.0
    layer_b_reason: str = ""
    status: EscalationStatus = EscalationStatus.PENDING
    reviewer_notes: str = ""
    reviewed_at: datetime | None = None
    reviewer_decision: GatewayDecision | None = None


class MetricsSummary(BaseModel):
    """Aggregate statistics over a set of transactions."""

    total_transactions: int = 0
    success_count: int = 0
    blocked_by_rules: int = 0
    blocked_by_semantic: int = 0
    auto_corrected: int = 0
    escalated: int = 0
    silent_failures: int = 0

    detection_rate: float = Field(
        default=0.0, description="Fraction of unsafe requests caught"
    )
    false_positive_rate: float = Field(
        default=0.0, description="Fraction of valid requests incorrectly blocked"
    )
    correction_success_rate: float = Field(
        default=0.0, description="Fraction of corrections that produced valid output"
    )
    mean_latency_ms: float = 0.0
    mean_auditor_tokens: float = 0.0
    estimated_cost_per_request_usd: float = 0.0
    rule_catch_fraction: float = Field(
        default=0.0, description="Of all blocks, fraction caught by Layer A vs Layer B"
    )
    semantic_catch_fraction: float = 0.0
