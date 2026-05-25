"""
scripts/seed_database.py — Reset and re-seed the Mock API database.

Useful when you want a clean slate between manual demo runs without
restarting the server.

Usage:
    python scripts/seed_database.py
    python scripts/seed_database.py --url http://localhost:8001
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset Mock API database to seed state")
    parser.add_argument("--url", default=settings.mock_api_base_url, help="Mock API base URL")
    args = parser.parse_args()

    try:
        resp = httpx.post(f"{args.url}/db/reset", timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        print(f"Database reset: {data['customers']} customers, {data['invoices']} invoices")
    except httpx.ConnectError:
        print(f"Could not connect to Mock API at {args.url}")
        print("Start it with: uvicorn mock_api:app --port 8001")
        sys.exit(1)


if __name__ == "__main__":
    main()
