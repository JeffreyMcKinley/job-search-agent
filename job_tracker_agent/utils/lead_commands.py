"""CLI wrappers for inbound job-lead scanning.

The pipeline itself lives in lead_scanner.py; this file is argument parsing and
JSON output. Welcome to the Jungle needs two commands rather than one because
its GraphQL API authenticates with a browser session cookie: `scan-leads` parks
the pending externalIds, `wttj-script` prints the JavaScript to run in Chrome,
and `resolve-wttj` ingests what that returns.
"""

from __future__ import annotations

import json
import sys


def _flag_value(args: list[str], flag: str, default: str = "") -> str:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def cmd_scan_leads(args: list[str]) -> None:
    """Scan job-alert email for new leads.

    Args: [--since N] [--dry-run] [--sources a,b] [--rescan] [--max N]
    """
    from lead_scanner import scan_leads

    try:
        # "30" and "30d" both read naturally here, and Gmail's own operator is
        # newer_than:30d, so accept the suffix rather than rejecting it.
        days = int(_flag_value(args, "--since", "7").rstrip("dD"))
        max_messages = int(_flag_value(args, "--max", "200"))
    except ValueError:
        print("--since takes a number of days (e.g. 7 or 7d); --max takes an integer")
        sys.exit(1)

    raw_sources = _flag_value(args, "--sources")
    source_names = [s.strip() for s in raw_sources.split(",") if s.strip()] or None

    try:
        report = scan_leads(
            days,
            dry_run="--dry-run" in args,
            source_names=source_names,
            rescan="--rescan" in args,
            max_messages=max_messages,
        )
    except ValueError as e:
        print(json.dumps({"error": str(e)}, indent=2))
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        # Most often an expired Gmail refresh token. The nightly run is meant to
        # carry on to the search leg after this, which is exactly why the
        # failure needs its own durable record -- in the prose log it is one
        # line among fifty and reads like a thin night rather than a broken one.
        import run_log

        run_log.record("scan-leads", ok=False, error=f"{type(e).__name__}: {e}")
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, indent=2))
        sys.exit(1)

    print(json.dumps(report, indent=2))


def cmd_lead_sources() -> None:
    """List configured lead sources and whether they are enabled."""
    from lead_sources import SOURCES

    print(json.dumps([
        {
            "name": s.name,
            "enabled": s.enabled,
            "strategy": s.strategy,
            "quality": s.quality,
            "gmail_from": s.gmail_from,
        }
        for s in SOURCES
    ], indent=2))


def cmd_wttj_script(args: list[str]) -> None:
    """Print the JavaScript that resolves pending WTTJ ids in Chrome.

    Args: [--ids id1,id2]  (defaults to whatever scan-leads parked)
    """
    from lead_scanner import load_pending_wttj, wttj_chrome_script

    raw_ids = _flag_value(args, "--ids")
    if raw_ids:
        ids = [i.strip() for i in raw_ids.split(",") if i.strip()]
    else:
        ids = [p["external_id"] for p in load_pending_wttj()]

    if not ids:
        print("# No pending Welcome to the Jungle ids. Run scan-leads first.")
        return

    print(wttj_chrome_script(ids))


def cmd_wttj_deck_script() -> None:
    """Print the JavaScript that pulls the whole WTTJ recommendation deck."""
    from lead_scanner import wttj_deck_script

    print(wttj_deck_script())


def cmd_resolve_wttj(args: list[str]) -> None:
    """Ingest the Chrome phase's GraphQL output and add the jobs to "to apply".

    Args: <result.json | -> [--dry-run]
    """
    from lead_scanner import resolve_wttj

    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print("Usage: resolve-wttj <result.json | -> [--dry-run]")
        sys.exit(1)

    source = positional[0]
    try:
        raw = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
    except OSError as e:
        print(json.dumps({"error": str(e)}, indent=2))
        sys.exit(1)

    try:
        payload = json.loads(raw)
        # javascript_tool returns the script's value as a JSON *string*; accept
        # both that and an already-decoded list so neither form needs unwrapping
        # by hand.
        if isinstance(payload, str):
            payload = json.loads(payload)
    except ValueError as e:
        print(json.dumps({"error": f"not valid JSON: {e}"}, indent=2))
        sys.exit(1)

    report = resolve_wttj(payload, dry_run="--dry-run" in args)
    print(json.dumps(report, indent=2))
    if report.get("error"):
        sys.exit(1)
