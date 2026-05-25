"""
validation_engine.py — Layered Validation Engine

Layer A: Deterministic rule enforcement (zero LLM cost).
         Validates payload fields against business rules and live API state.

Layer B: Semantic LLM auditor (independent model, isolated prompt).
         Evaluates intent alignment, hallucination, and policy reasoning
         for cases that deterministic rules alone cannot catch.

Both layers are stateless coroutines — the gateway calls them in sequence.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from config import settings
from prompts import build_auditor_messages
from schemas import (
    AccountStatus,
    ActionType,
    AgentAction,
    AuditVerdict,
    CancellationTiming,
    CreditRequest,
    InvoiceStatus,
    LayerAResult,
    LayerBResult,
    RefundRequest,
    RuleViolation,
    SubscriptionCancellationRequest,
    SubscriptionUpdateRequest,
)

log = structlog.get_logger("validation_engine")


# ─────────────────────────────────────────────────────────────────────────────
# Layer A — Deterministic Rule Validation
# ─────────────────────────────────────────────────────────────────────────────


async def run_layer_a(action: AgentAction) -> LayerAResult:
    """
    Run all deterministic rule checks against the action payload.
    Fetches live business state from the Mock API to validate ownership,
    status, and amounts. Never calls an LLM.
    """
    start = time.monotonic()
    violations: list[RuleViolation] = []

    try:
        async with httpx.AsyncClient(
            base_url=settings.mock_api_base_url,
            timeout=settings.http_timeout_seconds,
        ) as client:
            if action.action_type == ActionType.PROCESS_REFUND:
                violations = await _validate_refund(client, action)
            elif action.action_type == ActionType.ISSUE_CREDIT:
                violations = await _validate_credit(client, action)
            elif action.action_type == ActionType.CANCEL_SUBSCRIPTION:
                violations = await _validate_cancellation(client, action)
            elif action.action_type == ActionType.UPDATE_SUBSCRIPTION:
                violations = await _validate_subscription_update(client, action)
            else:
                # READ operations (lookup_customer, lookup_invoice) always pass
                violations = []

    except httpx.ConnectError:
        violations.append(
            RuleViolation(
                rule_id="API_UNREACHABLE",
                rule_name="Mock API Unreachable",
                description=(
                    f"Cannot reach Mock API at {settings.mock_api_base_url}. "
                    "Ensure mock_api.py is running."
                ),
                severity="error",
            )
        )
    except Exception as e:
        violations.append(
            RuleViolation(
                rule_id="RULE_ENGINE_ERROR",
                rule_name="Validation Engine Error",
                description=f"Unexpected error during Layer A validation: {e}",
                severity="error",
            )
        )

    latency = round((time.monotonic() - start) * 1000, 2)
    passed = len(violations) == 0

    log.info(
        "layer_a.complete",
        request_id=action.request_id,
        passed=passed,
        violations=len(violations),
        latency_ms=latency,
    )

    return LayerAResult(passed=passed, violations=violations, latency_ms=latency)


# ── Refund Rules ──────────────────────────────────────────────────────────────


async def _validate_refund(
    client: httpx.AsyncClient, action: AgentAction
) -> list[RuleViolation]:
    payload: RefundRequest = action.payload  # type: ignore[assignment]
    violations: list[RuleViolation] = []

    # ── Fetch customer ────────────────────────────────────────────────────────
    cust_resp = await client.get(f"/customers/{payload.customer_id}")
    if cust_resp.status_code == 404:
        violations.append(RuleViolation(
            rule_id="CUSTOMER_NOT_FOUND",
            rule_name="Customer Does Not Exist",
            description=f"Customer '{payload.customer_id}' was not found in the system.",
            field="customer_id",
        ))
        return violations  # Cannot continue without a valid customer

    customer = cust_resp.json()

    # ── Account must be ACTIVE ────────────────────────────────────────────────
    if customer["account_status"] != AccountStatus.ACTIVE.value:
        violations.append(RuleViolation(
            rule_id="ACCOUNT_NOT_ACTIVE",
            rule_name="Account Not Active",
            description=(
                f"Customer '{payload.customer_id}' account status is "
                f"'{customer['account_status']}'. Refunds require ACTIVE status."
            ),
            field="customer_id",
        ))

    # ── Fetch invoice ─────────────────────────────────────────────────────────
    inv_resp = await client.get(f"/invoices/{payload.invoice_id}")
    if inv_resp.status_code == 404:
        violations.append(RuleViolation(
            rule_id="INVOICE_NOT_FOUND",
            rule_name="Invoice Does Not Exist",
            description=f"Invoice '{payload.invoice_id}' does not exist — possible hallucination.",
            field="invoice_id",
        ))
        return violations  # Cannot continue without a valid invoice

    invoice = inv_resp.json()

    # ── Invoice ownership ─────────────────────────────────────────────────────
    if invoice["customer_id"] != payload.customer_id:
        violations.append(RuleViolation(
            rule_id="INVOICE_OWNERSHIP_MISMATCH",
            rule_name="Invoice Ownership Mismatch",
            description=(
                f"Invoice '{payload.invoice_id}' belongs to customer "
                f"'{invoice['customer_id']}', not '{payload.customer_id}'."
            ),
            field="invoice_id",
        ))

    # ── Invoice must be refundable ────────────────────────────────────────────
    refundable = {InvoiceStatus.PAID.value, InvoiceStatus.PARTIALLY_REFUNDED.value}
    if invoice["status"] not in refundable:
        violations.append(RuleViolation(
            rule_id="INVOICE_NOT_REFUNDABLE",
            rule_name="Invoice Cannot Be Refunded",
            description=(
                f"Invoice '{payload.invoice_id}' has status '{invoice['status']}'. "
                f"Only PAID or PARTIALLY_REFUNDED invoices can be refunded."
            ),
            field="invoice_id",
        ))

    # ── Amount must not exceed remaining balance ───────────────────────────────
    remaining = round(invoice["amount"] - invoice["refunded_amount"], 2)
    if payload.refund_amount > remaining:
        violations.append(RuleViolation(
            rule_id="REFUND_EXCEEDS_REMAINING",
            rule_name="Refund Amount Exceeds Remaining Balance",
            description=(
                f"Requested refund ${payload.refund_amount:.2f} exceeds "
                f"remaining refundable amount ${remaining:.2f} on invoice "
                f"'{payload.invoice_id}'."
            ),
            field="refund_amount",
        ))

    # ── High-value refunds require approval ───────────────────────────────────
    if (
        payload.refund_amount > settings.refund_approval_threshold
        and not payload.manager_approval
    ):
        violations.append(RuleViolation(
            rule_id="APPROVAL_REQUIRED",
            rule_name="Manager Approval Required",
            description=(
                f"Refund of ${payload.refund_amount:.2f} exceeds the "
                f"${settings.refund_approval_threshold:.2f} threshold and "
                f"requires manager_approval=true."
            ),
            field="manager_approval",
        ))

    # ── Contradictory fields: refund_amount=0 with a refund action ───────────
    if payload.refund_amount == 0:
        violations.append(RuleViolation(
            rule_id="ZERO_REFUND_AMOUNT",
            rule_name="Zero Refund Amount",
            description="refund_amount is 0 — contradictory for a PROCESS_REFUND action.",
            field="refund_amount",
            severity="warning",
        ))

    return violations


# ── Credit Rules ──────────────────────────────────────────────────────────────


async def _validate_credit(
    client: httpx.AsyncClient, action: AgentAction
) -> list[RuleViolation]:
    payload: CreditRequest = action.payload  # type: ignore[assignment]
    violations: list[RuleViolation] = []

    cust_resp = await client.get(f"/customers/{payload.customer_id}")
    if cust_resp.status_code == 404:
        violations.append(RuleViolation(
            rule_id="CUSTOMER_NOT_FOUND",
            rule_name="Customer Does Not Exist",
            description=f"Customer '{payload.customer_id}' was not found.",
            field="customer_id",
        ))
        return violations

    customer = cust_resp.json()

    if customer["account_status"] != AccountStatus.ACTIVE.value:
        violations.append(RuleViolation(
            rule_id="ACCOUNT_NOT_ACTIVE",
            rule_name="Account Not Active",
            description=(
                f"Cannot issue credit to customer '{payload.customer_id}' — "
                f"account status is '{customer['account_status']}'."
            ),
            field="customer_id",
        ))

    return violations


# ── Subscription Cancellation Rules ───────────────────────────────────────────


async def _validate_cancellation(
    client: httpx.AsyncClient, action: AgentAction
) -> list[RuleViolation]:
    payload: SubscriptionCancellationRequest = action.payload  # type: ignore[assignment]
    violations: list[RuleViolation] = []

    cust_resp = await client.get(f"/customers/{payload.customer_id}")
    if cust_resp.status_code == 404:
        violations.append(RuleViolation(
            rule_id="CUSTOMER_NOT_FOUND",
            rule_name="Customer Does Not Exist",
            description=f"Customer '{payload.customer_id}' was not found.",
            field="customer_id",
        ))
        return violations

    customer = cust_resp.json()

    if customer["account_status"] != AccountStatus.ACTIVE.value:
        violations.append(RuleViolation(
            rule_id="ACCOUNT_NOT_ACTIVE",
            rule_name="Account Not Active",
            description=(
                f"Cannot cancel subscription for '{payload.customer_id}' — "
                f"account status is '{customer['account_status']}'."
            ),
            field="customer_id",
        ))

    if customer["subscription_status"] == "cancelled":
        violations.append(RuleViolation(
            rule_id="SUBSCRIPTION_ALREADY_CANCELLED",
            rule_name="Subscription Already Cancelled",
            description=f"Customer '{payload.customer_id}' subscription is already cancelled.",
            field="customer_id",
        ))

    if not payload.confirm:
        violations.append(RuleViolation(
            rule_id="CONFIRMATION_MISSING",
            rule_name="Cancellation Confirmation Missing",
            description="confirm must be true to proceed with subscription cancellation.",
            field="confirm",
        ))

    return violations


# ── Subscription Update Rules ─────────────────────────────────────────────────


async def _validate_subscription_update(
    client: httpx.AsyncClient, action: AgentAction
) -> list[RuleViolation]:
    payload: SubscriptionUpdateRequest = action.payload  # type: ignore[assignment]
    violations: list[RuleViolation] = []

    cust_resp = await client.get(f"/customers/{payload.customer_id}")
    if cust_resp.status_code == 404:
        violations.append(RuleViolation(
            rule_id="CUSTOMER_NOT_FOUND",
            rule_name="Customer Does Not Exist",
            description=f"Customer '{payload.customer_id}' was not found.",
            field="customer_id",
        ))
        return violations

    customer = cust_resp.json()

    if customer["account_status"] != AccountStatus.ACTIVE.value:
        violations.append(RuleViolation(
            rule_id="ACCOUNT_NOT_ACTIVE",
            rule_name="Account Not Active",
            description=f"Account '{payload.customer_id}' is not active.",
            field="customer_id",
        ))

    if customer["subscription_tier"] == payload.new_tier.value:
        violations.append(RuleViolation(
            rule_id="SAME_TIER_UPDATE",
            rule_name="No-Op Tier Change",
            description=(
                f"Customer '{payload.customer_id}' is already on the "
                f"'{payload.new_tier.value}' tier."
            ),
            field="new_tier",
            severity="warning",
        ))

    return violations


# ─────────────────────────────────────────────────────────────────────────────
# Layer B — Semantic LLM Auditor
# ─────────────────────────────────────────────────────────────────────────────


async def run_layer_b(
    action: AgentAction,
    business_state: dict[str, Any],
) -> LayerBResult:
    """
    Run the independent semantic auditor LLM against the action.

    The auditor is architecturally isolated:
    - Uses a different model than the Chaos Agent.
    - Uses a separate system prompt (prompts.py).
    - Has no access to the agent's internal chain-of-thought.

    Returns a LayerBResult with verdict, confidence, and optional correction.
    """
    if not settings.enable_semantic_auditor:
        log.info("layer_b.skipped", reason="ENABLE_SEMANTIC_AUDITOR=false")
        return LayerBResult(
            verdict=AuditVerdict.PASS,
            confidence=1.0,
            failure_reason=None,
        )

    start = time.monotonic()

    try:
        messages = build_auditor_messages(action, business_state)
        result = await _call_auditor_llm(messages)
    except Exception as e:
        log.error("layer_b.error", error=str(e), request_id=action.request_id)
        # On auditor failure, fail open with low confidence to trigger rejection
        latency = round((time.monotonic() - start) * 1000, 2)
        return LayerBResult(
            verdict=AuditVerdict.UNCERTAIN,
            confidence=0.0,
            failure_reason=f"Auditor call failed: {e}",
            latency_ms=latency,
        )

    latency = round((time.monotonic() - start) * 1000, 2)
    result.latency_ms = latency
    result.model_used = settings.auditor_model

    log.info(
        "layer_b.complete",
        request_id=action.request_id,
        verdict=result.verdict,
        confidence=result.confidence,
        latency_ms=latency,
        tokens=result.tokens_used,
    )

    return result


async def _call_auditor_llm(messages: list[dict[str, str]]) -> LayerBResult:
    """Dispatch to the configured LLM provider and parse the structured response."""
    provider = settings.llm_provider.lower()

    if provider == "openai":
        return await _call_openai_auditor(messages)
    elif provider == "anthropic":
        return await _call_anthropic_auditor(messages)
    else:
        raise ValueError(f"Unsupported LLM provider: '{provider}'. Use 'openai' or 'anthropic'.")


async def _call_openai_auditor(messages: list[dict[str, str]]) -> LayerBResult:
    """Call OpenAI and parse the JSON verdict."""
    import json

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)

    response = await client.chat.completions.create(
        model=settings.auditor_model,
        messages=messages,  # type: ignore[arg-type]
        response_format={"type": "json_object"},
        temperature=0,  # Deterministic for auditing
        max_tokens=1024,
    )

    raw = response.choices[0].message.content or "{}"
    tokens_used = response.usage.total_tokens if response.usage else 0

    return _parse_auditor_response(raw, tokens_used)


async def _call_anthropic_auditor(messages: list[dict[str, str]]) -> LayerBResult:
    """Call Anthropic Claude and parse the JSON verdict."""
    import json

    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Anthropic uses system separate from messages
    system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_messages = [m for m in messages if m["role"] != "system"]

    response = await client.messages.create(
        model=settings.auditor_model,
        system=system_msg,
        messages=user_messages,  # type: ignore[arg-type]
        max_tokens=1024,
    )

    raw = response.content[0].text if response.content else "{}"
    tokens_used = response.usage.input_tokens + response.usage.output_tokens

    return _parse_auditor_response(raw, tokens_used)


def _parse_auditor_response(raw: str, tokens_used: int) -> LayerBResult:
    """Parse raw LLM JSON output into a LayerBResult."""
    import json

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return LayerBResult(
            verdict=AuditVerdict.UNCERTAIN,
            confidence=0.0,
            failure_reason=f"Auditor returned non-JSON response: {raw[:200]}",
            tokens_used=tokens_used,
        )

    raw_verdict = str(data.get("verdict", "UNCERTAIN")).upper()
    try:
        verdict = AuditVerdict(raw_verdict.lower())
    except ValueError:
        verdict = AuditVerdict.UNCERTAIN

    confidence = float(data.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))

    corrected_payload = data.get("corrected_payload")

    return LayerBResult(
        verdict=verdict,
        confidence=confidence,
        failure_reason=data.get("failure_reason"),
        corrected_payload=corrected_payload if isinstance(corrected_payload, dict) else None,
        tokens_used=tokens_used,
    )
