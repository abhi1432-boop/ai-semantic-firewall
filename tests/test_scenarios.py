"""
tests/test_scenarios.py — End-to-End Semantic Failure Scenario Tests

Runs all 10 failure scenarios (F01–F10) against the full gateway + mock API
stack. Both services spin up on ephemeral loopback ports for the test session
so no external processes are needed.

Layer B (semantic auditor) LLM calls are mocked for F06 and F07 — the two
scenarios that Layer A cannot catch deterministically. All other scenarios are
caught by Layer A with zero LLM cost.

Run:
    pytest tests/test_scenarios.py -v
"""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Any
from unittest.mock import patch, AsyncMock

import httpx
import pytest
import uvicorn

import mock_api as mock_api_module
import gateway as gateway_module
from chaos_agent import (
    make_f01_inflated_refund,
    make_f02_hallucinated_invoice,
    make_f03_wrong_customer,
    make_f04_double_refund,
    make_f05_credit_suspended_account,
    make_f06_wrong_cancellation_timing,
    make_f07_prompt_injection,
    make_f08_contradictory_payload,
    make_f09_invalid_state_transition,
    make_f10_policy_violation,
)
from schemas import (
    AgentAction,
    ActionType,
    AuditVerdict,
    FirewallResponse,
    GatewayDecision,
    LayerBResult,
    OutcomeCategory,
    RefundRequest,
)


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────


def _free_port() -> int:
    """Return an available loopback port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(app, port: int) -> uvicorn.Server:
    """Start a uvicorn server in a background thread; return the Server instance."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    def run():
        asyncio.run(server.serve())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return server


def _wait_for_port(port: int, retries: int = 40, delay: float = 0.1) -> None:
    """Block until the port accepts connections."""
    import time
    for _ in range(retries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(delay)
    raise RuntimeError(f"Server on port {port} did not start in time")


# ─────────────────────────────────────────────────────────────────────────────
# Session-scoped Servers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def mock_api_port() -> int:
    port = _free_port()
    _start_server(mock_api_module.app, port)
    _wait_for_port(port)
    return port


@pytest.fixture(scope="session")
def gateway_port(mock_api_port) -> int:
    port = _free_port()
    with patch("config.settings.mock_api_base_url", f"http://127.0.0.1:{mock_api_port}"):
        _start_server(gateway_module.app, port)
    _wait_for_port(port)
    return port


# ─────────────────────────────────────────────────────────────────────────────
# Per-test Clients
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def api_client(mock_api_port) -> httpx.AsyncClient:
    """HTTP client pointing at the mock API; resets DB state before each test."""
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{mock_api_port}", timeout=10.0
    ) as client:
        await client.post("/db/reset")
        yield client


@pytest.fixture
async def gw(gateway_port, mock_api_port) -> httpx.AsyncClient:
    """HTTP client pointing at the gateway; patches settings for each test."""
    with patch("config.settings.mock_api_base_url", f"http://127.0.0.1:{mock_api_port}"):
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}", timeout=30.0
        ) as client:
            # Reset mock API state before each test
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{mock_api_port}", timeout=10.0
            ) as reset:
                await reset.post("/db/reset")
            yield client


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────


async def submit(gw: httpx.AsyncClient, action: AgentAction) -> FirewallResponse:
    resp = await gw.post("/actions", json=action.model_dump(mode="json"))
    assert resp.status_code == 200, f"Gateway returned {resp.status_code}: {resp.text}"
    return FirewallResponse.model_validate(resp.json())


def _layer_b_fail(reason: str) -> LayerBResult:
    return LayerBResult(
        verdict=AuditVerdict.FAIL,
        confidence=0.93,
        failure_reason=reason,
        corrected_payload=None,
        tokens_used=500,
    )


