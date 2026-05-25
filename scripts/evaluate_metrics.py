"""
scripts/evaluate_metrics.py — Audit Log Report

Reads reports/audit_log.jsonl and prints a detailed accuracy, cost,
and latency report. Run this after run_demo.py has populated the log.

Usage:
    python scripts/evaluate_metrics.py
    python scripts/evaluate_metrics.py --log path/to/other.jsonl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tracker import load_events, print_metrics_report, print_recent_events


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Semantic Firewall — Metrics Report")
    parser.add_argument("--log", default=None, help="Path to audit log (default: reports/audit_log.jsonl)")
    parser.add_argument("--events", type=int, default=0, help="Also print last N events (0 = skip)")
    args = parser.parse_args()

    events = load_events(args.log)

    if not events:
        print("No events found in audit log. Run scripts/run_demo.py first.")
        sys.exit(1)

    print_metrics_report(events)

    if args.events > 0:
        print_recent_events(args.events, args.log)


if __name__ == "__main__":
    main()
