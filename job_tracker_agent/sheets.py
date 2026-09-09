"""Google Sheets wrapper using gspread for job tracker CRUD."""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import gspread
from config import (
    AUTHORIZED_USER_FILE,
    CONFIG_FILE,
    CREDENTIALS_FILE,
    HEADERS_BY_TAB,
    SPREADSHEET_ID,
    TAB_NAMES,
)
from gspread.utils import ValueInputOption
from models import JobRecord, SheetRow

if TYPE_CHECKING:
    from prep_models import PrepBatch


def authenticate() -> gspread.Client:
    """Run/reuse the gspread OAuth flow. Returns an authorized client."""
    if not CREDENTIALS_FILE.exists():
        print(f"ERROR: No credentials.json found at {CREDENTIALS_FILE}")
        print("Download OAuth 2.0 Client ID (Desktop) from Google Cloud Console.")
        print(f"Save it to: {CREDENTIALS_FILE}")
        sys.exit(1)

    if not AUTHORIZED_USER_FILE.exists():
        print("No authorized user found. Running OAuth flow...")

    return gspread.oauth(
        credentials_filename=str(CREDENTIALS_FILE),
        authorized_user_filename=str(AUTHORIZED_USER_FILE),
    )


def connect() -> gspread.Spreadsheet:
    """Connect to the Google Sheet. Runs OAuth if needed."""
    if not SPREADSHEET_ID:
        print(f"ERROR: No spreadsheet_id configured in {CONFIG_FILE}")
        print("Add it under [board]:")
        print('    [board]')
        print('    spreadsheet_id = "<the id from your sheet URL>"')
        print("The id is the long segment between /d/ and /edit.")
        sys.exit(1)
    return authenticate().open_by_key(SPREADSHEET_ID)


def ensure_tab(spreadsheet: gspread.Spreadsheet, tab: str) -> gspread.Worksheet:
    """Return a worksheet, creating it with headers if it is missing.

    Lets a sheet that predates a new tab heal itself on first use rather than
    failing with WorksheetNotFound partway through a batch.
    """
    try:
        return spreadsheet.worksheet(tab)
    except gspread.WorksheetNotFound:
        headers = HEADERS_BY_TAB[tab]
        ws = spreadsheet.add_worksheet(title=tab, rows=1000, cols=len(headers))
        ws.update(range_name="A1", values=[headers])
        ws.format("A1:Z1", {"textFormat": {"bold": True}})
        print(f"  Created missing tab '{tab}'.")
        return ws


def setup_sheet(spreadsheet: gspread.Spreadsheet) -> None:
    """Create every tab in TAB_NAMES with headers if it does not exist."""
    existing_tabs = [ws.title for ws in spreadsheet.worksheets()]

    for tab_name in TAB_NAMES:
        if tab_name in existing_tabs:
            ws = spreadsheet.worksheet(tab_name)
            # Check if headers exist
            row1 = ws.row_values(1)
            if not row1:
                ws.update(range_name="A1", values=[HEADERS_BY_TAB[tab_name]])
            print(f"  Tab '{tab_name}' already exists.")
        else:
            headers = HEADERS_BY_TAB[tab_name]
            ws = spreadsheet.add_worksheet(
                title=tab_name, rows=1000, cols=len(headers)
            )
            ws.update(range_name="A1", values=[headers])
            # Bold the header row
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
            print(f"  Created tab '{tab_name}' with {len(headers)} columns.")

    # Remove default "Sheet1" if it exists and is empty
    if "Sheet1" in existing_tabs:
        try:
            sheet1 = spreadsheet.worksheet("Sheet1")
            if not sheet1.get_all_values()[1:]:  # only header or empty
                spreadsheet.del_worksheet(sheet1)
                print("  Removed empty 'Sheet1'.")
        except Exception:  # noqa: BLE001, S110
            pass


def read_tab(spreadsheet: gspread.Spreadsheet, tab: str) -> list[SheetRow]:
    """Read all data rows from a tab (skips header)."""
    ws = spreadsheet.worksheet(tab)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []

    result = []
    for i, row in enumerate(rows[1:], start=2):  # row 2+ (1-indexed, skip header)
        if not any(cell.strip() for cell in row):
            continue  # skip blank rows
        record = JobRecord.from_row(row, tab)
        result.append(SheetRow(row_number=i, tab=tab, record=record))
    return result


