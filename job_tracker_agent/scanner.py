"""Scan ~/.proficiently/jobs/ folders and classify job states."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from config import APPLIED_STATUSES, DEAD_STATUSES, DRAFT_STATUSES, JOBS_DIR
from models import JobRecord, JobState


def _slugify(text: str) -> str:
    """Reduce to bare lowercase alphanumerics for slug comparison."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _slug_matches(folder_slug: str, candidate: str) -> bool:
    """True if candidate looks like the company the folder slug was built from."""
    cand = _slugify(candidate)
    if not cand or not folder_slug:
        return False
    # The folder slug is company-slug + "-YYYY-MM-DD"; the date is already
    # stripped by the caller in practice, but strip any trailing digits too.
    base = re.sub(r"\d+$", "", folder_slug)
    if not base:
        return False
    head = cand[:8]
    return bool(head) and (base.startswith(head) or cand.startswith(base[:8]))


def parse_posting_md(posting_path: Path) -> dict[str, str]:
    """Parse a posting.md file for company, title, URL.

    Expected format line 1: # Title - Company
    Then **Apply URL:** / **Company:** / **Location:** fields.
    """
    result: dict[str, str] = {
        "title": "",
        "company": "",
        "posting_url": "",
        "date_captured": "",
    }

    if not posting_path.exists():
        return result

    text = posting_path.read_text(encoding="utf-8", errors="replace")
    lines = text.strip().split("\n")

    # Line 1: "# Title <sep> Company". The canonical separator is " - ", but
    # en/em dashes and " @ " show up too.
    #
    # Which side holds the company is not reliably fixed: across the existing
    # corpus, 132 of 203 em-dash headers put the company on the right and 46
    # put it on the left. So rather than assume, use the folder slug as the
    # tiebreaker (it is derived from the company) and fall back to the
    # documented "Title <sep> Company" order when the slug settles nothing.
    if lines and lines[0].startswith("#"):
        header = lines[0].lstrip("#").strip()

        sep = next((s for s in (" — ", " – ", " - ", " @ ") if s in header), "")
        if sep:
            left, right = (part.strip() for part in header.rsplit(sep, 1))
            folder_slug = _slugify(posting_path.parent.name)
            left_is_company = _slug_matches(folder_slug, left)
            right_is_company = _slug_matches(folder_slug, right)

            if left_is_company and not right_is_company:
                result["company"], result["title"] = left, right
            else:
                # Right-hand company: the documented order, the corpus
                # majority, and the remaining case when the slug is ambiguous.
                result["title"], result["company"] = left, right
        else:
            result["title"] = header

    # Parse metadata fields (override header-extracted values where present)
    # Use a generic regex approach to handle varied markdown formatting
    prev_was_url_label = False
    for line in lines:
        stripped = line.strip().lstrip("- ")

        # URL fields — catch any labeled line containing a URL
        if not result["posting_url"]:
            # Match URLs with or without protocol
            url_in_line = re.search(r"(https?://\S+)", stripped)
            if not url_in_line:
                # Try protocol-less URLs on URL-labeled lines
                bare_url = re.search(r"([\w.-]+\.(?:com|io|net|org|co)/\S+)", stripped)
                if bare_url:
                    url_in_line = bare_url

            # Case 1: URL on same line as label
            # Match labels containing: url, source, apply (but not generic words)
            if url_in_line:
                label_match = re.match(
                    r"(?:\*\*)?(?:[\w\s]*(?:url|source|apply)[\w\s()\-]*)"
                    r"(?:\*\*)?:?\s*\*?\*?\s*",
                    stripped,
                    re.IGNORECASE,
                )
                if label_match:
                    result["posting_url"] = url_in_line.group(1).strip().rstrip(")")
                    prev_was_url_label = False
                    continue

            # Case 2: URL on line after a URL label (e.g., "## Direct Apply URL\nhttps://...")
            if prev_was_url_label and url_in_line:
                result["posting_url"] = url_in_line.group(1).strip().rstrip(")")
                prev_was_url_label = False
                continue

            # Check if this line IS a URL label (for next iteration)
            url_label = re.match(
                r"(?:#{1,3}\s+)?(?:\*\*)?(?:[\w\s]*(?:url|source|apply)[\w\s()\-]*)(?:\*\*)?:?\s*$",
                stripped,
                re.IGNORECASE,
            )
            prev_was_url_label = bool(url_label)
        else:
            prev_was_url_label = False

        # Company field
        company_match = re.match(
            r"\*\*Company\*?\*?:?\*?\*?\s*(.+)", stripped
        )
        if company_match:
            result["company"] = company_match.group(1).strip()
            continue

        # Title field (for "# Posting" headers that need override)
        title_match = re.match(
            r"\*\*(?:Title|Role|Position)\*?\*?:?\*?\*?\s*(.+)", stripped
        )
        if title_match and (not result["title"] or result["title"] == "Posting"):
            result["title"] = title_match.group(1).strip()
            continue

        # Date fields
        date_match_line = re.match(
            r"\*\*(?:Captured|Fetched)\*?\*?:?\*?\*?\s*(\d{4}-\d{2}-\d{2})",
            stripped,
        )
        if date_match_line:
            result["date_captured"] = date_match_line.group(1)
            continue

    return result


