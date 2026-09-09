"""Commands that only read or write the Google Sheet.

Nothing here touches ~/.proficiently/jobs/ or the agent; these are the thin
CLI wrappers around sheets.py.
"""

from __future__ import annotations

import json
import sys

from .dates import today


def cmd_auth() -> None:
    """Run the gspread OAuth flow."""
    from config import AUTHORIZED_USER_FILE
    from sheets import authenticate

    authenticate()
    print("Authentication successful.")
    print(f"Tokens saved to: {AUTHORIZED_USER_FILE}")


def cmd_setup_sheet() -> None:
    """Create all tabs with headers."""
    from sheets import connect, setup_sheet

    print("Connecting to Google Sheets...")
    spreadsheet = connect()
    print(f"Connected: {spreadsheet.title}")
    setup_sheet(spreadsheet)
    print("Sheet setup complete.")


def cmd_status() -> None:
    """Show row counts per tab."""
    from sheets import connect, get_status_counts

    spreadsheet = connect()
    counts = get_status_counts(spreadsheet)
    total = sum(counts.values())
    print(f"\nJob Tracker Status ({total} total)")
    print("-" * 35)
    for tab, count in counts.items():
        print(f"  {tab:<25} {count:>4}")


def cmd_list_rows(tab: str) -> None:
    """List all rows in a tab as JSON."""
    from sheets import connect, read_tab

    spreadsheet = connect()
    rows = read_tab(spreadsheet, tab)
    output = [
        {
            "row": r.row_number,
            "company": r.record.company,
            "title": r.record.title,
            "url": r.record.posting_url,
            "folder": r.record.folder_slug,
            "date_added": r.record.date_added,
        }
        for r in rows
    ]
    print(json.dumps(output, indent=2))


def cmd_add_row(args: list[str]) -> None:
    """Add a row to a tab.

    Args: <tab> <company> <title> <url> <folder> [notes]
    """
    if len(args) < 5:
        print("Usage: add-row <tab> <company> <title> <url> <folder> [notes]")
        sys.exit(1)

    tab, company, title, url, folder = args[:5]
    notes = args[5] if len(args) > 5 else ""

    from models import JobRecord
    from sheets import add_row, connect, find_by_url

    spreadsheet = connect()

    # Check for duplicates
    existing = find_by_url(spreadsheet, url)
    if existing:
        print(f"DUPLICATE: URL already in '{existing.tab}' (row {existing.row_number})")
        sys.exit(1)

    record = JobRecord(
        company=company,
        title=title,
        posting_url=url,
        date_added=today(),
        folder_slug=folder,
        notes=notes,
    )

    row_num = add_row(spreadsheet, tab, record)
    print(f"Added '{company} - {title}' to '{tab}' (row {row_num})")


def cmd_move_row(args: list[str]) -> None:
    """Move a row between tabs.

    Args: <url> <from_tab> <to_tab> [--date-applied DATE] [--ats ATS] [--date-resolved DATE] [--source SOURCE]
    """
    if len(args) < 3:
        print("Usage: move-row <url> <from_tab> <to_tab> [flags]")
        print("Flags: --date-applied DATE --ats ATS --date-resolved DATE")
        print("       --source SOURCE --resume PATH --cover-letter PATH --date-prepared DATE")
        print("       --company COMPANY --title TITLE --folder SLUG --notes NOTES")
        sys.exit(1)

    url, from_tab, to_tab = args[:3]

    # Parse optional flags
    flag_map = {
        "--date-applied": "date_applied",
        "--ats": "ats_type",
        "--date-resolved": "date_resolved",
        "--source": "source_details",
        "--resume": "resume_path",
        "--cover-letter": "cover_letter_path",
        "--date-prepared": "date_prepared",
        "--company": "company",
        "--title": "title",
        "--folder": "folder_slug",
        "--notes": "notes",
    }
    extra: dict[str, str] = {}
    i = 3
    while i < len(args):
        if args[i] in flag_map and i + 1 < len(args):
            extra[flag_map[args[i]]] = args[i + 1]
            i += 2
        else:
            i += 1

    from sheets import connect, find_by_url, move_row

    spreadsheet = connect()

    # Find the existing row to carry its data forward
    existing = find_by_url(spreadsheet, url)
    if not existing:
        print(f"URL not found in any tab: {url}")
        sys.exit(1)

    if existing.tab != from_tab:
        print(f"URL found in '{existing.tab}', not '{from_tab}'")
        sys.exit(1)

    # Update the record with new data
    updated = existing.record.model_copy(update=extra)

    success = move_row(spreadsheet, url, from_tab, to_tab, updated)
    if not success:
        sys.exit(1)


def cmd_find_url(url: str) -> None:
    """Find which tab contains a URL."""
    from sheets import connect, find_by_url

    spreadsheet = connect()
    result = find_by_url(spreadsheet, url)
    if result:
        print(json.dumps({
            "found": True,
            "tab": result.tab,
            "row": result.row_number,
            "company": result.record.company,
            "title": result.record.title,
        }))
    else:
        print(json.dumps({"found": False}))


def cmd_all_urls() -> None:
    """List all tracked URLs."""
    from sheets import connect, get_all_urls

    spreadsheet = connect()
    urls = get_all_urls(spreadsheet)
    for url in sorted(urls):
        print(url)


def cmd_dedup(tab: str) -> None:
    """Find and remove duplicate URLs within a tab, and rows already in another tab."""
    from sheets import connect, dedup_tab

    report = dedup_tab(connect(), tab)
    print(json.dumps(report, indent=2))
    if report["rows_removed"]:
        print(f"\nRemoved {report['rows_removed']} duplicate rows from '{tab}'.")
    else:
        print(f"\nNo duplicates found in '{tab}'.")
