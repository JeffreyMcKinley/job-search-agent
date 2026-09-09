"""Gmail-side commands: auth, the company list to scan for, and the scan itself."""

from __future__ import annotations

import json
import sys


def cmd_gmail_companies() -> None:
    """List companies from 'awaiting response' grouped for Gmail scanning.

    Output JSON: list of {company, search_name, urls: [...], domain}
    where search_name is cleaned for Gmail subject search.
    """
    from sheets import connect, group_rows_by_company, read_tab

    spreadsheet = connect()
    rows = read_tab(spreadsheet, "awaiting response")

    result = [
        {
            "company": g["company"],
            "search_name": g["company"],
            "domain": g["domain"],
            "urls": [r.record.posting_url for r in g["rows"]],
        }
        for g in sorted(
            group_rows_by_company(rows).values(), key=lambda g: g["company"]
        )
    ]
    print(json.dumps(result, indent=2))


def cmd_gmail_auth() -> None:
    """Run Gmail OAuth flow."""
    from gmail_scanner import gmail_auth

    print("Running Gmail OAuth flow...")
    print("This will open a browser window for Google sign-in.")
    print("Grant 'Gmail (readonly)' access when prompted.\n")
    gmail_auth()
    print("Gmail authentication successful.")


def cmd_scan_gmail(dry_run: bool = False) -> None:
    """Scan Gmail for rejections/interviews."""
    from config import GMAIL_AUTHORIZED_USER_FILE

    if not GMAIL_AUTHORIZED_USER_FILE.exists():
        print("ERROR: Gmail not authenticated.")
        print("Run: python main.py gmail-auth")
        sys.exit(1)

    from gmail_scanner import run_full_scan

    run_full_scan(dry_run=dry_run)
