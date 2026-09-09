"""CLI wrappers over run_log: write one stage record, or read the funnel back.

`run-log` exists because half the nightly funnel is only ever known to the
model. scan-leads and search-add can record themselves from Python, but how many
hiring.cafe queries ran, how many postings they returned, and why the rest were
dropped are facts that live in the agent's head and nowhere else. The skill
hands them over here at the end of the search step.
"""

from __future__ import annotations

import json
import sys


def cmd_run_log(args: list[str]) -> None:
    """Append one stage record.

    Args: --stage NAME [--ok true|false] [--error TEXT] [--metrics '<json>']
    """
    import run_log

    def flag(name: str, default: str = "") -> str:
        if name in args:
            i = args.index(name)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    stage = flag("--stage")
    if not stage:
        print("Usage: run-log --stage NAME [--ok true|false] [--error TEXT] [--metrics '<json>']")
        sys.exit(1)

    raw = flag("--metrics", "{}")
    try:
        metrics = json.loads(raw)
    except ValueError as e:
        print(json.dumps({"error": f"--metrics is not valid JSON: {e}"}, indent=2))
        sys.exit(1)
    if not isinstance(metrics, dict):
        print(json.dumps({"error": "--metrics must be a JSON object"}, indent=2))
        sys.exit(1)

    entry = run_log.record(
        stage,
        ok=flag("--ok", "true").lower() not in ("false", "0", "no"),
        metrics=metrics,
        error=flag("--error"),
    )
    print(json.dumps(entry, indent=2))


def cmd_run_report(args: list[str]) -> None:
    """Print the last N days of run stages as a funnel.

    Args: [--days N] [--json]
    """
    import run_log

    days = 7
    if "--days" in args:
        i = args.index("--days")
        if i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                print("--days takes an integer")
                sys.exit(1)

    entries = run_log.read_entries(days)
    if "--json" in args:
        print(json.dumps(entries, indent=2))
        return

    if not entries:
        print(f"No run metrics in the last {days} days ({run_log.RUN_METRICS_LOG}).")
        print("Nothing has run since logging was added, or the nightly task is not firing.")
        return

    by_date: dict[str, list[dict]] = {}
    for entry in entries:
        by_date.setdefault(entry["date"], []).append(entry)

    for date in sorted(by_date):
        stages = by_date[date]
        added = sum(s["metrics"].get("added", 0) for s in stages)
        missing = [s for s in run_log.STAGES if not any(x["stage"] == s for x in stages)]
        print(f"\n=== {date} — {added} added ===")
        for entry in stages:
            print(f"  {_format_stage(entry)}")
        if missing:
            # A stage with no record did not run. That is the distinction the
            # prose log could never make, and the usual cause of a thin night.
            print(f"  !! no record for: {', '.join(missing)}")


def _format_stage(entry: dict) -> str:
    metrics = entry.get("metrics", {})
    if not entry.get("ok", True):
        return f"{entry['stage']:<12} FAILED  {entry.get('error', '')}"

    if entry["stage"] == "scan-leads":
        body = (
            f"mail {metrics.get('messages_matched', 0)} matched"
            f" / {metrics.get('messages_new', 0)} new"
            f" / {metrics.get('messages_scanned', 0)} from a source"
            f" -> {metrics.get('candidates', 0)} candidates"
            f", {metrics.get('accepted', 0)} accepted"
            f", {metrics.get('wttj_pending', 0)} wttj pending"
            f" -> {metrics.get('added', 0)} added"
            f" ({metrics.get('skipped_duplicate', 0)} dup)"
        )
    elif entry["stage"] == "search":
        body = (
            f"{metrics.get('queries', 0)} queries"
            f" ({metrics.get('zero_result_queries', 0)} returned nothing)"
            f", {metrics.get('postings_scanned', 0)} postings"
            f" -> {metrics.get('high', 0)} high / {metrics.get('medium', 0)} medium"
            f", {metrics.get('already_tracked', 0)} already tracked"
        )
    elif entry["stage"] == "wttj":
        body = (
            f"{metrics.get('payload_jobs', 0)} from GraphQL"
            f" -> {metrics.get('resolved', 0)} resolved"
            f" -> {metrics.get('added', 0)} added"
            f" ({metrics.get('skipped_duplicate', 0)} dup)"
        )
    elif entry["stage"] == "search-add":
        body = (
            f"{metrics.get('folders_found', 0)} folders"
            f" -> {metrics.get('added', 0)} added"
            f" ({metrics.get('skipped_duplicate', 0)} dup)"
        )
    else:
        body = json.dumps(metrics)

    return f"{entry['stage']:<12} {body}"
