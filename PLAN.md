# AI Semantic Firewall — Production Architecture & Roadmap

## 1. System Overview

The AI Semantic Firewall is an independent middleware enforcement layer that intercepts agent-generated API calls, validates them for semantic correctness, and prevents unsafe transactions from reaching production enterprise systems.

The core problem it solves: LLM agents can generate payloads that pass JSON schema validation but are semantically wrong — wrong amounts, hallucinated IDs, violated business rules, or intent misalignment. Traditional API validation does not catch these.

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          AI SEMANTIC FIREWALL SYSTEM                        │
└─────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────┐       ┌────────────────────────────────────────────┐
  │   Customer Input  │──────▶│             CHAOS AGENT                   │
  │  (Support Request)│       │  (LLM-powered, intentionally unreliable)  │
  └──────────────────┘       └────────────────────┬───────────────────────┘
                                                   │
                                    Agent NEVER calls API directly
                                                   │
                                                   ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    SEMANTIC FIREWALL GATEWAY (gateway.py)                │
  │                                                                          │
  │   ┌──────────────────────────────────────────────────────────────────┐  │
  │   │                   VALIDATION ENGINE (validation_engine.py)       │  │
  │   │                                                                  │  │
  │   │  ┌─────────────────────────┐    ┌──────────────────────────────┐│  │
  │   │  │  LAYER A: Deterministic │    │  LAYER B: Semantic Auditor   ││  │
  │   │  │  Rule Validation        │    │  (Independent LLM Judge)     ││  │
  │   │  │                         │    │                              ││  │
  │   │  │  • Pydantic schema      │───▶│  • Intent alignment          ││  │
  │   │  │  • Business rules       │    │  • Hallucination detection   ││  │
  │   │  │  • State transitions    │    │  • Policy reasoning          ││  │
  │   │  │  • Field constraints    │    │  • Confidence scoring        ││  │
  │   │  │  • Policy enforcement   │    │  • Correction suggestions    ││  │
  │   │  └─────────────────────────┘    └──────────────────────────────┘│  │
  │   └──────────────────────────────────────────────────────────────────┘  │
  │                                                                          │
  │   Decision Engine:                                                       │
  │   ALLOW │ REJECT │ AUTO-CORRECT │ REQUEST-REGENERATION │ ESCALATE       │
  └──────────┬───────────────────────────────────────────────────────────────┘
             │ (only ALLOW reaches here)
             ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    MOCK ENTERPRISE API (mock_api.py)                     │
  │                                                                          │
  │   /customers/{id}  /invoices/{id}  /refunds  /credits  /subscriptions   │
  │   Strict deterministic business logic — final enforcement boundary       │
  └──────────────────────────────────────────────────────────────────────────┘
             │
             ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                    METRICS + AUDIT TRACKER (tracker.py)                  │
  │   Every transaction logged: outcome, latency, scores, corrections        │
  └──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Trust Boundaries

```
UNTRUSTED ZONE                    ENFORCEMENT ZONE              TRUSTED ZONE
─────────────────────────────────────────────────────────────────────────────
│  Chaos Agent          │──────▶│  Semantic Firewall    │──────▶│  Mock API │
│  (LLM output)         │       │  Gateway + Engine     │       │  (backend)│
│  • May hallucinate    │       │  • Policy enforcement │       │  • Final  │
│  • May inject prompts │       │  • Semantic auditing  │       │    rules  │
│  • May misalign       │       │  • State validation   │       │           │
─────────────────────────────────────────────────────────────────────────────

The Firewall is the ONLY trust boundary between the agent and the backend.
The backend MUST assume any request reaching it has been pre-validated.
The agent MUST assume all actions may be blocked or corrected.
```

---

## 4. Repository Structure

```
ai-semantic-firewall/
├── PLAN.md                    # This document
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
├── README.md                  # Quick-start guide
│
├── schemas.py                 # Shared Pydantic models (single source of truth)
├── mock_api.py                # FastAPI enterprise backend simulation
├── gateway.py                 # Semantic Firewall enforcement gateway
├── validation_engine.py       # Layered validation: rules + semantic auditor
├── chaos_agent.py             # Intentionally unreliable LLM agent
├── tracker.py                 # Metrics, logging, audit trail
│
├── config.py                  # Centralized configuration (env vars, constants)
├── prompts.py                 # Auditor system prompts (isolated from agent)
│
├── tests/
│   ├── test_mock_api.py       # Backend rule enforcement tests
│   ├── test_validation.py     # Rule layer unit tests
│   ├── test_semantic.py       # Auditor accuracy tests
│   ├── test_gateway.py        # Gateway integration tests
│   ├── test_scenarios.py      # End-to-end semantic failure scenarios
│   └── fixtures/
│       ├── sample_requests.json
│       └── expected_outcomes.json
│
├── scripts/
│   ├── run_demo.py            # Run all 10 failure scenarios
│   ├── evaluate_metrics.py    # Print accuracy/cost/latency report
│   └── seed_database.py       # Populate mock API with test data
│
└── reports/
    └── audit_log.jsonl        # Append-only audit trail (runtime generated)
```

