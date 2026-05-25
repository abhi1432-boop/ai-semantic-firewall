"""
scripts/review_escalations.py — Human-in-the-Loop Escalation Reviewer

Connects to a running Semantic Firewall Gateway and lets a human reviewer
approve or reject escalated transactions one by one.

Usage:
    python scripts/review_escalations.py                    # interactive review
    python scripts/review_escalations.py --list             # list pending only
    python scripts/review_escalations.py --list --all       # list all (any status)
    python scripts/review_escalations.py --gateway http://localhost:8000

Requires:
    - Gateway running with ENABLE_ESCALATION=true
    - pip install rich httpx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent))

console = Console()


# ─────────────────────────────────────────────────────────────────────────────
# Display Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _status_style(status: str) -> str:
    return {"pending": "yellow", "approved": "green", "rejected": "red"}.get(status, "white")


def _print_escalation_detail(item: dict) -> None:
    confidence = item.get("layer_b_confidence", 0.0)
    conf_color = "red" if confidence < 0.5 else "yellow" if confidence < 0.75 else "green"

    lines = [
        f"[bold]Request ID:[/bold]       {item['request_id']}",
        f"[bold]Timestamp:[/bold]        {item['timestamp']}",
        f"[bold]Action Type:[/bold]      {item['action_type']}",
        f"[bold]Customer Request:[/bold] {item['customer_request']}",
        "",
        f"[bold]Auditor Confidence:[/bold] [{conf_color}]{confidence:.2f}[/{conf_color}]",
        f"[bold]Auditor Reason:[/bold]   {item.get('layer_b_reason', 'N/A')}",
        "",
        "[bold]Rejection Reasons:[/bold]",
        *[f"  • {r}" for r in item.get("rejection_reasons", [])],
        "",
        "[bold]Generated Payload:[/bold]",
        json.dumps(item.get("generated_payload", {}), indent=2),
    ]

    status = item.get("status", "pending")
    panel_style = _status_style(status)

    if status != "pending":
        lines += [
            "",
            f"[bold]Review Decision:[/bold] [{panel_style}]{status.upper()}[/{panel_style}]",
            f"[bold]Reviewer Notes:[/bold]  {item.get('reviewer_notes', '')}",
            f"[bold]Reviewed At:[/bold]     {item.get('reviewed_at', '')}",
        ]

    console.print(Panel(
        "\n".join(lines),
        title=f"[cyan]Escalation — [{panel_style}]{status.upper()}[/{panel_style}][/cyan]",
        border_style=panel_style,
        padding=(1, 2),
    ))


def _print_escalation_table(items: list[dict]) -> None:
    table = Table(
        title="Escalation Queue",
        header_style="bold magenta",
        show_lines=True,
    )
    table.add_column("#", width=3, justify="right")
    table.add_column("Request ID", width=36)
    table.add_column("Action", width=22)
    table.add_column("Customer Request", width=38)
    table.add_column("Confidence", width=11, justify="right")
    table.add_column("Status", width=10, justify="center")

    for i, item in enumerate(items, 1):
        status = item.get("status", "pending")
        conf = item.get("layer_b_confidence", 0.0)
        conf_color = "red" if conf < 0.5 else "yellow" if conf < 0.75 else "green"
        status_style = _status_style(status)

        table.add_row(
            str(i),
            item["request_id"],
            item["action_type"],
            item["customer_request"][:38],
            f"[{conf_color}]{conf:.2f}[/{conf_color}]",
            f"[{status_style}]{status.upper()}[/{status_style}]",
        )

    console.print(table)


# ─────────────────────────────────────────────────────────────────────────────
# Core Logic
# ─────────────────────────────────────────────────────────────────────────────


async def fetch_escalations(client: httpx.AsyncClient, all_statuses: bool) -> list[dict]:
    status_param = "all" if all_statuses else "pending"
    resp = await client.get(f"/escalations?status={status_param}")
    if resp.status_code != 200:
        console.print(f"[red]Error fetching escalations: {resp.status_code} {resp.text}[/red]")
        return []
    return resp.json()


async def submit_review(
    client: httpx.AsyncClient, request_id: str, decision: str, notes: str
) -> dict | None:
    resp = await client.post(
        f"/escalations/{request_id}/review",
        json={"decision": decision, "notes": notes},
    )
    if resp.status_code == 200:
        return resp.json()
    console.print(f"[red]Error submitting review: {resp.status_code} {resp.text}[/red]")
    return None


async def interactive_review(client: httpx.AsyncClient) -> None:
    items = await fetch_escalations(client, all_statuses=False)

    if not items:
        console.print("[green]No pending escalations.[/green]")
        return

    console.rule(f"[bold cyan]Escalation Queue — {len(items)} pending[/bold cyan]")
    _print_escalation_table(items)
    console.print()

    for i, item in enumerate(items, 1):
        console.rule(f"[bold yellow]Item {i} of {len(items)}[/bold yellow]")
        _print_escalation_detail(item)

        decision = Prompt.ask(
            "\n[bold]Decision[/bold]",
            choices=["approve", "reject", "skip", "quit"],
            default="skip",
        )

        if decision == "quit":
            console.print("[dim]Exiting reviewer.[/dim]")
            break
        if decision == "skip":
            console.print("[dim]Skipped.[/dim]\n")
            continue

        notes = Prompt.ask("[bold]Reviewer notes[/bold] (optional)", default="")
        result = await submit_review(client, item["request_id"], decision, notes)

        if result:
            status_style = _status_style(result["status"])
            console.print(
                f"\n[{status_style}]✓ Decision recorded: "
                f"{result['status'].upper()}[/{status_style}]\n"
            )

    console.rule("[dim]Review session complete[/dim]")


async def list_only(client: httpx.AsyncClient, all_statuses: bool) -> None:
    items = await fetch_escalations(client, all_statuses)
    if not items:
        console.print("[green]No escalations found.[/green]")
        return
    status_label = "all" if all_statuses else "pending"
    console.rule(f"[bold cyan]Escalations ({status_label}) — {len(items)} items[/bold cyan]")
    _print_escalation_table(items)


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(description="AI Semantic Firewall — Escalation Reviewer")
    parser.add_argument(
        "--gateway",
        default="http://localhost:8000",
        metavar="URL",
        help="Gateway base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List escalations without entering interactive review",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include already-reviewed escalations (approved + rejected)",
    )
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.gateway, timeout=15.0) as client:
        # Verify gateway is reachable
        try:
            health = await client.get("/health")
            health.raise_for_status()
        except Exception as e:
            console.print(f"[red]Cannot reach gateway at {args.gateway}: {e}[/red]")
            sys.exit(1)

        if args.list:
            await list_only(client, all_statuses=args.all)
        else:
            await interactive_review(client)


if __name__ == "__main__":
    asyncio.run(main())