def find_by_url(
    spreadsheet: gspread.Spreadsheet, url: str
) -> SheetRow | None:
    """Find a job by posting URL across all tabs."""
    normalized = normalize_url(url)
    for tab in TAB_NAMES:
        try:
            rows = read_tab(spreadsheet, tab)
            for sheet_row in rows:
                if normalize_url(sheet_row.record.posting_url) == normalized:
                    return sheet_row
        except gspread.WorksheetNotFound:
            continue
    return None


def find_by_folder(
    spreadsheet: gspread.Spreadsheet, folder_slug: str
) -> SheetRow | None:
    """Find a job by folder slug across all tabs."""
    for tab in TAB_NAMES:
        try:
            rows = read_tab(spreadsheet, tab)
            for sheet_row in rows:
                if sheet_row.record.folder_slug == folder_slug:
                    return sheet_row
        except gspread.WorksheetNotFound:
            continue
    return None


def add_row(
    spreadsheet: gspread.Spreadsheet, tab: str, record: JobRecord
) -> int:
    """Append a row to a tab. Returns the new row number."""
    ws = spreadsheet.worksheet(tab)
    row_data = record.to_row(tab)
    ws.append_row(row_data, value_input_option=ValueInputOption.user_entered)
    # Get actual row number
    all_rows = ws.get_all_values()
    return len(all_rows)


def move_row(
    spreadsheet: gspread.Spreadsheet,
    url: str,
    from_tab: str,
    to_tab: str,
    updated_record: JobRecord | None = None,
) -> bool:
    """Move a row from one tab to another.

    If updated_record is provided, uses its data for the new row.
    Otherwise copies the existing data.
    """
    from_ws = spreadsheet.worksheet(from_tab)
    rows = read_tab(spreadsheet, from_tab)
    normalized = normalize_url(url)

    target_row = None
    for sheet_row in rows:
        if normalize_url(sheet_row.record.posting_url) == normalized:
            target_row = sheet_row
            break

    if not target_row:
        print(f"  URL not found in '{from_tab}': {url}")
        return False

    # Use updated record or existing
    record = updated_record if updated_record else target_row.record

    # Add to destination
    to_ws = spreadsheet.worksheet(to_tab)
    row_data = record.to_row(to_tab)
    to_ws.append_row(row_data, value_input_option=ValueInputOption.user_entered)

    # Delete from source
    from_ws.delete_rows(target_row.row_number)

    print(f"  Moved '{record.company} - {record.title}' from '{from_tab}' to '{to_tab}'.")
    return True


def bulk_add_rows(
    spreadsheet: gspread.Spreadsheet, tab: str, records: list[JobRecord]
) -> int:
    """Add multiple rows at once (efficient batch write). Returns count added."""
    if not records:
        return 0

    ws = spreadsheet.worksheet(tab)
    rows_data = [r.to_row(tab) for r in records]
    ws.append_rows(rows_data, value_input_option=ValueInputOption.user_entered)
    return len(rows_data)


def get_all_urls(spreadsheet: gspread.Spreadsheet) -> set[str]:
    """Get all posting URLs across all tabs for dedup."""
    urls: set[str] = set()
    for tab in TAB_NAMES:
        try:
            rows = read_tab(spreadsheet, tab)
            for sheet_row in rows:
                url = sheet_row.record.posting_url.strip()
                if url:
                    urls.add(normalize_url(url))
        except gspread.WorksheetNotFound:
            continue
    return urls


def get_status_counts(spreadsheet: gspread.Spreadsheet) -> dict[str, int]:
    """Get row count per tab."""
    counts: dict[str, int] = {}
    for tab in TAB_NAMES:
        try:
            rows = read_tab(spreadsheet, tab)
            counts[tab] = len(rows)
        except gspread.WorksheetNotFound:
            counts[tab] = 0
    return counts


def normalize_url(url: str) -> str:
    """Normalize a URL for comparison."""
    url = url.strip().rstrip("/")
    # Remove common tracking params
    if "?" in url:
        base, params = url.split("?", 1)
        # Keep only path-significant params
        clean_params = []
        for param in params.split("&"):
            key = param.split("=")[0].lower()
            if key not in {"utm_source", "utm_medium", "utm_campaign", "ref", "source"}:
                clean_params.append(param)
        if clean_params:
            url = base + "?" + "&".join(clean_params)
        else:
            url = base
    return url.lower()