def parse_applied_md(applied_path: Path) -> dict[str, str]:
    """Parse an applied.md file for status, date, ATS type.

    Handles two formats:
    1. Structured: "- **Date**: ...\n- **Status**: ..."
    2. Inline: "**Date:** 2026-08-01 — **ATS:** Greenhouse — **Status:** Draft..."
    """
    result: dict[str, str] = {
        "status": "",
        "status_raw": "",
        "date_applied": "",
        "ats_type": "",
    }

    if not applied_path.exists():
        return result

    text = applied_path.read_text(encoding="utf-8", errors="replace")

    # Collect ALL status/date/ats lines, use the LAST one found
    # (files often have initial "Draft" then later "Submitted" after manual submission)
    all_dates = re.findall(r"\*\*Date:?\*\*:?\s*(\d{4}-\d{2}-\d{2})", text)
    all_ats = re.findall(r"\*\*ATS:?\*\*:?\s*([^—*\n]+)", text)
    all_status = re.findall(r"\*\*Status:?\*\*:?\s*([^\n]+)", text)

    if all_dates:
        result["date_applied"] = all_dates[-1].strip()
    if all_ats:
        result["ats_type"] = all_ats[-1].strip()

    # For status: use the LAST status line, but prefer any "applied/submitted"
    # status over a "draft" status (user's workflow: fill form, submit manually)
    if all_status:
        # Check if ANY status line indicates submission
        best_status = all_status[-1].strip()
        for s in all_status:
            classified = classify_status(s.strip())
            if classified == "applied":
                best_status = s.strip()
                break  # found a positive submission — use it

        result["status_raw"] = best_status
        result["status"] = classify_status(best_status)

    # Also check structured format (- **Date**: ...)
    struct_dates = re.findall(r"-\s*\*\*Date\*\*:\s*(\d{4}-\d{2}-\d{2})", text)
    struct_ats = re.findall(r"-\s*\*\*ATS\*\*:\s*([^\n]+)", text)
    struct_status = re.findall(r"-\s*\*\*Status\*\*:\s*([^\n]+)", text)

    if struct_dates and not all_dates:
        result["date_applied"] = struct_dates[-1].strip()
    if struct_ats and not all_ats:
        result["ats_type"] = struct_ats[-1].strip()
    if struct_status and not all_status:
        best = struct_status[-1].strip()
        for s in struct_status:
            if classify_status(s.strip()) == "applied":
                best = s.strip()
                break
        result["status_raw"] = best
        result["status"] = classify_status(best)

    return result


def classify_status(raw_status: str) -> str:
    """Classify a raw status string into a canonical state.

    Returns one of: 'applied', 'draft', 'dead', 'unknown'
    """
    lower = raw_status.lower()

    # Check for applied/submitted
    for keyword in APPLIED_STATUSES:
        if keyword in lower:
            # But not "NOT submitted"
            if "not submitted" in lower or "not applied" in lower:
                # Check for draft
                if any(k in lower for k in DRAFT_STATUSES):
                    return "draft"
                return "dead"
            return "applied"

    # Check for draft
    for keyword in DRAFT_STATUSES:
        if keyword in lower:
            return "draft"

    # Check for dead statuses
    for keyword in DEAD_STATUSES:
        if keyword in lower:
            return "dead"

    return "unknown"