---

## 5. Data & Control Flow

```
REQUEST LIFECYCLE
─────────────────

1. Customer submits support request (natural language)
2. Chaos Agent processes request, decides action, generates JSON payload
3. Agent submits payload to Firewall Gateway (NEVER directly to API)

4. Gateway receives payload + context metadata
   a. Logs incoming transaction (tracker)
   b. Runs Layer A: Deterministic Rule Validation
      - Schema validation (Pydantic)
      - Business rule checks (amount limits, state machines, ownership)
      - If FAIL → REJECT immediately, log reason, return to agent
   c. Runs Layer B: Semantic Auditor (independent LLM)
      - Constructs auditor context: original request + payload + state + policy
      - LLM judge evaluates alignment and correctness
      - Returns: PASS/FAIL + confidence score + failure reason + optional correction
      - If FAIL + correctable → AUTO-CORRECT and re-validate
      - If FAIL + not correctable → REJECT or ESCALATE
   d. If both PASS → ALLOW: forward to Mock API

5. Mock API executes action (final enforcement boundary)
6. Response returned through Gateway to Agent
7. All metadata logged to tracker (outcome, latency, scores, corrections)
```

---

## 6. Validation Pipeline Deep Dive

### Layer A: Deterministic Rules (Zero LLM cost)

| Check | Rule | Error |
|---|---|---|
| Customer existence | customer_id must exist in DB | 404 Not Found |
| Invoice ownership | invoice.customer_id must match request customer_id | 403 Forbidden |
| Refund amount | refund_amount <= invoice.amount | 400 Bad Request |
| Refund limit | refund_amount <= $100 OR has manager_approval=true | 403 Policy |
| Duplicate refund | invoice.status != REFUNDED | 409 Conflict |
| Account status | account.status != SUSPENDED for credit ops | 403 Forbidden |
| Invoice status | invoice must be in valid state for requested operation | 400 |
| Amount range | 0 < amount <= 10000 | 400 |
| Cancellation timing | subscription not already cancelled | 409 |

### Layer B: Semantic Auditor (LLM cost per uncertain request)

The auditor receives a structured context bundle and must answer:

1. Does the action match what the customer actually requested?
2. Are there signs of hallucination (invented IDs, wrong customer, wrong amounts)?
3. Does the action violate any stated business policy in non-obvious ways?
4. Are the field values internally consistent (no contradictory parameters)?
5. Could this be a prompt injection attempting to override policy?

The auditor outputs a structured JSON verdict with confidence score 0.0–1.0.

Auditor isolation: The auditor uses a different model, different system prompt, and has no access to the agent's internal state. This prevents correlated failure.

---

## 7. Failure Mode Taxonomy

| ID | Scenario | Caught By |
|---|---|---|
| F01 | Wrong refund amount (10x inflation) | Layer A (amount constraint) |
| F02 | Hallucinated invoice ID | Layer A (ownership check) |
| F03 | Wrong customer account | Layer A (ownership) + Layer B |
| F04 | Double refund | Layer A (status check) |
| F05 | Credit to suspended account | Layer A (status check) |
| F06 | Immediate vs delayed cancellation | Layer B only |
| F07 | Prompt injection policy override | Layer B only |
| F08 | Contradictory payload fields | Layer A (field logic) + Layer B |
| F09 | Invalid action sequence | Layer A (state machine) |
| F10 | Schema-valid but policy-violating | Layer B only |

---

## 8. Milestone Roadmap

### Phase 1 — Foundation ✅
- [x] `PLAN.md` — architecture + roadmap
- [x] `requirements.txt` — dependency manifest
- [x] `schemas.py` — shared type system
- [x] `mock_api.py` — deterministic enterprise backend

### Phase 2 — Core Firewall ✅
- [x] `validation_engine.py` — Layer A deterministic rules
- [x] `gateway.py` — middleware enforcement layer
- [x] `tracker.py` — audit logging + metrics
- [x] `config.py` — environment + constants

### Phase 3 — Agent + Auditor ✅
- [x] `chaos_agent.py` — unreliable LLM agent
- [x] `validation_engine.py` — Layer B semantic auditor
- [x] `prompts.py` — auditor system prompts
- [x] End-to-end integration test (`tests/test_scenarios.py` — 18/18 passing)