def clean_company_name(raw: str) -> str:
    """Extract just the company name from verbose sheet entries.

    Strips parenthetical descriptions, em-dash suffixes, and common noise.
    Examples:
        'Algolia (AI Search platform; 18,000+ customers...)' -> 'Algolia'
        'Cacheflow (fintech; billing / CPQ)' -> 'Cacheflow'
        '/ Brand:** Together Group — department/brand **Imerza**' -> 'Together Group'
        'Senior Software Engineer' -> ''  (job title, not company)
        'Product Platform Team @ Orum' -> 'Orum'
        'id:** InstantCommerce · **Posting id:** 743...' -> 'InstantCommerce'
    """
    name = raw.strip()

    # Remove markdown bold markers
    name = name.replace("**", "").strip()

    # Strip leading noise patterns: "/ Brand:", "id:", "ID / Posting ID:", "Posting ID:"
    # Run multiple passes since patterns can nest
    name = re.sub(
        r"^[/*\s]*(?:(?:ID\s*/\s*)?Posting\s*ID|Brand|id)[:\s]*",
        "", name, flags=re.IGNORECASE
    ).strip()
    name = re.sub(
        r"^[/*\s]*(?:(?:ID\s*/\s*)?Posting\s*ID|Brand|id)[:\s]*",
        "", name, flags=re.IGNORECASE
    ).strip()

    # Handle "CompanyName · Posting id: 12345" or "CompanyName / 12345" patterns
    if "·" in name or "Posting" in name:
        m = re.match(r"([A-Za-z][\w\s]*?)(?:\s*[·]|\s+Posting)", name)
        if m:
            name = m.group(1).strip()
    # Strip trailing " / <digits>" (posting IDs)
    name = re.sub(r"\s*/\s*\d{5,}$", "", name).strip()

    # Handle @ separator: "Title @ Company" or "Team @ Company" — take right side
    if " @ " in name:
        name = name.rsplit(" @ ", 1)[-1].strip()

    # Take text before first parenthetical
    paren_match = re.match(r"^([^(]+)", name)
    if paren_match:
        name = paren_match.group(1).strip()

    # Take text before em-dash or en-dash description
    for sep in [" — ", " – ", " - "]:
        if sep in name:
            candidate = name.split(sep)[0].strip()
            # Only split if left side looks like a company name (not a job title)
            if len(candidate.split()) <= 5 and not any(
                kw in candidate.lower()
                for kw in ["engineer", "developer", "lead", "senior", "staff",
                           "founding", "full stack", "fullstack", "backend",
                           "frontend", "product"]
            ):
                name = candidate
                break

    # Skip entries that look like job titles rather than company names
    title_keywords = {
        "engineer", "developer", "lead", "senior", "staff", "founding",
        "full stack", "fullstack", "backend", "frontend", "product",
        "software", "programmer", "analyst", "mid level", "forward deployed",
        "full-stack", "cyber",
    }
    if any(kw in name.lower() for kw in title_keywords):
        return ""

    # Final cleanup
    name = name.strip(" ,;./")
    # Skip if too short (single char) or too long (probably not a real name)
    if len(name) <= 1 or len(name) > 80:
        return ""
    return name


def group_rows_by_company(rows: list[SheetRow]) -> dict[str, dict]:
    """Group sheet rows by cleaned company name.

    Rows whose company cleans to an empty string are dropped.

    Returns a dict keyed by lowercased company name, each value:
        {"company": str, "domain": str, "rows": list[SheetRow]}
    where domain is derived from the first URL seen for that company.
    """
    from url_parser import extract_company_domain

    groups: dict[str, dict] = {}
    for r in rows:
        clean = clean_company_name(r.record.company.strip())
        if not clean:
            continue

        key = clean.lower()
        if key not in groups:
            domain = ""
            if r.record.posting_url:
                domain = extract_company_domain(r.record.posting_url)
            groups[key] = {"company": clean, "domain": domain, "rows": []}
        groups[key]["rows"].append(r)

    return groups


