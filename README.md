# AI Semantic Firewall

A middleware enforcement layer that sits between LLM agents and production APIs, blocking transactions that are schema-valid but semantically wrong — hallucinated IDs, inflated amounts, intent misalignment, prompt injection, and policy violations that traditional validation cannot catch.

---

## The Problem

LLM agents can generate API payloads that pass JSON schema validation but are still wrong:

- Refund $500 when the customer asked for $50
- Reference an invoice that belongs to a different customer
- Cancel a subscription immediately when the customer said "at end of period"
- Inject policy-override instructions into a reason field
- Hallucinate an invoice ID that doesn't exist

None of these are caught by schema validators. This project builds a firewall that catches all of them.

---

## Architecture

```
Customer Request
      │
      ▼
 Chaos Agent  (LLM-powered, intentionally unreliable)
      │
      │  NEVER calls the API directly
      ▼
┌─────────────────────────────────────────┐
│         Semantic Firewall Gateway       │
│                                         │
│  Layer A — Deterministic Rules          │
│  • Pydantic schema validation           │
│  • Business rules (amounts, ownership)  │
│  • State machine checks                 │
│  • Policy enforcement                   │
│            │                            │
│            ▼                            │
│  Layer B — Semantic Auditor             │
│  • Independent Claude judge             │
│  • Intent alignment check              │
│  • Hallucination detection              │
│  • Prompt injection detection           │
│  • Confidence scoring                   │
│                                         │
│  Decision: ALLOW / REJECT / CORRECT /  │
│            ESCALATE                     │
└───────────────┬─────────────────────────┘
                │ (only ALLOW reaches here)
                ▼
        Mock Enterprise API
        /refunds  /credits  /subscriptions
```

The auditor uses a **separate model with a separate system prompt** — it has no access to the agent's internal state, preventing correlated failure.

---

## Failure Modes Caught

| ID | Scenario | Caught By |
|----|----------|-----------|
| F01 | Inflated refund amount | Layer A |
| F02 | Hallucinated invoice ID | Layer A |
| F03 | Wrong customer account | Layer A + Layer B |
| F04 | Double refund (replay) | Layer A |
| F05 | Credit to suspended account | Layer A |
| F06 | Immediate vs delayed cancellation | Layer B only |
| F07 | Prompt injection in payload fields | Layer B only |
| F08 | Contradictory payload fields | Layer A + Layer B |
| F09 | Invalid state transition | Layer A |
| F10 | Schema-valid policy violation | Layer B only |

---

## Project Structure

```
ai-semantic-firewall/
├── schemas.py            # Shared Pydantic models
├── mock_api.py           # Fake enterprise backend (FastAPI)
├── gateway.py            # Firewall enforcement gateway
├── validation_engine.py  # Layer A rules + Layer B auditor
├── chaos_agent.py        # Unreliable LLM agent
├── tracker.py            # Audit logging + metrics
├── config.py             # Settings (env vars)
├── prompts.py            # Auditor system prompts
│
├── scripts/
│   ├── run_demo.py           # Live eval: all 10 scenarios + report
│   ├── evaluate_metrics.py   # Print metrics from audit log
│   ├── seed_database.py      # Reset mock API data
│   └── review_escalations.py # Human-in-the-loop review CLI
│
└── tests/
    ├── test_scenarios.py   # 18 end-to-end failure scenario tests
    └── test_adversarial.py # 14 prompt injection + boundary tests
```

---

## Quickstart

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Set up environment**
```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

**3. Run the test suite (no API key needed)**
```bash
pytest tests/ -v
# 32 passed
```

**4. Run the live demo (API key required)**
```bash
python3 scripts/run_demo.py
```
Starts both servers in-process, runs all 10 failure scenarios against the real Claude auditor, and prints a detection rate report.

**5. Try the self-correction loop**
```bash
python3 chaos_agent.py --live-correct "Refund $50 from invoice inv_001" --max-retries 3
```
The agent generates a payload, submits it, and if rejected uses the firewall's rejection reasons to fix and retry automatically.

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required for Layer B auditor and live agent |
| `AUDITOR_MODEL` | `claude-haiku-4-5-20251001` | Model for the semantic auditor |
| `AGENT_MODEL` | `claude-haiku-4-5-20251001` | Model for the chaos agent |
| `SEMANTIC_CONFIDENCE_THRESHOLD` | `0.75` | Below this → reject |
| `AUTO_CORRECT_THRESHOLD` | `0.85` | Above this → attempt auto-correction |
| `ENABLE_ESCALATION` | `false` | Route uncertain decisions to human review queue |

---

## Phase 5 Features

**Self-correction loop** — when the gateway rejects an action, the agent receives the rejection reasons and asks the LLM to fix its payload, then retries up to N times.

**Escalation queue** — transactions the auditor flags as uncertain can be routed to a human reviewer instead of auto-rejected. Run the review CLI:
```bash
# Start gateway with ENABLE_ESCALATION=true, then:
python3 scripts/review_escalations.py
```

**Adversarial test suite** — 14 tests covering prompt injection variants (in `customer_request`, `reason`, `agent_reasoning` fields), unicode obfuscation, boundary values at the $100 policy limit, cross-customer targeting, and replay attacks.

---

## Results

Running `python3 scripts/run_demo.py` against the live Claude auditor:

- **Detection rate**: 10/10 failure scenarios caught
- **False positive rate**: ~1/11 (valid request occasionally over-rejected by auditor — tunable via `SEMANTIC_CONFIDENCE_THRESHOLD`)
- **Cost**: ~$0.0002 per request (claude-haiku-4-5)
- **Latency**: ~300–800 ms end-to-end including auditor call
