"""Commands that reconcile ~/.proficiently/jobs/ folders with the sheet.

Everything here reads the on-disk job folders through scanner.py and then
writes what it found into the spreadsheet.
"""

from __future__ import annotations

import json
import sys

from config import TAB_NAMES
from prep_models import FETCH_FAILED_SENTINEL

from .dates import today


def cmd_backfill() -> None:
    """Import all ~/.proficiently/jobs/ into the spreadsheet."""
    from models import JobState
    from scanner import scan_job_folders
    from sheets import bulk_add_rows, connect, get_all_urls, setup_sheet

    spreadsheet = connect()
    setup_sheet(spreadsheet)

    # Get existing URLs to avoid duplicates
    existing_urls = get_all_urls(spreadsheet)
    print(f"Found {len(existing_urls)} existing URLs in sheet.")

    # Scan all folders
    records = scan_job_folders()

    # Group by target tab, skipping duplicates and dead jobs
    by_tab: dict[str, list] = {tab: [] for tab in TAB_NAMES}
    skipped_dups = 0
    skipped_dead = 0

    for record in records:
        if record.state == JobState.DEAD:
            skipped_dead += 1
            continue

        url = record.posting_url.strip()
        if url and url.lower() in existing_urls:
            skipped_dups += 1
            continue

        tab = record.state.value
        if tab in by_tab:
            by_tab[tab].append(record)
            if url:
                existing_urls.add(url.lower())

    # Bulk add to each tab
    total = 0
    for tab, tab_records in by_tab.items():
        if tab_records:
            count = bulk_add_rows(spreadsheet, tab, tab_records)
            print(f"  Added {count} rows to '{tab}'")
            total += count

    print(f"\nBackfill complete: {total} added, {skipped_dups} duplicates skipped, {skipped_dead} dead/expired skipped.")


def cmd_scan_folders() -> None:
    """Scan folders and print state summary as JSON."""
    from scanner import scan_job_folders

    records = scan_job_folders()
    by_state: dict[str, list[dict]] = {}
    for r in records:
        state = r.state.value
        if state not in by_state:
            by_state[state] = []
        by_state[state].append({
            "company": r.company,
            "title": r.title,
            "url": r.posting_url,
            "folder": r.folder_slug,
        })

    summary = {state: len(items) for state, items in by_state.items()}
    print(json.dumps({"summary": summary, "details": by_state}, indent=2))


def cmd_prepare_move(url: str) -> None:
    """Move a URL from 'to apply' to 'ready to apply', auto-detecting folder data.

    Scans ~/.proficiently/jobs/ for a matching folder, extracts company, title,
    resume/cover-letter paths, and populates all columns automatically.
    """

    from scanner import detect_materials, find_folder_for_url, parse_posting_md
    from sheets import connect, find_by_url, move_row

    spreadsheet = connect()

    # Find in sheet
    existing = find_by_url(spreadsheet, url)
    if not existing:
        print(json.dumps({"success": False, "error": f"URL not found in sheet: {url}"}))
        sys.exit(1)
    if existing.tab != "to apply":
        print(json.dumps({"success": False, "error": f"URL in '{existing.tab}', not 'to apply'"}))
        sys.exit(1)

    # Find matching proficiently folder
    matched_folder = find_folder_for_url(url)

    # Build updated record
    extra: dict[str, str] = {
        "date_prepared": today(),
    }

    if matched_folder:
        extra["folder_slug"] = matched_folder.name

        # Parse posting for company/title
        posting = parse_posting_md(matched_folder / "posting.md")
        if posting.get("company"):
            extra["company"] = posting["company"]
        if posting.get("title") and posting["title"] != FETCH_FAILED_SENTINEL:
            extra["title"] = posting["title"]

        extra.update(detect_materials(matched_folder))
    else:
        # No folder found — still move but with minimal data
        # Try to get company/title from existing row or URL
        from url_parser import extract_company_from_url, title_case_company
        if not existing.record.company:
            slug, _ = extract_company_from_url(url)
            if slug:
                extra["company"] = title_case_company(slug)

    updated = existing.record.model_copy(update=extra)
    success = move_row(spreadsheet, url, "to apply", "ready to apply", updated)

    result = {
        "success": success,
        "company": updated.company,
        "title": updated.title,
        "folder": updated.folder_slug,
        "resume": updated.resume_path,
        "cover_letter": updated.cover_letter_path,
    }
    print(json.dumps(result, indent=2))