def delete_rows_safe(
    worksheet: gspread.Worksheet, row_numbers: list[int]
) -> int:
    """Delete rows from a worksheet, throttled to stay under Sheets API quota.

    Deletes highest row number first so earlier row numbers stay valid.
    Retries once after a 60s wait on HTTP 429, and pauses 15s every 30 deletes.
    Returns the number of rows actually deleted.
    """
    ordered = sorted(set(row_numbers), reverse=True)
    deleted = 0

    for row_num in ordered:
        try:
            worksheet.delete_rows(row_num)
            deleted += 1
        except Exception as e:  # noqa: BLE001
            if "429" in str(e):
                print(f"  Rate limited after {deleted} deletes. Waiting 60s...")
                time.sleep(60)
                worksheet.delete_rows(row_num)
                deleted += 1
            else:
                print(f"  Error deleting row {row_num}: {e}")

        # Rate limit: ~40 deletes/min to stay under quota
        if deleted and deleted % 30 == 0:
            print(f"  Deleted {deleted}/{len(ordered)} rows, pausing...")
            time.sleep(15)

    return deleted


def apply_prep_batch(
    spreadsheet: gspread.Spreadsheet,
    batch: "PrepBatch",
    requested_urls: list[str],
) -> dict[str, Any]:
    """Apply a whole prep run to the sheet in one batched pass.

    Deliberately does NOT loop move_row: each move_row call costs a read_tab
    plus an append plus a delete, and callers usually add a find_by_url on top
    (six more tab reads). At ten jobs that blows the 60-reads-per-minute quota.
    This does three reads and two appends total, then one throttled delete pass.

    Routing:
      prepared                        -> "ready to apply"
      invalid, terminal reason        -> "invalid"
      invalid, transient reason       -> stays in "to apply", diagnostic in Notes
                                         so the next prep run retries it

    Idempotent: a URL already sitting in the destination tab is not appended
    twice, but is still removed from "to apply". Re-running the same result
    file after a crash converges instead of duplicating.
    """
    from config import JOBS_DIR
    from prep_models import PrepStatus
    from scanner import detect_materials, find_folder_for_url

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

    # --- reads: exactly three, up front ---------------------------------
    to_apply = read_tab(spreadsheet, "to apply")
    by_url = {normalize_url(r.record.posting_url): r for r in to_apply}
    already_ready = {
        normalize_url(r.record.posting_url) for r in read_tab(spreadsheet, "ready to apply")
    }
    ensure_tab(spreadsheet, "invalid")  # heals a sheet created before this tab
    already_invalid = {
        normalize_url(r.record.posting_url) for r in read_tab(spreadsheet, "invalid")
    }

    ready_records: list[JobRecord] = []
    invalid_records: list[JobRecord] = []
    rows_to_delete: list[int] = []
    report: dict[str, list[dict[str, str]]] = {
        "moved_to_ready": [],
        "moved_to_invalid": [],
        "stayed": [],
        "already_moved": [],
        "not_in_to_apply": [],
        "notes_updates": [],
    }

    for item in batch.items:
        key = normalize_url(item.url)
        sheet_row = by_url.get(key)

        if sheet_row is None:
            if key in already_ready or key in already_invalid:
                report["already_moved"].append({"url": item.url, "status": item.status.value})
            else:
                report["not_in_to_apply"].append({"url": item.url, "status": item.status.value})
            continue

        record = sheet_row.record

        if item.status is PrepStatus.PREPARED:
            extra: dict[str, str] = {"date_prepared": item.date_prepared or today}

            # Trust folder_name, but verify it actually points at this job.
            folder = None
            if item.folder_name:
                candidate = JOBS_DIR / item.folder_name
                if candidate.is_dir():
                    folder = candidate
            if folder is None:
                folder = find_folder_for_url(item.url)

            if folder is not None:
                extra["folder_slug"] = folder.name
                materials = detect_materials(folder)
                # The agent's own filenames win when the files really exist;
                # detect_materials is the fallback.
                if item.resume_pdf and (folder / item.resume_pdf).is_file():
                    materials["resume_path"] = item.resume_pdf
                if item.cover_letter_pdf and (folder / item.cover_letter_pdf).is_file():
                    materials["cover_letter_path"] = item.cover_letter_pdf
                extra.update(materials)

            if item.company:
                extra["company"] = item.company
            if item.title:
                extra["title"] = item.title
            if item.notes:
                extra["notes"] = item.notes

            if key in already_ready:
                report["already_moved"].append({"url": item.url, "status": "prepared"})
            else:
                ready_records.append(record.model_copy(update=extra))
                report["moved_to_ready"].append(
                    {"url": item.url, "company": extra.get("company", record.company),
                     "folder": extra.get("folder_slug", "")}
                )
            rows_to_delete.append(sheet_row.row_number)

        elif item.is_terminal:
            reason = item.invalid_reason.value if item.invalid_reason else "other"
            extra = {
                "date_resolved": today,
                "source_details": f"{reason}: {item.notes}".strip(),
            }
            if item.company:
                extra["company"] = item.company
            if item.title:
                extra["title"] = item.title

            if key in already_invalid:
                report["already_moved"].append({"url": item.url, "status": "invalid"})
            else:
                invalid_records.append(record.model_copy(update=extra))
                report["moved_to_invalid"].append({"url": item.url, "reason": reason})
            rows_to_delete.append(sheet_row.row_number)

        else:
            # Transient failure. Leave the row where it is and record why, so
            # the next run has the diagnostic and retries.
            reason = item.invalid_reason.value if item.invalid_reason else "other"
            report["stayed"].append({"url": item.url, "reason": reason, "notes": item.notes})
            report["notes_updates"].append(
                {"row": str(sheet_row.row_number), "notes": f"[{today} {reason}] {item.notes}"}
            )

    # --- writes ---------------------------------------------------------
    if ready_records:
        bulk_add_rows(spreadsheet, "ready to apply", ready_records)
    if invalid_records:
        bulk_add_rows(spreadsheet, "invalid", invalid_records)

    # Notes for rows that stay put, written before the deletes shift row numbers.
    if report["notes_updates"]:
        ws = spreadsheet.worksheet("to apply")
        notes_col = HEADERS_BY_TAB["to apply"].index("Notes") + 1
        for upd in report["notes_updates"]:
            try:
                ws.update_cell(int(upd["row"]), notes_col, upd["notes"][:500])
            except Exception as e:  # noqa: BLE001
                print(f"  Could not write notes to row {upd['row']}: {e}")

    deleted = 0
    if rows_to_delete:
        ws = spreadsheet.worksheet("to apply")
        deleted = delete_rows_safe(ws, rows_to_delete)

    return {
        "requested": len(requested_urls),
        "items": len(batch.items),
        "appended_ready": len(ready_records),
        "appended_invalid": len(invalid_records),
        "deleted_from_to_apply": deleted,
        **report,
    }


