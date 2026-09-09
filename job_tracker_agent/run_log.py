"""Durable, machine-readable record of what each nightly run actually did.

The nightly runner captures the model's prose into logs/job-search-<date>.log.
That prose is a summary of a summary: the structured reports that scan-leads and
search-add print go to stdout, get read by the model, and are gone. So a thin
night ("6 new jobs") is indistinguishable from a broken one -- there is no
record of whether the email leg saw zero new mail, saw mail and filtered it all
out, or never authenticated at all.

This module writes one JSON object per stage per run to RUN_METRICS_LOG, so the
funnel survives the run and `main.py run-report` can show several nights side by
side.

The file is JSONL and append-only. A corrupt line is skipped on read rather than
failing the report -- a logging file should never be able to break the thing it
is logging.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from config import RUN_METRICS_LOG

# Stages a nightly run passes through. Anything not in here is still accepted;
# the list is what run-report knows how to lay out.
STAGES = ("scan-leads", "wttj", "linkedin", "search", "search-add")


def record(
    stage: str,
    *,
    ok: bool = True,
    metrics: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Append one stage record and return it.

    Never raises: a failure to write metrics must not take down the run that
    produced them.
    """
    now = datetime.now(timezone.utc)
    entry: dict[str, Any] = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": now.strftime("%Y-%m-%d"),
        "stage": stage,
        "ok": ok,
        "metrics": metrics or {},
    }
    if error:
        entry["error"] = error

    try:
        RUN_METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RUN_METRICS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as e:  # noqa: BLE001
        print(f"  (run-metrics write failed: {e})")

    return entry


def read_entries(days: int = 7) -> list[dict[str, Any]]:
    """Every recorded stage from the last `days` days, oldest first."""
    if not RUN_METRICS_LOG.exists():
        return []

    cutoff = _cutoff(days)
    entries: list[dict[str, Any]] = []
    for line in RUN_METRICS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("date", "") >= cutoff:
            entries.append(entry)
    return entries


def _cutoff(days: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
