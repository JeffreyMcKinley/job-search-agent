"""CLI entrypoint for job tracker agent.

Usage:
    python main.py auth              — Run OAuth flow for Google Sheets
    python main.py setup-sheet       — Create tabs with headers
    python main.py status            — Show row counts per tab
    python main.py list-rows <tab>   — List all rows in a tab (JSON)
    python main.py add-row <tab> <company> <title> <url> <folder> [notes]
    python main.py move-row <url> <from_tab> <to_tab> [--date-applied DATE] [--ats ATS]
    python main.py find-url <url>    — Find which tab contains a URL (JSON)
    python main.py all-urls          — List all tracked URLs (one per line)
    python main.py backfill          — Import ~/.proficiently/jobs/ into sheet
    python main.py scan-folders      — Scan folders and print state summary (JSON)
    python main.py fetch-job <url>   — Fetch job URL, extract company+title from HTML (JSON)
    python main.py prepare-move <url> — Move URL from "to apply" to "ready to apply" with auto-detected folder data
    python main.py gmail-companies   — List companies from "awaiting response" for Gmail scan (JSON)
    python main.py dedup <tab>       — Find and remove duplicate URLs in a tab (JSON report)
    python main.py gmail-auth        — Run OAuth flow for Gmail API (readonly)
    python main.py scan-gmail        — Scan Gmail for rejections/interviews and move rows
    python main.py scan-gmail --dry-run — Scan Gmail but don't move any rows
    python main.py lead-sources      — List configured job-alert senders (JSON)
    python main.py scan-leads [--since N] [--dry-run] [--sources a,b] [--rescan] [--max N]
                                     — Scan job-alert email for new leads, add to "to apply"
    python main.py wttj-script [--ids id1,id2]
                                     — Print the Chrome JS that resolves Welcome to the Jungle ids
    python main.py wttj-deck-script  — Print the Chrome JS that pulls the whole WTTJ recommendation deck
    python main.py resolve-wttj <result.json | -> [--dry-run]
                                     — Ingest that script's output and add the jobs
    python main.py linkedin-search [--keywords "a,b"] [--location TEXT] [--since 24h|7d] [--pages N] [--dry-run]
                                     — Search LinkedIn's guest endpoint, add new postings to "to apply"
    python main.py search-add [--date YYYY-MM-DD] [--urls url1,url2,...] — Add today's search results to sheet
    python main.py make-pdf <folder> [--kind resume|cover-letter] [--accent #RRGGBB]
                                     — Render folder HTML to PDF via headless Chrome
    python main.py prep [--limit N] [--urls u1,u2] [--model M] [--timeout S] [--no-apply] [--dry-run]
                                     — Prepare all "to apply" jobs end to end
    python main.py check-bespoke [<folder>...] — Verify materials are bespoke (exit 0/2)
    python main.py prep-schema       — Print the JSON Schema for a prep result file
    python main.py prep-validate <result.json> — Validate a prep result (exit 0 ok, 2 bad)
    python main.py prep-apply <result.json>    — Apply a prep result to the sheet (idempotent)
    python main.py run-log --stage NAME [--ok true|false] [--error TEXT] [--metrics '<json>']
                                     — Append one nightly-run stage record
    python main.py run-report [--days N] [--json]
                                     — Show the nightly funnel, and which stages left no record

Every command body lives in utils/, grouped by what it talks to. This file is
argument dispatch and nothing else.
"""

from __future__ import annotations

import sys

# Windows consoles default to cp1252, which cannot encode the emoji in the
# scan output — printing a result would raise UnicodeEncodeError mid-run (and
# again inside the except handler that tried to report it).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from utils import (
    cmd_add_row,
    cmd_all_urls,
    cmd_auth,
    cmd_backfill,
    cmd_check_bespoke,
    cmd_dedup,
    cmd_fetch_job,
    cmd_find_url,
    cmd_gmail_auth,
    cmd_gmail_companies,
    cmd_lead_sources,
    cmd_linkedin_search,
    cmd_list_rows,
    cmd_make_pdf,
    cmd_move_row,
    cmd_prep,
    cmd_prep_apply,
    cmd_prep_schema,
    cmd_prep_validate,
    cmd_prepare_move,
    cmd_resolve_wttj,
    cmd_run_log,
    cmd_run_report,
    cmd_scan_folders,
    cmd_scan_gmail,
    cmd_scan_leads,
    cmd_search_add,
    cmd_setup_sheet,
    cmd_status,
    cmd_wttj_deck_script,
    cmd_wttj_script,
)


def __main__() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "auth":
        cmd_auth()
    elif command == "setup-sheet":
        cmd_setup_sheet()
    elif command == "status":
        cmd_status()
    elif command == "list-rows":
        if len(sys.argv) < 3:
            print("Usage: list-rows <tab>")
            sys.exit(1)
        cmd_list_rows(sys.argv[2])
    elif command == "add-row":
        cmd_add_row(sys.argv[2:])
    elif command == "move-row":
        cmd_move_row(sys.argv[2:])
    elif command == "find-url":
        if len(sys.argv) < 3:
            print("Usage: find-url <url>")
            sys.exit(1)
        cmd_find_url(sys.argv[2])
    elif command == "all-urls":
        cmd_all_urls()
    elif command == "backfill":
        cmd_backfill()
    elif command == "scan-folders":
        cmd_scan_folders()
    elif command == "fetch-job":
        if len(sys.argv) < 3:
            print("Usage: fetch-job <url>")
            sys.exit(1)
        cmd_fetch_job(sys.argv[2])
    elif command == "prepare-move":
        if len(sys.argv) < 3:
            print("Usage: prepare-move <url>")
            sys.exit(1)
        cmd_prepare_move(sys.argv[2])
    elif command == "gmail-companies":
        cmd_gmail_companies()
    elif command == "dedup":
        if len(sys.argv) < 3:
            print("Usage: dedup <tab>")
            sys.exit(1)
        cmd_dedup(sys.argv[2])
    elif command == "linkedin-search":
        cmd_linkedin_search(sys.argv[2:])
    elif command == "search-add":
        cmd_search_add(sys.argv[2:])
    elif command == "make-pdf":
        cmd_make_pdf(sys.argv[2:])
    elif command == "prep":
        cmd_prep(sys.argv[2:])
    elif command == "check-bespoke":
        cmd_check_bespoke(sys.argv[2:])
    elif command == "prep-schema":
        cmd_prep_schema()
    elif command == "prep-validate":
        if len(sys.argv) < 3:
            print("Usage: prep-validate <result.json>")
            sys.exit(1)
        cmd_prep_validate(sys.argv[2])
    elif command == "prep-apply":
        if len(sys.argv) < 3:
            print("Usage: prep-apply <result.json>")
            sys.exit(1)
        cmd_prep_apply(sys.argv[2])
    elif command == "gmail-auth":
        cmd_gmail_auth()
    elif command == "scan-gmail":
        dry_run = "--dry-run" in sys.argv
        cmd_scan_gmail(dry_run=dry_run)
    elif command == "run-log":
        cmd_run_log(sys.argv[2:])
    elif command == "run-report":
        cmd_run_report(sys.argv[2:])
    elif command == "lead-sources":
        cmd_lead_sources()
    elif command == "scan-leads":
        cmd_scan_leads(sys.argv[2:])
    elif command == "wttj-deck-script":
        cmd_wttj_deck_script()
    elif command == "wttj-script":
        cmd_wttj_script(sys.argv[2:])
    elif command == "resolve-wttj":
        cmd_resolve_wttj(sys.argv[2:])
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    __main__()
