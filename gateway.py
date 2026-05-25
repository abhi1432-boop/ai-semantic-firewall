"""
gateway.py — Semantic Firewall Gateway

The single enforcement boundary between the Chaos Agent and the Mock Enterprise API.
The agent MUST NEVER call the API directly — all actions pass through here.

Decision logic:
  1. Run Layer A (deterministic rules).
     FAIL → REJECT immediately.
  2. Run Layer B (semantic auditor, if enabled).
     FAIL + correctable → AUTO-CORRECT and re-run Layer A.
     FAIL + not correctable → REJECT or ESCALATE.
     UNCERTAIN below threshold → REJECT.
  3. Both pass → ALLOW: forward payload to Mock API.
  4. Record every decision to the audit tracker.

Run standalone:
    uvicorn gateway:app --port 8000 --reload
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import tracker
from config import settings
from schemas import (
    ActionType,
    AgentAction,
    AuditVerdict,
    CreditRequest,
    EscalationItem,
    EscalationStatus,
    FirewallResponse,
    GatewayDecision,
    GatewayEvent,
    LayerBResult,
    OutcomeCategory,
    RefundRequest,
    SubscriptionCancellationRequest,
    SubscriptionUpdateRequest,
)
from validation_engine import run_layer_a, run_layer_b

log = structlog.get_logger("gateway")

# In-memory escalation queue (keyed by request_id for O(1) lookup)
_escalation_queue: dict[str, EscalationItem] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Application Lifecycle
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "gateway.startup",
        mock_api=settings.mock_api_base_url,
        semantic_auditor=settings.enable_semantic_auditor,
        auto_correction=settings.enable_auto_correction,
    )
    yield
    log.info("gateway.shutdown")


app = FastAPI(
    title="AI Semantic Firewall Gateway",
    description=(
        "Middleware enforcement layer that intercepts agent-generated API calls, "
        "validates them for semantic correctness, and blocks unsafe transactions."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Middleware — Request Tracing
# ─────────────────────────────────────────────────────────────────────────────


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Exception Handler
# ─────────────────────────────────────────────────────────────────────────────


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "request_id": getattr(request.state, "request_id", "")},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Core: Fetch Business State from Mock API
# ─────────────────────────────────────────────────────────────────────────────


async def _fetch_business_state(action: AgentAction) -> dict[str, Any]:
    """
    Pull the relevant slice of enterprise state needed by the auditor.
    This gives the auditor ground truth to reason against.
    """
    state: dict[str, Any] = {}

    async with httpx.AsyncClient(
        base_url=settings.mock_api_base_url,
        timeout=settings.http_timeout_seconds,
    ) as client:
        try:
            # Always pull customer record if we have a customer_id
            payload = action.payload
            customer_id = getattr(payload, "customer_id", None)

            if customer_id:
                resp = await client.get(f"/customers/{customer_id}")
                if resp.status_code == 200:
                    state["customer"] = resp.json()

            # For refunds, also pull the invoice
            if action.action_type == ActionType.PROCESS_REFUND:
                invoice_id = getattr(payload, "invoice_id", None)
                if invoice_id:
                    resp = await client.get(f"/invoices/{invoice_id}")
                    if resp.status_code == 200:
                        state["invoice"] = resp.json()
                    # Also fetch all invoices for this customer so the auditor
                    # can detect cross-customer ownership issues
                    if customer_id:
                        resp2 = await client.get(f"/customers/{customer_id}/invoices")
                        if resp2.status_code == 200:
                            state["customer_invoices"] = resp2.json()

        except httpx.ConnectError:
            state["_error"] = "Mock API unreachable"

    return state


# ─────────────────────────────────────────────────────────────────────────────
# Core: Forward Approved Action to Mock API
# ─────────────────────────────────────────────────────────────────────────────


async def _forward_to_api(
    action: AgentAction,
    request_id: str,
    override_payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """
    Forward the (possibly corrected) payload to the Mock API.
    Returns (status_code, response_body).
    """
    payload = action.payload
    route_map = {
        ActionType.PROCESS_REFUND: ("POST", "/refunds"),
        ActionType.ISSUE_CREDIT: ("POST", "/credits"),
        ActionType.CANCEL_SUBSCRIPTION: ("POST", "/subscriptions/cancel"),
        ActionType.UPDATE_SUBSCRIPTION: ("POST", "/subscriptions/update"),
        ActionType.LOOKUP_CUSTOMER: ("GET", f"/customers/{getattr(payload, 'customer_id', '')}"),
        ActionType.LOOKUP_INVOICE: ("GET", f"/invoices/{getattr(payload, 'invoice_id', '')}"),
    }

    method, path = route_map[action.action_type]
    body = override_payload if override_payload is not None else payload.model_dump(mode="json")

    async with httpx.AsyncClient(
        base_url=settings.mock_api_base_url,
        timeout=settings.http_timeout_seconds,
    ) as client:
        resp = await client.request(
            method=method,
            url=path,
            json=body if method == "POST" else None,
            headers={"X-Request-ID": request_id},
        )

    try:
        body_json = resp.json()
    except Exception:
        body_json = {"raw": resp.text}

    return resp.status_code, body_json


# ─────────────────────────────────────────────────────────────────────────────
# Core: Build a corrected payload from auditor suggestion
# ─────────────────────────────────────────────────────────────────────────────


def _apply_correction(
    action: AgentAction, corrected_fields: dict[str, Any]
) -> AgentAction:
    """
    Merge corrected fields from the auditor into the action payload.
    Returns a new AgentAction with the updated payload.
    """
    current = action.payload.model_dump(mode="json")
    current.update(corrected_fields)

    payload_type = type(action.payload)
    new_payload = payload_type.model_validate(current)

    return action.model_copy(update={"payload": new_payload})


# ─────────────────────────────────────────────────────────────────────────────
# Core: Decision Engine
# ─────────────────────────────────────────────────────────────────────────────


async def process_action(action: AgentAction) -> FirewallResponse:
    """
    Main gateway decision pipeline. Returns a FirewallResponse to the agent.
    Also records the full GatewayEvent to the audit tracker.
    """
    wall_start = time.monotonic()
    request_id = action.request_id
    correction_attempts = 0
    layer_b_result: LayerBResult | None = None
    api_response: dict[str, Any] | None = None
    api_status_code: int | None = None
    correction_applied: dict[str, Any] | None = None

    log.info(
        "gateway.processing",
        request_id=request_id,
        action_type=action.action_type,
        customer_request=action.customer_request[:80],
    )

    # ── Step 1: Layer A — Deterministic Rules ─────────────────────────────────
    layer_a_result = await run_layer_a(action)

    if not layer_a_result.passed:
        decision = GatewayDecision.REJECT
        outcome = OutcomeCategory.BLOCKED_RULES
        reasons = [v.description for v in layer_a_result.violations]

        log.warning(
            "gateway.rejected_by_rules",
            request_id=request_id,
            violations=reasons,
        )

        await _record(
            action, layer_a_result, None, decision, outcome,
            correction_applied, correction_attempts,
            api_response, api_status_code, wall_start,
        )

        return FirewallResponse(
            request_id=request_id,
            decision=decision,
            allowed=False,
            outcome_category=outcome,
            rejection_reasons=reasons,
            total_latency_ms=round((time.monotonic() - wall_start) * 1000, 2),
            message="Request blocked by deterministic rule validation.",
        )

    # ── Step 2: Layer B — Semantic Auditor ────────────────────────────────────
    if settings.enable_semantic_auditor:
        business_state = await _fetch_business_state(action)
        layer_b_result = await run_layer_b(action, business_state)

        # Handle FAIL with auto-correction loop
        if layer_b_result.verdict == AuditVerdict.FAIL:
            if (
                settings.enable_auto_correction
                and layer_b_result.corrected_payload is not None
                and layer_b_result.confidence >= settings.auto_correct_threshold
            ):
                # Attempt correction up to max retries
                working_action = action
                for attempt in range(1, settings.max_correction_retries + 1):
                    correction_attempts = attempt
                    corrected = _apply_correction(working_action, layer_b_result.corrected_payload)

                    # Re-run Layer A on corrected payload
                    recheck = await run_layer_a(corrected)
                    if recheck.passed:
                        # Correction worked — use the corrected action
                        working_action = corrected
                        correction_applied = layer_b_result.corrected_payload
                        log.info(
                            "gateway.auto_corrected",
                            request_id=request_id,
                            attempt=attempt,
                            correction=correction_applied,
                        )
                        action = working_action
                        break

                    # Correction still fails — try again with updated auditor call
                    if attempt < settings.max_correction_retries:
                        business_state = await _fetch_business_state(corrected)
                        layer_b_result = await run_layer_b(corrected, business_state)
                        if layer_b_result.verdict != AuditVerdict.FAIL:
                            break
                else:
                    # All correction attempts exhausted
                    decision = GatewayDecision.REJECT
                    outcome = OutcomeCategory.BLOCKED_SEMANTIC
                    reasons = [layer_b_result.failure_reason or "Semantic audit failed; correction unsuccessful."]
                    escalation_ticket: str | None = None

                    if settings.enable_escalation:
                        decision = GatewayDecision.ESCALATE
                        outcome = OutcomeCategory.ESCALATED
                        item = _enqueue_escalation(action, layer_b_result, reasons)
                        escalation_ticket = item.request_id

                    await _record(
                        action, layer_a_result, layer_b_result, decision, outcome,
                        correction_applied, correction_attempts,
                        api_response, api_status_code, wall_start,
                    )

                    return FirewallResponse(
                        request_id=request_id,
                        decision=decision,
                        allowed=False,
                        outcome_category=outcome,
                        rejection_reasons=reasons,
                        escalation_ticket=escalation_ticket,
                        total_latency_ms=round((time.monotonic() - wall_start) * 1000, 2),
                        message="Request blocked by semantic auditor after failed correction.",
                    )

            else:
                # FAIL with no correctable payload (or correction not attempted) — always reject
                decision = GatewayDecision.REJECT
                outcome = OutcomeCategory.BLOCKED_SEMANTIC
                reasons = [layer_b_result.failure_reason or "Semantic audit failed."]
                escalation_ticket = None

                if settings.enable_escalation:
                    decision = GatewayDecision.ESCALATE
                    outcome = OutcomeCategory.ESCALATED
                    item = _enqueue_escalation(action, layer_b_result, reasons)
                    escalation_ticket = item.request_id

                log.warning(
                    "gateway.rejected_by_semantic",
                    request_id=request_id,
                    confidence=layer_b_result.confidence,
                    reason=reasons,
                )

                await _record(
                    action, layer_a_result, layer_b_result, decision, outcome,
                    correction_applied, correction_attempts,
                    api_response, api_status_code, wall_start,
                )

                return FirewallResponse(
                    request_id=request_id,
                    decision=decision,
                    allowed=False,
                    outcome_category=outcome,
                    rejection_reasons=reasons,
                    escalation_ticket=escalation_ticket,
                    total_latency_ms=round((time.monotonic() - wall_start) * 1000, 2),
                    message="Request blocked by semantic auditor.",
                )

        elif layer_b_result.verdict == AuditVerdict.UNCERTAIN:
            # Below confidence threshold → reject (fail closed)
            if layer_b_result.confidence < settings.semantic_confidence_threshold:
                decision = GatewayDecision.REJECT
                outcome = OutcomeCategory.BLOCKED_SEMANTIC
                reasons = ["Semantic auditor confidence below threshold — failing closed."]

                await _record(
                    action, layer_a_result, layer_b_result, decision, outcome,
                    correction_applied, correction_attempts,
                    api_response, api_status_code, wall_start,
                )

                return FirewallResponse(
                    request_id=request_id,
                    decision=decision,
                    allowed=False,
                    outcome_category=outcome,
                    rejection_reasons=reasons,
                    total_latency_ms=round((time.monotonic() - wall_start) * 1000, 2),
                    message="Request rejected — auditor could not confidently verify the action.",
                )

    # ── Step 3: ALLOW — Forward to Mock API ──────────────────────────────────
    override = correction_applied  # May be None if no correction was needed
    api_status_code, api_response = await _forward_to_api(action, request_id, override)

    if api_status_code in (200, 201):
        decision = GatewayDecision.AUTO_CORRECT if correction_applied else GatewayDecision.ALLOW
        outcome = OutcomeCategory.AUTO_CORRECTED if correction_applied else OutcomeCategory.SUCCESS
        log.info(
            "gateway.allowed",
            request_id=request_id,
            api_status=api_status_code,
            corrected=correction_applied is not None,
        )
    else:
        # API rejected despite passing validation — log as potential silent failure
        decision = GatewayDecision.ALLOW
        outcome = OutcomeCategory.SILENT_FAILURE
        log.error(
            "gateway.api_rejected_after_validation",
            request_id=request_id,
            api_status=api_status_code,
            api_response=api_response,
        )

    await _record(
        action, layer_a_result, layer_b_result, decision, outcome,
        correction_applied, correction_attempts,
        api_response, api_status_code, wall_start,
    )

    return FirewallResponse(
        request_id=request_id,
        decision=decision,
        allowed=True,
        outcome_category=outcome,
        api_response=api_response,
        corrected_payload=correction_applied,
        total_latency_ms=round((time.monotonic() - wall_start) * 1000, 2),
        message="Request processed successfully." if outcome == OutcomeCategory.SUCCESS
        else "Request auto-corrected and processed.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helper: Escalation Queue Management
# ─────────────────────────────────────────────────────────────────────────────


def _enqueue_escalation(
    action: AgentAction,
    layer_b_result: LayerBResult,
    reasons: list[str],
) -> EscalationItem:
    item = EscalationItem(
        request_id=action.request_id,
        customer_request=action.customer_request,
        action_type=action.action_type,
        generated_payload=action.payload.model_dump(mode="json"),
        rejection_reasons=reasons,
        layer_b_confidence=layer_b_result.confidence,
        layer_b_reason=layer_b_result.failure_reason or "",
    )
    _escalation_queue[item.request_id] = item
    log.info("gateway.escalated", request_id=item.request_id, reason=item.layer_b_reason)
    return item


# ─────────────────────────────────────────────────────────────────────────────
# Helper: Build and record a GatewayEvent
# ─────────────────────────────────────────────────────────────────────────────


async def _record(
    action: AgentAction,
    layer_a_result,
    layer_b_result: LayerBResult | None,
    decision: GatewayDecision,
    outcome: OutcomeCategory,
    correction_applied: dict[str, Any] | None,
    correction_attempts: int,
    api_response: dict[str, Any] | None,
    api_status_code: int | None,
    wall_start: float,
) -> None:
    event = GatewayEvent(
        request_id=action.request_id,
        action_type=action.action_type,
        customer_request=action.customer_request,
        generated_payload=action.payload.model_dump(mode="json"),
        layer_a_result=layer_a_result,
        layer_b_result=layer_b_result,
        gateway_decision=decision,
        correction_applied=correction_applied,
        correction_attempts=correction_attempts,
        api_response=api_response,
        api_status_code=api_status_code,
        total_latency_ms=round((time.monotonic() - wall_start) * 1000, 2),
        outcome_category=outcome,
    )
    await tracker.record_event(event)


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/actions",
    response_model=FirewallResponse,
    summary="Submit an agent action for validation and execution",
)
async def submit_action(action: AgentAction) -> FirewallResponse:
    """
    Primary gateway endpoint. The Chaos Agent submits every generated action here.
    The gateway validates, decides, and optionally forwards to the Mock API.
    """
    return await process_action(action)


@app.get("/health", summary="Gateway health check")
async def health() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": "semantic-firewall-gateway",
        "version": "1.0.0",
        "mock_api": settings.mock_api_base_url,
    }


@app.get("/metrics", summary="Return current aggregate metrics")
async def get_metrics() -> dict[str, Any]:
    """Return aggregate metrics computed from the audit log."""
    events = tracker.load_events()
    metrics = tracker.compute_metrics(events)
    return metrics.model_dump()


@app.get("/events", summary="Return recent audit events")
async def get_events(limit: int = 20) -> list[dict[str, Any]]:
    """Return the last N gateway events from the audit log."""
    events = tracker.load_events()
    return [e.model_dump(mode="json") for e in events[-limit:]]


# ─────────────────────────────────────────────────────────────────────────────
# Escalation Queue Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.get(
    "/escalations",
    response_model=list[EscalationItem],
    summary="List escalated transactions awaiting human review",
)
async def list_escalations(status: str = "pending") -> list[EscalationItem]:
    """Returns escalations filtered by status (pending | approved | rejected | all)."""
    items = list(_escalation_queue.values())
    if status != "all":
        items = [i for i in items if i.status.value == status]
    return sorted(items, key=lambda i: i.timestamp)


@app.get(
    "/escalations/{request_id}",
    response_model=EscalationItem,
    summary="Get a single escalation item",
)
async def get_escalation(request_id: str) -> EscalationItem:
    item = _escalation_queue.get(request_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Escalation {request_id!r} not found")
    return item


class ReviewDecision(BaseModel):
    decision: str  # "approve" | "reject"
    notes: str = ""


@app.post(
    "/escalations/{request_id}/review",
    response_model=EscalationItem,
    summary="Submit a human review decision on an escalated transaction",
)
async def review_escalation(request_id: str, body: ReviewDecision) -> EscalationItem:
    from datetime import datetime as _dt

    item = _escalation_queue.get(request_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Escalation {request_id!r} not found")
    if item.status != EscalationStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Escalation already reviewed: {item.status.value}")

    if body.decision == "approve":
        item.status = EscalationStatus.APPROVED
        item.reviewer_decision = GatewayDecision.ALLOW
    elif body.decision == "reject":
        item.status = EscalationStatus.REJECTED
        item.reviewer_decision = GatewayDecision.REJECT
    else:
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")

    item.reviewer_notes = body.notes
    item.reviewed_at = _dt.utcnow()

    log.info(
        "gateway.escalation_reviewed",
        request_id=request_id,
        decision=body.decision,
        notes=body.notes,
    )
    return item