def scan_job_folders() -> list[JobRecord]:
    """Scan all ~/.proficiently/jobs/ folders and build JobRecord list."""
    if not JOBS_DIR.exists():
        print(f"Jobs directory not found: {JOBS_DIR}")
        return []

    records: list[JobRecord] = []
    skipped_dead = 0

    for folder in sorted(JOBS_DIR.iterdir()):
        if not folder.is_dir():
            continue

        slug = folder.name
        posting_path = folder / "posting.md"
        applied_path = folder / "applied.md"
        resume_path = folder / "resume.md"
        cover_letter_path = folder / "cover-letter.md"

        # Parse posting.md for basic info
        posting = parse_posting_md(posting_path)
        if not posting["title"] and not posting["company"]:
            # Can't identify this job at all
            continue

        # Determine state
        has_resume = resume_path.exists()
        has_cover_letter = cover_letter_path.exists()
        has_applied = applied_path.exists()

        record = JobRecord(
            company=posting.get("company", ""),
            title=posting.get("title", ""),
            posting_url=posting.get("posting_url", ""),
            folder_slug=slug,
            date_added=posting.get("date_captured", ""),
        )

        if has_applied:
            applied = parse_applied_md(applied_path)
            record.ats_type = applied.get("ats_type", "")
            record.date_applied = applied.get("date_applied", "")

            status = applied.get("status", "unknown")

            if status == "applied":
                record.state = JobState.AWAITING
            elif status == "draft":
                # Draft = form was filled by automation. In practice, user
                # submits manually after. Treat as awaiting response.
                record.state = JobState.AWAITING
                record.notes = f"Draft (assumed submitted): {applied.get('status_raw', '')}"
            elif status == "dead":
                record.state = JobState.DEAD
                record.notes = applied.get("status_raw", "")
                skipped_dead += 1
                # Still add to records so caller can decide
            else:
                # Unknown status with applied.md — treat as awaiting
                record.state = JobState.AWAITING
                record.notes = f"Status: {applied.get('status_raw', 'unknown')}"
        elif has_resume:
            record.state = JobState.READY
            record.date_prepared = _get_file_date(resume_path)
        else:
            record.state = JobState.TO_APPLY

        # Fill in resume/cover letter paths if they exist
        if has_resume:
            # Find PDF if exists
            pdfs = list(folder.glob("*Resume*.pdf"))
            if pdfs:
                record.resume_path = pdfs[0].name
            else:
                record.resume_path = "resume.md"

        if has_cover_letter:
            pdfs = list(folder.glob("*Cover Letter*.pdf"))
            if pdfs:
                record.cover_letter_path = pdfs[0].name
            else:
                record.cover_letter_path = "cover-letter.md"

        records.append(record)

    print(f"Scanned {len(records)} job folders ({skipped_dead} dead/expired).")
    return records


def _get_file_date(path: Path) -> str:
    """Get file modification date as ISO string."""
    try:
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""


def find_folder_for_url(url: str) -> Path | None:
    """Find the ~/.proficiently/jobs/ folder whose posting.md matches this URL.

    Shared by cmd_prepare_move and sheets.apply_prep_batch so the two cannot
    drift apart on how a sheet row is linked to a job folder.
    """
    from sheets import normalize_url

    if not JOBS_DIR.exists():
        return None

    target = normalize_url(url)
    for folder in JOBS_DIR.iterdir():
        if not folder.is_dir():
            continue
        posting_path = folder / "posting.md"
        if not posting_path.exists():
            continue
        posting = parse_posting_md(posting_path)
        if posting.get("posting_url") and normalize_url(posting["posting_url"]) == target:
            return folder
    return None


def detect_materials(folder: Path) -> dict[str, str]:
    """Find the resume and cover letter filenames inside a job folder.

    Prefers a PDF (that is what gets uploaded to the ATS) and falls back to the
    markdown source so a rendering failure never drops a job off the board.
    Returns filenames relative to the folder, not absolute paths.
    """
    found: dict[str, str] = {}

    resume_pdfs = sorted(folder.glob("*Resume*.pdf")) + sorted(folder.glob("*resume*.pdf"))
    if resume_pdfs:
        found["resume_path"] = resume_pdfs[0].name
    elif (folder / "resume.md").exists():
        found["resume_path"] = "resume.md"

    cl_pdfs = sorted(folder.glob("*Cover Letter*.pdf")) + sorted(folder.glob("*cover*letter*.pdf"))
    if cl_pdfs:
        found["cover_letter_path"] = cl_pdfs[0].name
    elif (folder / "cover-letter.md").exists():
        found["cover_letter_path"] = "cover-letter.md"

    return found