def add_jobs_to_apply(jobs: list[dict[str, str]], date_added: str) -> dict:
    """Dedup jobs against every tab, then batch-append the survivors to "to apply".

    Each job is {company, title, url, folder} with an optional "notes". Returns
    the added/skipped report rather than printing it, so both `search-add` and
    `scan-leads` can wrap it in their own output.

    Callers are responsible for having stripped secrets from the URLs first —
    see link_extract.strip_secrets. Anything passed here ends up in the sheet.
    """
    from models import JobRecord
    from sheets import bulk_add_rows, connect, get_all_urls, normalize_url

    if not jobs:
        return {
            "added": [],
            "skipped_duplicate": [],
            "total_scanned": 0,
            "total_added": 0,
        }

    spreadsheet = connect()
    existing_urls = get_all_urls(spreadsheet)

    to_add: list[JobRecord] = []
    skipped_dup: list[dict[str, str]] = []

    for job in jobs:
        norm = normalize_url(job["url"])
        if norm in existing_urls:
            skipped_dup.append({"company": job.get("company", ""), "url": job["url"]})
            continue
        to_add.append(JobRecord(
            company=job.get("company", ""),
            title=job.get("title", ""),
            posting_url=job["url"],
            date_added=date_added,
            folder_slug=job.get("folder", ""),
            notes=job.get("notes", ""),
        ))
        existing_urls.add(norm)  # Prevent intra-batch duplicates

    if to_add:
        bulk_add_rows(spreadsheet, "to apply", to_add)

    return {
        "added": [
            {
                "company": r.company,
                "title": r.title,
                "url": r.posting_url,
                "folder": r.folder_slug,
            }
            for r in to_add
        ],
        "skipped_duplicate": skipped_dup,
        "total_scanned": len(jobs),
        "total_added": len(to_add),
    }


def cmd_search_add(args: list[str]) -> None:
    """Add jobs from a search run to 'to apply' tab.

    Scans ~/.proficiently/jobs/ folders by date, deduplicates against sheet,
    and batch-adds new entries.

    Args: [--date YYYY-MM-DD] [--urls url1,url2,...]
    """
    from config import JOBS_DIR
    from scanner import parse_posting_md
    from sheets import normalize_url

    # Parse args
    target_date = today()
    explicit_urls: list[str] = []

    i = 0
    while i < len(args):
        if args[i] == "--date" and i + 1 < len(args):
            target_date = args[i + 1]
            i += 2
        elif args[i] == "--urls" and i + 1 < len(args):
            explicit_urls = [u.strip() for u in args[i + 1].split(",") if u.strip()]
            i += 2
        else:
            i += 1

    # Scan folders ending with target date
    found_jobs: list[dict[str, str]] = []
    if JOBS_DIR.exists():
        for folder in sorted(JOBS_DIR.iterdir()):
            if not folder.is_dir():
                continue
            if not folder.name.endswith(target_date):
                continue
            posting_path = folder / "posting.md"
            if not posting_path.exists():
                continue
            posting = parse_posting_md(posting_path)
            url = posting.get("posting_url", "").strip()
            if url:
                found_jobs.append({
                    "company": posting.get("company", ""),
                    "title": posting.get("title", ""),
                    "url": url,
                    "folder": folder.name,
                })

    # Add explicit URLs not already found via folder scan
    found_urls_lower = {normalize_url(j["url"]) for j in found_jobs}
    for url in explicit_urls:
        if normalize_url(url) not in found_urls_lower:
            # Try to find a matching folder for this URL
            matched = False
            if JOBS_DIR.exists():
                for folder in JOBS_DIR.iterdir():
                    if not folder.is_dir():
                        continue
                    posting_path = folder / "posting.md"
                    if posting_path.exists():
                        posting = parse_posting_md(posting_path)
                        folder_url = posting.get("posting_url", "").strip()
                        if folder_url and normalize_url(folder_url) == normalize_url(url):
                            found_jobs.append({
                                "company": posting.get("company", ""),
                                "title": posting.get("title", ""),
                                "url": url,
                                "folder": folder.name,
                            })
                            matched = True
                            break

            if not matched:
                # No folder — add with minimal info, use URL domain as company
                from url_parser import extract_company_from_url, title_case_company
                slug, _ = extract_company_from_url(url)
                company = title_case_company(slug) if slug else ""
                found_jobs.append({
                    "company": company,
                    "title": "",
                    "url": url,
                    "folder": f"{slug or 'unknown'}-{target_date}",
                })
            found_urls_lower.add(normalize_url(url))

    report = add_jobs_to_apply(found_jobs, target_date)

    import run_log

    run_log.record("search-add", metrics={
        "date": target_date,
        "folders_found": len(found_jobs),
        "explicit_urls": len(explicit_urls),
        "added": report.get("total_added", 0),
        "skipped_duplicate": len(report.get("skipped_duplicate", [])),
    })

    print(json.dumps(report, indent=2))