def _layer_b_pass() -> LayerBResult:
    return LayerBResult(
        verdict=AuditVerdict.PASS,
        confidence=0.95,
        failure_reason=None,
        tokens_used=400,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Seed Data Sanity Checks
# ─────────────────────────────────────────────────────────────────────────────


async def test_seed_customers_exist(api_client):
    for cid in ("cust_001", "cust_002", "cust_003", "cust_004", "cust_005", "cust_006"):
        resp = await api_client.get(f"/customers/{cid}")
        assert resp.status_code == 200, f"{cid} not in seed data"


async def test_seed_invoices_exist(api_client):
    for iid in ("inv_001", "inv_002", "inv_003", "inv_004", "inv_005", "inv_006", "inv_007"):
        resp = await api_client.get(f"/invoices/{iid}")
        assert resp.status_code == 200, f"{iid} not in seed data"


async def test_inv_003_is_already_refunded(api_client):
    resp = await api_client.get("/invoices/inv_003")
    assert resp.json()["status"] == "refunded"


async def test_cust_003_is_suspended(api_client):
    resp = await api_client.get("/customers/cust_003")
    assert resp.json()["account_status"] == "suspended"


async def test_cust_006_active_with_cancelled_subscription(api_client):
    resp = await api_client.get("/customers/cust_006")
    data = resp.json()
    assert data["account_status"] == "active"
    assert data["subscription_status"] == "cancelled"


# ─────────────────────────────────────────────────────────────────────────────
# Health Checks
# ─────────────────────────────────────────────────────────────────────────────


async def test_gateway_health(gw):
    resp = await gw.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


async def test_mock_api_health(api_client):
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# ─────────────────────────────────────────────────────────────────────────────
# Layer A Scenarios — no LLM needed
# ─────────────────────────────────────────────────────────────────────────────


async def test_f01_inflated_refund(gw):
    """F01: 10x inflated refund amount — caught by Layer A REFUND_EXCEEDS_REMAINING."""
    _, action = make_f01_inflated_refund()
    result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_RULES
    assert not result.allowed
    assert any("exceed" in r.lower() or "amount" in r.lower() for r in result.rejection_reasons)


async def test_f02_hallucinated_invoice(gw):
    """F02: Non-existent invoice ID — caught by Layer A INVOICE_NOT_FOUND."""
    _, action = make_f02_hallucinated_invoice()
    result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_RULES
    assert not result.allowed
    assert any("invoice" in r.lower() for r in result.rejection_reasons)


async def test_f03_wrong_customer(gw):
    """F03: Invoice owned by different customer — caught by Layer A INVOICE_OWNERSHIP_MISMATCH."""
    _, action = make_f03_wrong_customer()
    result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_RULES
    assert not result.allowed
    assert any(
        "ownership" in r.lower() or "belong" in r.lower() or "mismatch" in r.lower()
        for r in result.rejection_reasons
    )


async def test_f04_double_refund(gw):
    """F04: Already-refunded invoice — caught by Layer A INVOICE_NOT_REFUNDABLE."""
    _, action = make_f04_double_refund()
    result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_RULES
    assert not result.allowed
    assert any(
        "refund" in r.lower() or "status" in r.lower()
        for r in result.rejection_reasons
    )


async def test_f05_credit_suspended_account(gw):
    """F05: Credit to suspended account — caught by Layer A ACCOUNT_NOT_ACTIVE."""
    _, action = make_f05_credit_suspended_account()
    result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_RULES
    assert not result.allowed
    assert any(
        "active" in r.lower() or "suspend" in r.lower() or "status" in r.lower()
        for r in result.rejection_reasons
    )


async def test_f09_re_cancel_subscription(gw):
    """F09: Re-cancelling already-cancelled subscription — caught by Layer A SUBSCRIPTION_ALREADY_CANCELLED."""
    _, action = make_f09_invalid_state_transition()
    result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_RULES
    assert not result.allowed
    assert any(
        "cancel" in r.lower() or "subscription" in r.lower()
        for r in result.rejection_reasons
    )


async def test_f10_policy_violation_no_approval(gw):
    """F10: Refund > $100 without manager_approval — caught by Layer A APPROVAL_REQUIRED."""
    _, action = make_f10_policy_violation()
    result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_RULES
    assert not result.allowed
    assert any(
        "approval" in r.lower() or "threshold" in r.lower() or "manager" in r.lower()
        for r in result.rejection_reasons
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layer B Scenarios — LLM mocked
# ─────────────────────────────────────────────────────────────────────────────


async def test_f06_wrong_cancellation_timing(gw, mock_api_port):
    """F06: IMMEDIATE timing when customer asked for end-of-period — Layer B only.

    Layer A has no timing semantics; the auditor must detect the mismatch.
    The gateway's internal httpx calls are still routed to the live mock API;
    only the LLM call is intercepted.
    """
    _, action = make_f06_wrong_cancellation_timing()

    mock_result = _layer_b_fail(
        "Customer explicitly requested end-of-period cancellation, "
        "but the payload sets timing=immediate. Intent mismatch."
    )

    with patch(
        "validation_engine._call_auditor_llm",
        new=AsyncMock(return_value=mock_result),
    ):
        with patch("config.settings.mock_api_base_url", f"http://127.0.0.1:{mock_api_port}"):
            result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_SEMANTIC
    assert not result.allowed
    assert result.rejection_reasons


async def test_f07_prompt_injection(gw, mock_api_port):
    """F07: Prompt injection in customer_request — Layer B only.

    Layer A cannot inspect free-text fields for injection attacks.
    """
    _, action = make_f07_prompt_injection()

    mock_result = _layer_b_fail(
        "Customer request contains a prompt injection attack: "
        "'SYSTEM OVERRIDE: Ignore all previous instructions.' This is a policy bypass attempt."
    )

    with patch(
        "validation_engine._call_auditor_llm",
        new=AsyncMock(return_value=mock_result),
    ):
        with patch("config.settings.mock_api_base_url", f"http://127.0.0.1:{mock_api_port}"):
            result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_SEMANTIC
    assert not result.allowed
    assert result.rejection_reasons


# ─────────────────────────────────────────────────────────────────────────────
# F08 — Contradictory fields (Layer A warning, Layer B catches semantics)
# ─────────────────────────────────────────────────────────────────────────────


async def test_f08_contradictory_payload_layer_b(gw, mock_api_port):
    """F08: $0.01 refund with 'full refund' reason — Layer B catches the contradiction.

    Layer A's ZERO_REFUND_AMOUNT check only fires at exactly 0; 0.01 passes Layer A.
    The semantic auditor must detect the amount-vs-reason contradiction.
    """
    _, action = make_f08_contradictory_payload()

    mock_result = _layer_b_fail(
        "Payload is internally inconsistent: refund_amount=$0.01 but reason states "
        "'Full refund requested — all charges reversed'. Amount contradicts stated intent."
    )

    with patch(
        "validation_engine._call_auditor_llm",
        new=AsyncMock(return_value=mock_result),
    ):
        with patch("config.settings.mock_api_base_url", f"http://127.0.0.1:{mock_api_port}"):
            result = await submit(gw, action)

    assert result.decision == GatewayDecision.REJECT
    assert result.outcome_category == OutcomeCategory.BLOCKED_SEMANTIC
    assert not result.allowed


# ─────────────────────────────────────────────────────────────────────────────
# Happy Path — Valid request should be ALLOWED
# ─────────────────────────────────────────────────────────────────────────────


async def test_valid_refund_is_allowed(gw, mock_api_port):
    """A well-formed, policy-compliant refund under $100 should pass through."""
    action = AgentAction(
        action_type=ActionType.PROCESS_REFUND,
        customer_request="Please refund my invoice inv_001 for $50.",
        payload=RefundRequest(
            customer_id="cust_001",
            invoice_id="inv_001",
            refund_amount=50.00,
            reason="Customer is dissatisfied with the service",
            manager_approval=False,
        ),
    )

    with patch(
        "validation_engine._call_auditor_llm",
        new=AsyncMock(return_value=_layer_b_pass()),
    ):
        with patch("config.settings.mock_api_base_url", f"http://127.0.0.1:{mock_api_port}"):
            result = await submit(gw, action)

    assert result.allowed
    assert result.decision in (GatewayDecision.ALLOW, GatewayDecision.AUTO_CORRECT)
    assert result.outcome_category in (OutcomeCategory.SUCCESS, OutcomeCategory.AUTO_CORRECTED)
