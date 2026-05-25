"""
mock_api.py — Mock Enterprise CRM/Billing API

A strict, fully asynchronous FastAPI service that simulates a deterministic
enterprise backend. This API enforces hard business rules and represents the
TRUSTED ZONE — it assumes all requests reaching it have been pre-validated
by the Semantic Firewall Gateway.

The API maintains an in-memory database seeded with realistic test data.
State is preserved in-process and reset on restart (intentional for testing).

Run standalone:
    uvicorn mock_api:app --port 8001 --reload
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from schemas import (
    AccountStatus,
    APIError,
    CancellationResponse,
    CancellationTiming,
    CreditRequest,
    CreditResponse,
    CustomerRecord,
    CustomerResponse,
    Invoice,
    InvoiceResponse,
    InvoiceStatus,
    RefundRequest,
    RefundResponse,
    SubscriptionCancellationRequest,
    SubscriptionStatus,
    SubscriptionTier,
    SubscriptionUpdateRequest,
)

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(),
    ],
)
log = structlog.get_logger("mock_api")


# ─────────────────────────────────────────────────────────────────────────────
# In-Memory Database
# ─────────────────────────────────────────────────────────────────────────────

_CUSTOMERS: dict[str, CustomerRecord] = {}
_INVOICES: dict[str, Invoice] = {}


def _seed_database() -> None:
    """Populate the in-memory store with deterministic test fixtures."""
    now = datetime.utcnow()

    customers_data = [
        CustomerRecord(
            customer_id="cust_001",
            name="Alice Sharma",
            email="alice@example.com",
            account_status=AccountStatus.ACTIVE,
            account_balance=50.00,
            subscription_tier=SubscriptionTier.PROFESSIONAL,
            subscription_status=SubscriptionStatus.ACTIVE,
            created_at=now - timedelta(days=365),
        ),
        CustomerRecord(
            customer_id="cust_002",
            name="Bob Tanaka",
            email="bob@example.com",
            account_status=AccountStatus.ACTIVE,
            account_balance=0.00,
            subscription_tier=SubscriptionTier.STARTER,
            subscription_status=SubscriptionStatus.ACTIVE,
            created_at=now - timedelta(days=180),
        ),
        CustomerRecord(
            customer_id="cust_003",
            name="Carol Mensah",
            email="carol@example.com",
            account_status=AccountStatus.SUSPENDED,
            account_balance=25.00,
            subscription_tier=SubscriptionTier.STARTER,
            subscription_status=SubscriptionStatus.PAST_DUE,
            created_at=now - timedelta(days=90),
        ),
        CustomerRecord(
            customer_id="cust_004",
            name="David Chen",
            email="david@example.com",
            account_status=AccountStatus.ACTIVE,
            account_balance=200.00,
            subscription_tier=SubscriptionTier.ENTERPRISE,
            subscription_status=SubscriptionStatus.ACTIVE,
            created_at=now - timedelta(days=730),
        ),
        CustomerRecord(
            customer_id="cust_005",
            name="Eve Rodriguez",
            email="eve@example.com",
            account_status=AccountStatus.CLOSED,
            account_balance=0.00,
            subscription_tier=SubscriptionTier.FREE,
            subscription_status=SubscriptionStatus.CANCELLED,
            created_at=now - timedelta(days=500),
        ),
        # cust_006: ACTIVE account but already-cancelled subscription — used by F09
        CustomerRecord(
            customer_id="cust_006",
            name="Frank Okafor",
            email="frank@example.com",
            account_status=AccountStatus.ACTIVE,
            account_balance=0.00,
            subscription_tier=SubscriptionTier.FREE,
            subscription_status=SubscriptionStatus.CANCELLED,
            created_at=now - timedelta(days=120),
        ),
    ]
    for c in customers_data:
        _CUSTOMERS[c.customer_id] = c

    invoices_data = [
        # cust_001 invoices
        Invoice(
            invoice_id="inv_001",
            customer_id="cust_001",
            amount=99.00,
            status=InvoiceStatus.PAID,
            description="Professional Plan — Monthly",
            refunded_amount=0.0,
            created_at=now - timedelta(days=30),
            paid_at=now - timedelta(days=29),
        ),
        Invoice(
            invoice_id="inv_002",
            customer_id="cust_001",
            amount=20.00,
            status=InvoiceStatus.PAID,
            description="Add-on: Extra API Calls",
            refunded_amount=0.0,
            created_at=now - timedelta(days=15),
            paid_at=now - timedelta(days=14),
        ),
        Invoice(
            invoice_id="inv_003",
            customer_id="cust_001",
            amount=99.00,
            status=InvoiceStatus.REFUNDED,
            description="Professional Plan — Previous Month",
            refunded_amount=99.00,
            created_at=now - timedelta(days=60),
            paid_at=now - timedelta(days=59),
        ),
        # cust_002 invoices
        Invoice(
            invoice_id="inv_004",
            customer_id="cust_002",
            amount=29.00,
            status=InvoiceStatus.PAID,
            description="Starter Plan — Monthly",
            refunded_amount=0.0,
            created_at=now - timedelta(days=10),
            paid_at=now - timedelta(days=9),
        ),
        Invoice(
            invoice_id="inv_005",
            customer_id="cust_002",
            amount=150.00,
            status=InvoiceStatus.PAID,
            description="Annual Starter Plan",
            refunded_amount=0.0,
            created_at=now - timedelta(days=180),
            paid_at=now - timedelta(days=179),
        ),
        # cust_004 invoices
        Invoice(
            invoice_id="inv_006",
            customer_id="cust_004",
            amount=499.00,
            status=InvoiceStatus.PAID,
            description="Enterprise Plan — Monthly",
            refunded_amount=0.0,
            created_at=now - timedelta(days=5),
            paid_at=now - timedelta(days=4),
        ),
        Invoice(
            invoice_id="inv_007",
            customer_id="cust_004",
            amount=50.00,
            status=InvoiceStatus.PARTIALLY_REFUNDED,
            description="Setup Fee",
            refunded_amount=25.00,
            created_at=now - timedelta(days=200),
            paid_at=now - timedelta(days=199),
        ),
        # Large invoice used by adversarial boundary-value tests
        Invoice(
            invoice_id="inv_008",
            customer_id="cust_001",
            amount=200.00,
            status=InvoiceStatus.PAID,
            description="Enterprise Plan — Annual",
            refunded_amount=0.0,
            created_at=now - timedelta(days=5),
            paid_at=now - timedelta(days=4),
        ),
    ]
    for inv in invoices_data:
        _INVOICES[inv.invoice_id] = inv

    log.info("database.seeded", customers=len(_CUSTOMERS), invoices=len(_INVOICES))


# ─────────────────────────────────────────────────────────────────────────────
# Application Lifecycle
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    _seed_database()
    log.info("mock_api.startup", message="Mock Enterprise API is ready")
    yield
    log.info("mock_api.shutdown", message="Mock Enterprise API shutting down")


app = FastAPI(
    title="Mock Enterprise CRM/Billing API",
    description=(
        "Deterministic enterprise backend for the AI Semantic Firewall project. "
        "Enforces strict business rules — all requests are assumed pre-validated."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Middleware — Request Tracing
# ─────────────────────────────────────────────────────────────────────────────


@app.middleware("http")
async def request_trace_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    log.info(
        "request.received",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    log.info(
        "request.completed",
        request_id=request_id,
        status_code=response.status_code,
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Exception Handlers
# ─────────────────────────────────────────────────────────────────────────────


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    error = APIError(
        error_code=f"HTTP_{exc.status_code}",
        message=exc.detail,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=error.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dependency — Extract Request ID
# ─────────────────────────────────────────────────────────────────────────────


async def get_request_id(
    x_request_id: Annotated[str | None, Header()] = None,
) -> str:
    return x_request_id or str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────────
# Helper: Business Rule Assertions
# ─────────────────────────────────────────────────────────────────────────────

# Maximum refund allowed without manager pre-approval
REFUND_APPROVAL_THRESHOLD = 100.00


def _require_customer(customer_id: str, request_id: str) -> CustomerRecord:
    """Fetch customer or raise 404."""
    customer = _CUSTOMERS.get(customer_id)
    if not customer:
        log.warning("customer.not_found", customer_id=customer_id, request_id=request_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer '{customer_id}' does not exist.",
        )
    return customer


def _require_invoice(invoice_id: str, request_id: str) -> Invoice:
    """Fetch invoice or raise 404."""
    invoice = _INVOICES.get(invoice_id)
    if not invoice:
        log.warning("invoice.not_found", invoice_id=invoice_id, request_id=request_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice '{invoice_id}' does not exist.",
        )
    return invoice


def _require_invoice_ownership(
    invoice: Invoice, customer_id: str, request_id: str
) -> None:
    """Ensure the invoice belongs to the requesting customer."""
    if invoice.customer_id != customer_id:
        log.warning(
            "invoice.ownership_mismatch",
            invoice_id=invoice.invoice_id,
            invoice_owner=invoice.customer_id,
            requesting_customer=customer_id,
            request_id=request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Invoice '{invoice.invoice_id}' does not belong to "
                f"customer '{customer_id}'."
            ),
        )


def _require_account_active(customer: CustomerRecord, request_id: str) -> None:
    """Raise 403 if account is not in ACTIVE state."""
    if customer.account_status != AccountStatus.ACTIVE:
        log.warning(
            "customer.account_not_active",
            customer_id=customer.customer_id,
            status=customer.account_status,
            request_id=request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Account '{customer.customer_id}' has status "
                f"'{customer.account_status.value}' — operation not permitted."
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Customer Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.get(
    "/customers/{customer_id}",
    response_model=CustomerResponse,
    summary="Look up a customer by ID",
)
async def get_customer(
    customer_id: str,
    request_id: Annotated[str, Depends(get_request_id)],
) -> CustomerResponse:
    """Return basic customer information. Safe read-only operation."""
    customer = _require_customer(customer_id, request_id)
    return CustomerResponse(
        customer_id=customer.customer_id,
        name=customer.name,
        email=customer.email,
        account_status=customer.account_status,
        account_balance=customer.account_balance,
        subscription_tier=customer.subscription_tier,
        subscription_status=customer.subscription_status,
    )


@app.get(
    "/customers",
    response_model=list[CustomerResponse],
    summary="List all customers (admin use)",
)
async def list_customers() -> list[CustomerResponse]:
    """Return all customers. Used by Validation Engine to check state."""
    return [
        CustomerResponse(
            customer_id=c.customer_id,
            name=c.name,
            email=c.email,
            account_status=c.account_status,
            account_balance=c.account_balance,
            subscription_tier=c.subscription_tier,
            subscription_status=c.subscription_status,
        )
        for c in _CUSTOMERS.values()
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Invoice Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceResponse,
    summary="Retrieve an invoice by ID",
)
async def get_invoice(
    invoice_id: str,
    request_id: Annotated[str, Depends(get_request_id)],
) -> InvoiceResponse:
    """Return invoice details. Read-only operation."""
    invoice = _require_invoice(invoice_id, request_id)
    return InvoiceResponse(
        invoice_id=invoice.invoice_id,
        customer_id=invoice.customer_id,
        amount=invoice.amount,
        status=invoice.status,
        description=invoice.description,
        refunded_amount=invoice.refunded_amount,
        created_at=invoice.created_at,
        paid_at=invoice.paid_at,
    )


@app.get(
    "/customers/{customer_id}/invoices",
    response_model=list[InvoiceResponse],
    summary="List invoices for a customer",
)
async def list_customer_invoices(
    customer_id: str,
    request_id: Annotated[str, Depends(get_request_id)],
) -> list[InvoiceResponse]:
    """Return all invoices owned by a customer."""
    _require_customer(customer_id, request_id)
    invoices = [inv for inv in _INVOICES.values() if inv.customer_id == customer_id]
    return [
        InvoiceResponse(
            invoice_id=inv.invoice_id,
            customer_id=inv.customer_id,
            amount=inv.amount,
            status=inv.status,
            description=inv.description,
            refunded_amount=inv.refunded_amount,
            created_at=inv.created_at,
            paid_at=inv.paid_at,
        )
        for inv in invoices
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Refund Endpoint
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/refunds",
    response_model=RefundResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Process a customer refund",
)
async def process_refund(
    refund_req: RefundRequest,
    request_id: Annotated[str, Depends(get_request_id)],
) -> RefundResponse:
    """
    Process a refund against a paid invoice.

    Business rules enforced:
    - Customer must exist and account must be ACTIVE.
    - Invoice must exist and belong to the requesting customer.
    - Invoice must be in PAID or PARTIALLY_REFUNDED status.
    - Refund amount must not exceed remaining refundable amount.
    - Refunds above $100 require manager_approval=true.
    """
    customer = _require_customer(refund_req.customer_id, request_id)
    invoice = _require_invoice(refund_req.invoice_id, request_id)

    _require_account_active(customer, request_id)
    _require_invoice_ownership(invoice, refund_req.customer_id, request_id)

    # Invoice must be in a refundable state
    refundable_statuses = {InvoiceStatus.PAID, InvoiceStatus.PARTIALLY_REFUNDED}
    if invoice.status not in refundable_statuses:
        log.warning(
            "refund.invalid_invoice_status",
            invoice_id=invoice.invoice_id,
            invoice_status=invoice.status,
            request_id=request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Invoice '{invoice.invoice_id}' has status "
                f"'{invoice.status.value}' and cannot be refunded."
            ),
        )

    # Cannot refund more than remains on the invoice
    remaining = round(invoice.amount - invoice.refunded_amount, 2)
    if refund_req.refund_amount > remaining:
        log.warning(
            "refund.exceeds_remaining",
            invoice_id=invoice.invoice_id,
            requested=refund_req.refund_amount,
            remaining=remaining,
            request_id=request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Refund amount ${refund_req.refund_amount:.2f} exceeds "
                f"remaining refundable amount ${remaining:.2f} on invoice "
                f"'{invoice.invoice_id}'."
            ),
        )

    # High-value refunds require explicit manager approval
    if refund_req.refund_amount > REFUND_APPROVAL_THRESHOLD and not refund_req.manager_approval:
        log.warning(
            "refund.approval_required",
            amount=refund_req.refund_amount,
            threshold=REFUND_APPROVAL_THRESHOLD,
            request_id=request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Refunds above ${REFUND_APPROVAL_THRESHOLD:.2f} require "
                f"manager_approval=true. Requested: ${refund_req.refund_amount:.2f}."
            ),
        )

    # Apply the refund — mutate in-memory state
    new_refunded_total = round(invoice.refunded_amount + refund_req.refund_amount, 2)
    if new_refunded_total >= invoice.amount:
        new_status = InvoiceStatus.REFUNDED
    else:
        new_status = InvoiceStatus.PARTIALLY_REFUNDED

    _INVOICES[invoice.invoice_id] = invoice.model_copy(
        update={"refunded_amount": new_refunded_total, "status": new_status}
    )

    log.info(
        "refund.processed",
        customer_id=refund_req.customer_id,
        invoice_id=refund_req.invoice_id,
        amount=refund_req.refund_amount,
        new_invoice_status=new_status,
        request_id=request_id,
    )

    return RefundResponse(
        customer_id=refund_req.customer_id,
        invoice_id=refund_req.invoice_id,
        refund_amount=refund_req.refund_amount,
        new_invoice_status=new_status,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Account Credit Endpoint
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/credits",
    response_model=CreditResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue an account credit",
)
async def issue_credit(
    credit_req: CreditRequest,
    request_id: Annotated[str, Depends(get_request_id)],
) -> CreditResponse:
    """
    Add a credit balance to a customer account.

    Business rules enforced:
    - Customer must exist.
    - Account must be ACTIVE (suspended accounts cannot receive credits).
    - Credit amount must be positive.
    """
    customer = _require_customer(credit_req.customer_id, request_id)
    _require_account_active(customer, request_id)

    new_balance = round(customer.account_balance + credit_req.credit_amount, 2)
    _CUSTOMERS[customer.customer_id] = customer.model_copy(
        update={"account_balance": new_balance}
    )

    log.info(
        "credit.issued",
        customer_id=credit_req.customer_id,
        amount=credit_req.credit_amount,
        new_balance=new_balance,
        request_id=request_id,
    )

    return CreditResponse(
        customer_id=credit_req.customer_id,
        credit_amount=credit_req.credit_amount,
        new_balance=new_balance,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Subscription Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.post(
    "/subscriptions/cancel",
    response_model=CancellationResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel a customer subscription",
)
async def cancel_subscription(
    cancel_req: SubscriptionCancellationRequest,
    request_id: Annotated[str, Depends(get_request_id)],
) -> CancellationResponse:
    """
    Cancel a customer's subscription.

    Business rules enforced:
    - Customer must exist and account must be ACTIVE.
    - Subscription must not already be CANCELLED.
    - Requires confirm=true in the payload.
    """
    customer = _require_customer(cancel_req.customer_id, request_id)
    _require_account_active(customer, request_id)

    if customer.subscription_status == SubscriptionStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Customer '{cancel_req.customer_id}' subscription is already cancelled.",
        )

    if cancel_req.timing == CancellationTiming.IMMEDIATE:
        effective_date = datetime.utcnow()
        new_status = SubscriptionStatus.CANCELLED
    else:
        # End of current billing period — approximate as 30 days
        effective_date = datetime.utcnow() + timedelta(days=30)
        new_status = SubscriptionStatus.CANCELLED

    _CUSTOMERS[customer.customer_id] = customer.model_copy(
        update={"subscription_status": new_status}
    )

    log.info(
        "subscription.cancelled",
        customer_id=cancel_req.customer_id,
        timing=cancel_req.timing,
        effective_date=effective_date.isoformat(),
        request_id=request_id,
    )

    return CancellationResponse(
        customer_id=cancel_req.customer_id,
        timing=cancel_req.timing,
        effective_date=effective_date,
    )


@app.post(
    "/subscriptions/update",
    response_model=dict[str, Any],
    status_code=status.HTTP_200_OK,
    summary="Update a customer's subscription tier",
)
async def update_subscription(
    update_req: SubscriptionUpdateRequest,
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """
    Change subscription tier for a customer.

    Business rules enforced:
    - Customer must exist and account must be ACTIVE.
    - New tier must differ from current tier.
    """
    customer = _require_customer(update_req.customer_id, request_id)
    _require_account_active(customer, request_id)

    if customer.subscription_tier == update_req.new_tier:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Customer '{update_req.customer_id}' is already on the "
                f"'{update_req.new_tier.value}' tier."
            ),
        )

    old_tier = customer.subscription_tier
    _CUSTOMERS[customer.customer_id] = customer.model_copy(
        update={"subscription_tier": update_req.new_tier}
    )

    log.info(
        "subscription.updated",
        customer_id=update_req.customer_id,
        old_tier=old_tier,
        new_tier=update_req.new_tier,
        request_id=request_id,
    )

    return {
        "customer_id": update_req.customer_id,
        "old_tier": old_tier.value,
        "new_tier": update_req.new_tier.value,
        "message": "Subscription tier updated successfully",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Health & Diagnostics
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/health", summary="Health check")
async def health_check() -> dict[str, str]:
    return {"status": "healthy", "service": "mock-enterprise-api", "version": "1.0.0"}


@app.get("/db/state", summary="Return full in-memory DB state (debug only)")
async def get_db_state() -> dict[str, Any]:
    """Expose full database state for testing and validation engine queries."""
    return {
        "customers": {k: v.model_dump(mode="json") for k, v in _CUSTOMERS.items()},
        "invoices": {k: v.model_dump(mode="json") for k, v in _INVOICES.items()},
    }


@app.post("/db/reset", summary="Reset database to seed state (testing only)")
async def reset_database() -> dict[str, str]:
    """Wipe and re-seed the in-memory database. Used between test runs."""
    _CUSTOMERS.clear()
    _INVOICES.clear()
    _seed_database()
    log.info("database.reset")
    return {"status": "reset", "customers": str(len(_CUSTOMERS)), "invoices": str(len(_INVOICES))}
