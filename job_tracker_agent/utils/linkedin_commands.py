"""CLI wrapper for the LinkedIn search source.

The fetching and parsing live in linkedin_search.py; this file is argument
parsing, the sheet write, and the run-log record. It reuses
lead_scanner.is_relevant for title filtering and folder_commands.add_jobs_to_apply
for dedup and append, so a LinkedIn result reaches the board by exactly the same
path an email lead does.
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


def cmd_linkedin_search(args: list[str]) -> None:
    """Search LinkedIn's guest endpoint and add new postings to "to apply".

    Args: [--keywords "a,b"] [--location TEXT] [--since 24h|7d] [--pages N]
          [--dry-run]
    """
    import linkedin_search
    import run_log
    from config import LINKEDIN_MAX_PAGES
    from lead_scanner import is_relevant
    from url_parser import extract_company_from_url
    from utils.dates import today

    try:
        pages = int(_flag_value(args, "--pages", str(LINKEDIN_MAX_PAGES)))
    except ValueError:
        print("--pages takes an integer")
        sys.exit(1)

    queries = _build_queries(args)

    # LinkedIn is a mixed-quality source in the same sense the low-quality email
    # senders are: plenty of real postings that are simply not this search. So
    # a title has to clear the allow list, not merely miss the deny list.
    def title_filter(title: str) -> tuple[bool, str]:
        return is_relevant(title, "", "low")

    try:
        report = linkedin_search.search(
            queries, max_pages=pages, title_filter=title_filter
        )
    except Exception as e:  # noqa: BLE001
        run_log.record("linkedin", ok=False, error=f"{type(e).__name__}: {e}")
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, indent=2))
        sys.exit(1)

    kept = [
        {
            "company": p.company,
            "title": p.title,
            "location": p.location,
            "url": p.url,
            "query": p.query,
        }
        for p in report.postings
    ]

    if "--dry-run" in args:
        print(json.dumps(
            {"dry_run": True, "metrics": report.metrics(), "postings": kept}, indent=2
        ))
        return

    date_added = today()
    jobs = []
    for posting in report.postings:
        slug, _ats = extract_company_from_url(posting.url)
        if not slug:
            # A /jobs/view/ URL names no employer, so fall back to the company
            # off the card. prep re-fetches the posting and corrects both.
            slug = _slugify(posting.company)
        jobs.append({
            "company": posting.company,
            "title": posting.title,
            "url": posting.url,
            "folder": f"{slug or 'unknown'}-{date_added}",
            # The source belongs in Notes: a LinkedIn URL is not an employer
            # URL, so when prep cannot fetch one this says why without anyone
            # having to open the link.
            "notes": f"source: linkedin ({posting.query})",
        })

    from utils.folder_commands import add_jobs_to_apply

    result = add_jobs_to_apply(jobs, date_added)

    metrics = report.metrics()
    metrics["added"] = result["total_added"]
    metrics["skipped_duplicate"] = len(result["skipped_duplicate"])
    run_log.record("linkedin", ok=not report.errors, metrics=metrics)

    print(json.dumps({
        "metrics": metrics,
        "errors": report.errors,
        "added": result["added"],
        "skipped_duplicate": result["skipped_duplicate"],
    }, indent=2))


def _build_queries(args: list[str]) -> list[dict[str, str]]:
    """Config queries by default; a flag replaces the whole set, not one field.

    Overriding a single field across four configured queries has no sensible
    meaning, so --keywords builds a fresh query per keyword instead.
    """
    from config import LINKEDIN_QUERIES

    raw_keywords = _flag_value(args, "--keywords")
    location = _flag_value(args, "--location")
    since = _flag_value(args, "--since")

    keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
    if not keywords and not location and not since:
        return list(LINKEDIN_QUERIES)

    if not keywords:
        # Location or recency alone: keep the configured keyword set and
        # re-point it, so "--location Berlin" still searches the right roles.
        keywords = [q["keywords"] for q in LINKEDIN_QUERIES]

    tpr = {"24h": "r86400", "7d": "r604800", "": "r86400"}.get(since, since)

    queries: list[dict[str, str]] = []
    for keyword in keywords:
        query = {
            "label": f"{keyword} ({location or 'United States'})",
            "keywords": keyword,
            "location": location or "United States",
            "f_TPR": tpr,
            "f_E": "3,4",
        }
        if not location:
            query["f_WT"] = "2"
        queries.append(query)
    return queries


def _slugify(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