### Phase 4 — Evaluation ✅
- [x] All 10 failure scenarios as automated tests (`tests/test_scenarios.py` — 18/18)
- [x] Metrics dashboard via `rich` (`tracker.py` + `scripts/evaluate_metrics.py`)
- [x] Accuracy/cost/latency report (`scripts/evaluate_metrics.py`)
- [x] Detection rate analysis (`scripts/run_demo.py` — 10/10 detected, 0 missed, 1 false positive)

### Phase 5 — Advanced Extensions ✅ (core items)
- [x] Self-correction loop — `chaos_agent.py`: `run_live_with_correction()` + `--live-correct` CLI flag
- [x] Human-in-the-loop escalation queue — `gateway.py`: `/escalations` endpoints + `scripts/review_escalations.py`
- [x] Adversarial prompt injection suite — `tests/test_adversarial.py` (14 tests: injections, boundary values, replay, unicode obfuscation, cross-customer)
- [ ] Policy learning from blocked transactions
- [ ] Multi-agent cross-validation
- [ ] Real-time Prometheus/Grafana metrics

---

## 9. Testing Strategy

### Unit Tests
- Every business rule in isolation
- Schema validation edge cases
- Auditor prompt formatting
- Tracker serialization

### Integration Tests
- Gateway → Validation Engine pipeline
- Gateway → Mock API forwarding
- Agent → Gateway → API full chain

### Scenario Tests (Semantic Failure Suite)
- All 10 failure modes with known expected outcomes
- Detection rate measurement
- False positive measurement (valid requests incorrectly blocked)

### Adversarial Tests
- Prompt injection variants
- Boundary amount values ($99.99, $100.00, $100.01)
- Malformed but structurally valid payloads
- Replay attacks (resubmitting blocked requests)

---

## 10. Observability & Logging Strategy

Every transaction emits a structured JSON log entry:

```json
{
  "request_id": "uuid4",
  "timestamp": "ISO8601",
  "customer_request": "...",
  "generated_payload": {...},
  "layer_a_result": {"passed": true|false, "violations": [...]},
  "layer_b_result": {"verdict": "PASS|FAIL", "confidence": 0.0-1.0, "reason": "..."},
  "gateway_decision": "ALLOW|REJECT|CORRECT|ESCALATE",
  "correction_applied": {...} | null,
  "api_response": {...} | null,
  "latency_ms": 142,
  "auditor_tokens_used": 850,
  "outcome_category": "Success|Blocked-Rules|Blocked-Semantic|Auto-Corrected|Escalated"
}
```

Log destinations:
- `structlog` → stdout (development)
- `reports/audit_log.jsonl` → append-only file (local persistence)
- Future: OpenTelemetry → Jaeger/Grafana (production)

---

## 11. Configuration & Environment

```bash
# LLM Provider
OPENAI_API_KEY=sk-...
AUDITOR_MODEL=gpt-4o-mini        # Auditor (isolated from agent)
AGENT_MODEL=gpt-4o               # Chaos Agent

# Firewall Thresholds
SEMANTIC_CONFIDENCE_THRESHOLD=0.75   # Below this → REJECT
AUTO_CORRECT_THRESHOLD=0.85          # Above this → attempt auto-correction
MAX_CORRECTION_RETRIES=2

# Service Ports
MOCK_API_PORT=8001
GATEWAY_PORT=8000

# Feature Flags
ENABLE_SEMANTIC_AUDITOR=true
ENABLE_AUTO_CORRECTION=true
ENABLE_ESCALATION=false         # Phase 5 feature
```

---

## 12. Advanced Extensions (Future Phases)

### Self-Correction Loop
When the auditor returns a CORRECTED_PAYLOAD with high confidence, the gateway automatically substitutes the corrected values, re-runs validation, and forwards if clean. Logged as AUTO-CORRECTED.

### Human-in-the-Loop Escalation
Transactions flagged ESCALATE are placed in a review queue. A simulated human reviewer approves/rejects. Builds dataset for policy learning.

### Policy Learning
Blocked transactions form a training dataset. Over time, deterministic rules can be automatically proposed from patterns in blocked semantic failures.

### Multi-Agent Cross-Validation
A second independent agent re-interprets the original customer request and proposes an action. The gateway compares both outputs for consistency before allowing either.

### Adversarial Prompt Testing Suite
Automated generation of prompt injection variants against known policy rules. Measures auditor robustness and jailbreak resistance.

### Distributed Validation Service
Validation Engine refactored as a gRPC microservice. Multiple gateway instances share one validation cluster. Enables horizontal scaling and per-service policy namespacing.

### Real-Time Dashboard
`rich`-powered terminal dashboard showing live transaction stream, detection rates, cost per validation, and alert queue.
