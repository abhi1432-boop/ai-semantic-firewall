"""
tracker.py — Metrics & Audit Tracker

Every gateway transaction is recorded here as a structured JSONL entry.
Provides aggregate metrics and a rich terminal summary report.

The tracker is the ONLY writer to the audit log. All other modules call
record_event() and never touch the log file directly.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
import structlog
from rich.console import Console
from rich.table import Table

from config import settings
from schemas import GatewayEvent, MetricsSummary, OutcomeCategory

log = structlog.get_logger("tracker")
console = Console()

# ─────────────────────────────────────────────────────────────────────────────
# Write lock — prevents interleaved writes under concurrent async requests
# ─────────────────────────────────────────────────────────────────────────────

_write_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Core: record a single transaction
# ─────────────────────────────────────────────────────────────────────────────


async def record_event(event: GatewayEvent) -> None:
    """Append a GatewayEvent to the JSONL audit log (thread-safe)."""
    log_path = Path(settings.audit_log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = orjson.dumps(event.model_dump(mode="json"), option=orjson.OPT_APPEND_NEWLINE)

    async with _write_lock:
        with open(log_path, "ab") as f:
            f.write(entry)

    log.info(
        "tracker.recorded",
        request_id=event.request_id,
        decision=event.gateway_decision,
        outcome=event.outcome_category,
        latency_ms=event.total_latency_ms,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Read & aggregate
# ─────────────────────────────────────────────────────────────────────────────


def load_events(log_path: str | None = None) -> list[GatewayEvent]:
    """Load all events from the audit log. Returns empty list if log missing."""
    path = Path(log_path or settings.audit_log_path)
    if not path.exists():
        return []

    events: list[GatewayEvent] = []
    with open(path, "rb") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(GatewayEvent.model_validate(orjson.loads(line)))
                except Exception as e:
                    log.warning("tracker.parse_error", error=str(e))
    return events


def compute_metrics(events: list[GatewayEvent]) -> MetricsSummary:
    """Derive aggregate statistics from a list of GatewayEvents."""
    if not events:
        return MetricsSummary()

    counts: dict[str, int] = defaultdict(int)
    for e in events:
        counts[e.outcome_category.value] += 1

    total = len(events)
    success = counts[OutcomeCategory.SUCCESS.value]
    blocked_rules = counts[OutcomeCategory.BLOCKED_RULES.value]
    blocked_semantic = counts[OutcomeCategory.BLOCKED_SEMANTIC.value]
    auto_corrected = counts[OutcomeCategory.AUTO_CORRECTED.value]
    escalated = counts[OutcomeCategory.ESCALATED.value]
    silent_failures = counts[OutcomeCategory.SILENT_FAILURE.value]

    total_blocked = blocked_rules + blocked_semantic + escalated
    total_unsafe = total_blocked + silent_failures  # requests that should have been caught

    # Detection rate: fraction of unsafe requests that were actually caught
    detection_rate = total_blocked / total_unsafe if total_unsafe > 0 else 1.0

    # False positive rate: valid requests incorrectly blocked
    # We treat AUTO_CORRECTED as partial false positives (caught but recovered)
    total_safe = success + auto_corrected
    false_positive_rate = 0.0  # Cannot compute without ground truth labels

    # Correction success: of correction attempts, how many succeeded (became SUCCESS)
    total_corrections = auto_corrected
    correction_attempts = sum(e.correction_attempts for e in events if e.correction_attempts > 0)
    correction_success_rate = (
        total_corrections / correction_attempts if correction_attempts > 0 else 0.0
    )

    # Latency
    latencies = [e.total_latency_ms for e in events if e.total_latency_ms > 0]
    mean_latency = sum(latencies) / len(latencies) if latencies else 0.0

    # Auditor token usage
    token_counts = [
        e.layer_b_result.tokens_used
        for e in events
        if e.layer_b_result and e.layer_b_result.tokens_used > 0
    ]
    mean_tokens = sum(token_counts) / len(token_counts) if token_counts else 0.0

    # Rough cost estimate: gpt-4o-mini at ~$0.15/1M input tokens
    cost_per_request = (mean_tokens / 1_000_000) * 0.15 if mean_tokens else 0.0

    # Rule vs semantic catch distribution
    total_catches = blocked_rules + blocked_semantic
    rule_fraction = blocked_rules / total_catches if total_catches > 0 else 0.0
    semantic_fraction = blocked_semantic / total_catches if total_catches > 0 else 0.0

    return MetricsSummary(
        total_transactions=total,
        success_count=success,
        blocked_by_rules=blocked_rules,
        blocked_by_semantic=blocked_semantic,
        auto_corrected=auto_corrected,
        escalated=escalated,
        silent_failures=silent_failures,
        detection_rate=round(detection_rate, 4),
        false_positive_rate=round(false_positive_rate, 4),
        correction_success_rate=round(correction_success_rate, 4),
        mean_latency_ms=round(mean_latency, 2),
        mean_auditor_tokens=round(mean_tokens, 1),
        estimated_cost_per_request_usd=round(cost_per_request, 6),
        rule_catch_fraction=round(rule_fraction, 4),
        semantic_catch_fraction=round(semantic_fraction, 4),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rich Terminal Report
# ─────────────────────────────────────────────────────────────────────────────


def print_metrics_report(events: list[GatewayEvent] | None = None) -> None:
    """Print a rich-formatted metrics summary to the terminal."""
    if events is None:
        events = load_events()

    metrics = compute_metrics(events)

    console.rule("[bold cyan]AI Semantic Firewall — Metrics Report[/bold cyan]")
    console.print(f"[dim]Generated: {datetime.utcnow().isoformat()}[/dim]\n")

    # ── Transaction Breakdown ─────────────────────────────────────────────────
    breakdown = Table(title="Transaction Breakdown", show_header=True, header_style="bold magenta")
    breakdown.add_column("Outcome", style="cyan", no_wrap=True)
    breakdown.add_column("Count", justify="right")
    breakdown.add_column("% of Total", justify="right")

    def pct(n: int) -> str:
        if metrics.total_transactions == 0:
            return "0.0%"
        return f"{100 * n / metrics.total_transactions:.1f}%"

    breakdown.add_row("[green]Success[/green]", str(metrics.success_count), pct(metrics.success_count))
    breakdown.add_row("[yellow]Auto-Corrected[/yellow]", str(metrics.auto_corrected), pct(metrics.auto_corrected))
    breakdown.add_row("[red]Blocked — Rules[/red]", str(metrics.blocked_by_rules), pct(metrics.blocked_by_rules))
    breakdown.add_row("[red]Blocked — Semantic[/red]", str(metrics.blocked_by_semantic), pct(metrics.blocked_by_semantic))
    breakdown.add_row("[orange1]Escalated[/orange1]", str(metrics.escalated), pct(metrics.escalated))
    breakdown.add_row("[bold red]Silent Failures[/bold red]", str(metrics.silent_failures), pct(metrics.silent_failures))
    breakdown.add_row("[bold]TOTAL[/bold]", str(metrics.total_transactions), "100%")

    console.print(breakdown)
    console.print()

    # ── Security Metrics ──────────────────────────────────────────────────────
    security = Table(title="Security Metrics", show_header=True, header_style="bold magenta")
    security.add_column("Metric", style="cyan")
    security.add_column("Value", justify="right")

    def pct_str(v: float) -> str:
        return f"{v * 100:.1f}%"

    security.add_row("Detection Rate", f"[green]{pct_str(metrics.detection_rate)}[/green]")
    security.add_row("Silent Failure Rate", f"[red]{pct_str(1 - metrics.detection_rate) if metrics.silent_failures else '0.0%'}[/red]")
    security.add_row("Rule Catch Fraction", pct_str(metrics.rule_catch_fraction))
    security.add_row("Semantic Catch Fraction", pct_str(metrics.semantic_catch_fraction))
    security.add_row("Correction Success Rate", pct_str(metrics.correction_success_rate))

    console.print(security)
    console.print()

    # ── Performance & Cost ────────────────────────────────────────────────────
    perf = Table(title="Performance & Cost", show_header=True, header_style="bold magenta")
    perf.add_column("Metric", style="cyan")
    perf.add_column("Value", justify="right")

    perf.add_row("Mean Latency", f"{metrics.mean_latency_ms:.1f} ms")
    perf.add_row("Mean Auditor Tokens", f"{metrics.mean_auditor_tokens:.0f}")
    perf.add_row("Est. Cost / Request", f"${metrics.estimated_cost_per_request_usd:.6f}")
    perf.add_row(
        "Est. Cost / 1,000 Requests",
        f"${metrics.estimated_cost_per_request_usd * 1000:.4f}",
    )

    console.print(perf)
    console.rule()


def print_recent_events(n: int = 10, log_path: str | None = None) -> None:
    """Print the last N events as a formatted table."""
    events = load_events(log_path)
    recent = events[-n:]

    table = Table(title=f"Last {len(recent)} Transactions", header_style="bold magenta")
    table.add_column("Request ID", style="dim", width=12)
    table.add_column("Action")
    table.add_column("Decision")
    table.add_column("Outcome")
    table.add_column("Latency", justify="right")
    table.add_column("Confidence", justify="right")

    outcome_colors = {
        OutcomeCategory.SUCCESS.value: "green",
        OutcomeCategory.AUTO_CORRECTED.value: "yellow",
        OutcomeCategory.BLOCKED_RULES.value: "red",
        OutcomeCategory.BLOCKED_SEMANTIC.value: "red",
        OutcomeCategory.ESCALATED.value: "orange1",
        OutcomeCategory.SILENT_FAILURE.value: "bold red",
    }

    for e in recent:
        color = outcome_colors.get(e.outcome_category.value, "white")
        confidence = (
            f"{e.layer_b_result.confidence:.2f}"
            if e.layer_b_result
            else "N/A (rules)"
        )
        table.add_row(
            e.request_id[:8] + "…",
            e.action_type.value,
            e.gateway_decision.value,
            f"[{color}]{e.outcome_category.value}[/{color}]",
            f"{e.total_latency_ms:.0f} ms",
            confidence,
        )

    console.print(table)