def dedup_tab(spreadsheet: gspread.Spreadsheet, tab: str) -> dict[str, Any]:
    """Remove duplicate URLs from a tab and return a report.

    Two kinds of duplicate are removed: repeats within the tab (first
    occurrence kept), and rows whose URL already appears in another tab
    (removed here, since the other tab is further along the pipeline).

    Extracted from cmd_dedup so `prep` can dedup directly instead of relying
    on an agent to remember to run it first.
    """
    rows = read_tab(spreadsheet, tab)

    seen: dict[str, list[SheetRow]] = {}
    for r in rows:
        url = normalize_url(r.record.posting_url)
        if url:
            seen.setdefault(url, []).append(r)
    within_tab_dups = {u: rl for u, rl in seen.items() if len(rl) > 1}

    other_urls: dict[str, str] = {}
    for other_tab in (t for t in TAB_NAMES if t != tab):
        try:
            for r in read_tab(spreadsheet, other_tab):
                url = normalize_url(r.record.posting_url)
                if url:
                    other_urls[url] = other_tab
        except gspread.WorksheetNotFound:
            continue

    cross_tab_dups: list[dict[str, Any]] = [
        {
            "url": r.record.posting_url,
            "company": r.record.company,
            "this_tab": tab,
            "also_in": other_urls[normalize_url(r.record.posting_url)],
            "row": r.row_number,
        }
        for r in rows
        if normalize_url(r.record.posting_url) in other_urls
    ]

    rows_to_delete = [r.row_number for rl in within_tab_dups.values() for r in rl[1:]]
    rows_to_delete += [int(d["row"]) for d in cross_tab_dups]

    deleted = 0
    if rows_to_delete:
        deleted = delete_rows_safe(spreadsheet.worksheet(tab), rows_to_delete)

    return {
        "tab": tab,
        "total_rows": len(rows),
        "within_tab_duplicates": [
            {
                "url": rl[0].record.posting_url,
                "company": rl[0].record.company,
                "count": len(rl),
                "rows": [r.row_number for r in rl],
            }
            for rl in within_tab_dups.values()
        ],
        "cross_tab_duplicates": cross_tab_dups,
        "rows_removed": deleted,
    }
