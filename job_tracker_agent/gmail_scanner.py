"""Gmail scanning for job application responses.

Connects to Gmail API (readonly) and scans for rejection/interview emails
from companies in the "awaiting response" sheet tab.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from config import (
    CREDENTIALS_FILE,
    GMAIL_AUTHORIZED_USER_FILE,
    GMAIL_SCAN_LOG,
    GMAIL_SCOPES,
)
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
from googleapiclient.discovery import build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

# Email classification patterns
# Rejection signals, split the same way as the interview ones.
#
# WEAK entries are hedges that appear just as often in confirmation mail
# ("unfortunately, due to volume we may not reply to everyone"), so one on
# its own is not a rejection — it needs a second signal to count.
STRONG_REJECTION_PATTERNS = [
    r"not\s+moving\s+forward",
    r"won'?t\s+be\s+(?:moving|advancing|proceeding)",
    r"will\s+not\s+be\s+(?:moving|advancing)\s+forward",
    r"decided\s+not\s+to\s+(?:proceed|move|continue|pursue)",
    r"other\s+candidates",
    r"not\s+a\s+(?:good\s+)?fit",
    r"position\s+has\s+been\s+filled",
    r"not\s+(?:been\s+)?selected",
    r"unable\s+to\s+(?:move|offer)",
    r"pursue\s+other\s+candidates",
    r"no\s+longer\s+(?:being\s+)?consider",
    r"regret\s+to\s+inform",
    r"not\s+(?:be\s+)?proceed",
]

WEAK_REJECTION_PATTERNS = [
    r"unfortunately",
    r"we\s+have\s+decided",
    r"after\s+careful\s+(?:review|consideration)",
]

REJECTION_PATTERNS = STRONG_REJECTION_PATTERNS + WEAK_REJECTION_PATTERNS

# A conditional strips a phrase of its force: "if you are not selected"
# describes a possible future, not a decision that has been made.
CONDITIONAL_RE = re.compile(
    r"\b(?:if|should|unless|whether|in case)\b[^.!?;]{0,60}$",
    re.IGNORECASE,
)

# Interview signals split by strength.
#
# STRONG signals name a real, scheduled step for this candidate. WEAK ones only
# describe what a hiring process contains — and application-confirmation emails
# routinely walk through "our technical interview loop" or "a coding challenge"
# before anyone has looked at the resume. A weak signal inside a confirmation
# email is therefore not evidence of an interview.
STRONG_INTERVIEW_PATTERNS = [
    r"schedule\s+(?:an?\s+)?interview",
    r"(?:your|check)\s+availability",
    r"like\s+to\s+schedule",
    r"would\s+like\s+to\s+connect",
    r"(?:move|moving)\s+forward\s+with\s+(?:you|your)",
    r"pleased\s+to\s+(?:inform|invite)",
    r"invite\s+you\s+to\s+(?:interview|schedule|meet|chat|speak|a\s+(?:call|conversation|interview))",
    r"interview\s+(?:confirmation|scheduled|invitation)",
]

WEAK_INTERVIEW_PATTERNS = [
    r"(?:next|following)\s+steps?\s+in\s+(?:the|our)\s+(?:process|interview)",
    r"phone\s+screen",
    r"technical\s+interview",
    r"coding\s+(?:challenge|assessment|test)",
    r"meet\s+the\s+team",
    r"(?:take[- ]?home|live[- ]?coding)\s+(?:assignment|assessment|exercise|challenge)",
    r"intro(?:duction)?\s+call",
    r"recruiter\s+(?:screen|call)",
    r"hiring\s+manager\s+(?:call|meeting|round)",
]

INTERVIEW_PATTERNS = STRONG_INTERVIEW_PATTERNS + WEAK_INTERVIEW_PATTERNS

# Words that invert an interview phrase when they sit just before it, e.g.
# "we have decided not to move forward with your candidacy" — a rejection that
# otherwise scores as an invitation.
NEGATION_RE = re.compile(
    r"\b(?:not|never|unable|declin\w*|regret\w*|unfortunately)\b[^.!?]{0,40}$",
    re.IGNORECASE,
)

# Patterns that indicate auto-confirmation (not a real response)
NEUTRAL_PATTERNS = [
    r"thank\s+you\s+for\s+(?:applying|your\s+(?:application|interest))",
    r"(?:application|submission)\s+(?:has\s+been\s+)?received",
    r"we(?:'ve|\s+have)\s+received\s+your\s+(?:application|resume)",
    r"confirm(?:ation|ing)\s+(?:of\s+)?(?:your\s+)?(?:application|submission)",
    r"do[- ]?not[- ]?reply",
    r"noreply",
    r"no[- ]?reply",
]


def gmail_auth() -> Credentials:
    """Authenticate with Gmail API. Opens browser if no token exists."""
    creds = None

    if GMAIL_AUTHORIZED_USER_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(GMAIL_AUTHORIZED_USER_FILE), GMAIL_SCOPES
        )

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError as exc:
                # Refresh tokens issued while the OAuth app is in "Testing"
                # status expire 7 days after consent, used or not; a revoked
                # grant fails the same way. Archive the dead token and fall
                # through to a fresh browser flow instead of failing.
                print(f"Stored Gmail token unusable ({exc}). Re-authorizing.")
                stamp = datetime.now().strftime("%Y%m%d")
                GMAIL_AUTHORIZED_USER_FILE.replace(
                    GMAIL_AUTHORIZED_USER_FILE.with_suffix(
                        GMAIL_AUTHORIZED_USER_FILE.suffix + f".expired-{stamp}"
                    )
                )
                creds = None

        if not refreshed:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"No credentials.json found at {CREDENTIALS_FILE}. "
                    "Download OAuth 2.0 Client ID (Desktop) from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Save credentials
        GMAIL_AUTHORIZED_USER_FILE.write_text(creds.to_json())
        print(f"Gmail tokens saved to: {GMAIL_AUTHORIZED_USER_FILE}")

    return creds


def get_gmail_service():
    """Build Gmail API service."""
    creds = gmail_auth()
    return build("gmail", "v1", credentials=creds)


# Gmail bills per-minute quota units, and a full scan issues two searches per
# company back to back. Without backoff the tail of a 100+ company run comes
# back 403 rateLimitExceeded — which used to be swallowed as "no email found"
# and silently understated the results.
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
MAX_RETRIES = 5


def _is_rate_limit(exc: HttpError) -> bool:
    """True if an HttpError is a quota/rate-limit rejection rather than a real error."""
    if exc.resp.status not in (403, 429):
        return False
    if exc.resp.status == 429:
        return True
    try:
        detail = json.loads(exc.content.decode("utf-8"))
    except (ValueError, AttributeError, UnicodeDecodeError):
        return False
    errors = detail.get("error", {}).get("errors", [])
    return any(e.get("reason") in RATE_LIMIT_REASONS for e in errors)


class GmailRateLimited(Exception):
    """Raised when a call still fails with a quota error after every retry."""


def _execute(request, what: str):
    """Run a Gmail API request, backing off on quota errors.

    Raises GmailRateLimited if the quota never clears, so the caller can mark
    the company unscanned rather than treating it as a clean "no results".
    """
    delay = 2.0
    for attempt in range(MAX_RETRIES):
        try:
            return request().execute()
        except HttpError as e:
            if not _is_rate_limit(e):
                raise
            if attempt == MAX_RETRIES - 1:
                raise GmailRateLimited(what) from e
            print(f"  Rate limited on {what}; retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
    raise GmailRateLimited(what)


def search_threads(service, query: str, max_results: int = 5) -> list[dict]:
    """Search Gmail threads matching query. Returns list of thread metadata."""
    try:
        result = _execute(
            lambda: service.users().threads().list(
                userId="me", q=query, maxResults=max_results
            ),
            f"search {query!r}",
        )
        return result.get("threads", [])
    except GmailRateLimited:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"  Gmail search error: {e}")
        return []


def get_thread_messages(service, thread_id: str) -> list[dict]:
    """Get messages from a thread with plain text bodies."""
    try:
        thread = _execute(
            lambda: service.users().threads().get(
                userId="me", id=thread_id, format="full"
            ),
            f"thread {thread_id}",
        )
        messages = thread.get("messages", [])
        result = []
        for msg in messages:
            parsed = _parse_message(msg)
            if parsed:
                result.append(parsed)
        return result
    except GmailRateLimited:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"  Gmail thread read error: {e}")
        return []


def _parse_message(msg: dict) -> dict | None:
    """Extract useful fields from a Gmail message."""
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

    # Get plain text body
    body = _extract_body(msg.get("payload", {}))

    # Parse date
    date_str = headers.get("date", "")
    date_parsed = ""
    if date_str:
        # Date headers are RFC 5322; let the stdlib parser handle the variants
        # (offsets, obsolete zone names like EST, missing day-of-week).
        try:
            parsed = parsedate_to_datetime(date_str.strip())
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                # A "-0000" offset means UTC with the sender's zone unknown.
                parsed = parsed.replace(tzinfo=timezone.utc)
            # Report the date on the user's calendar, matching the date_added /
            # date_applied columns this gets compared against in the sheet.
            date_parsed = parsed.astimezone().strftime("%Y-%m-%d")
        if not date_parsed:
            # Fallback: use internalDate (epoch milliseconds, UTC)
            internal = msg.get("internalDate")
            if internal:
                dt = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
                date_parsed = dt.astimezone().strftime("%Y-%m-%d")

    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": date_parsed,
        "date_raw": date_str,
        "snippet": msg.get("snippet", ""),
        "body": body[:3000],  # Truncate long bodies
    }


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text body from message payload."""
    import base64

    mime_type = payload.get("mimeType", "")

    # Direct text/plain
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Multipart: recurse into parts
    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Nested multipart
    for part in parts:
        body = _extract_body(part)
        if body:
            return body

    # Fallback: try HTML and strip tags
    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html)

    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                return re.sub(r"<[^>]+>", " ", html)

    return ""


def _count_matches(patterns: list[str], text: str, *, negated_ok: bool) -> int:
    """Count patterns that match in a way that actually asserts something.

    A conditional prefix always disqualifies a match ("if you are not selected"
    describes a possible future). A negating prefix disqualifies only interview
    phrases — "decided not to move forward" is an invitation turned inside out,
    while a rejection phrase sitting after "unfortunately" is just a rejection.
    Callers pass negated_ok=True for rejection patterns.
    """
    total = 0
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            prefix = text[max(0, match.start() - 70):match.start()]
            if CONDITIONAL_RE.search(prefix):
                continue
            if not negated_ok and NEGATION_RE.search(prefix):
                continue
            total += 1
            break
    return total


# Marketing mail is typed in a word processor, so apostrophes and dashes arrive
# as curly Unicode. "won’t" does not match a pattern written with a straight
# quote, which silently cost a real rejection its match.
_PUNCTUATION = str.maketrans({
    "‘": "'", "’": "'", "ʼ": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-", "‑": "-",
    " ": " ", "​": "",
})


def normalize_text(text: str) -> str:
    """Lowercase and fold typographic punctuation to its ASCII equivalent."""
    return text.translate(_PUNCTUATION).lower()


def classify_email(subject: str, body: str, sender: str) -> str:
    """Classify an email as 'rejection', 'interview', or 'neutral'."""
    text = normalize_text(f"{subject} {body}")

    neutral_score = sum(1 for p in NEUTRAL_PATTERNS if re.search(p, text, re.IGNORECASE))
    strong_reject = _count_matches(STRONG_REJECTION_PATTERNS, text, negated_ok=True)
    weak_reject = _count_matches(WEAK_REJECTION_PATTERNS, text, negated_ok=True)
    strong_interview = _count_matches(STRONG_INTERVIEW_PATTERNS, text, negated_ok=False)
    weak_interview = _count_matches(WEAK_INTERVIEW_PATTERNS, text, negated_ok=False)

    # One hedge alone is noise; two together read as a real "no".
    rejected = strong_reject > 0 or weak_reject >= 2

    # A rejection outranks an interview signal when both appear: rejection
    # letters borrow invitation phrasing ("we'd be glad to connect on other roles").
    if rejected and strong_reject >= strong_interview:
        return "rejection"

    if strong_interview > 0:
        return "interview"

    if rejected:
        return "rejection"

    # Only process boilerplate matched. In an auto-confirmation that walks
    # through the funnel, that says nothing about this candidate.
    if weak_interview > 0 and neutral_score == 0:
        return "interview"

    return "neutral"


def scan_company(
    service,
    company_name: str,
    domain: str = "",
    max_threads: int = 3,
) -> dict:
    """Scan Gmail for a single company. Returns classification result.

    Returns dict with:
        company: str
        status: 'rejection' | 'interview' | 'neutral' | 'no_results'
        email_date: str (YYYY-MM-DD)
        details: str (subject line or summary)
        thread_count: int
    """
    result = {
        "company": company_name,
        "status": "no_results",
        "email_date": "",
        "details": "",
        "thread_count": 0,
    }

    # Search by subject first
    query = f"subject:({company_name}) newer_than:90d"
    threads = search_threads(service, query, max_results=max_threads)

    # Fallback: search by domain if available
    if not threads and domain and "." in domain:
        query = f"from:(*@{domain}) newer_than:90d"
        threads = search_threads(service, query, max_results=max_threads)

    if not threads:
        return result

    result["thread_count"] = len(threads)

    # Read and classify every message, then let the most recent decisive one
    # win. A rejection that lands after an interview invitation is the current
    # state of the application; the old code pinned "interview" permanently.
    decisive: list[tuple[str, str, str]] = []  # (date, status, subject)
    fallback_date = ""
    fallback_details = ""

    for thread_meta in threads[:max_threads]:
        messages = get_thread_messages(service, thread_meta["id"])
        for msg in messages:
            classification = classify_email(
                msg.get("subject", ""),
                msg.get("body", ""),
                msg.get("from", ""),
            )
            if classification in ("rejection", "interview"):
                decisive.append(
                    (msg.get("date", ""), classification, msg.get("subject", ""))
                )
            elif not fallback_date:
                fallback_date = msg.get("date", "")
                fallback_details = msg.get("subject", "")

    if decisive:
        # Dates are YYYY-MM-DD, so lexical order is chronological. On the same
        # date a rejection outranks an interview: "rejection" > "interview".
        best_date, best_status, best_details = max(decisive, key=lambda d: (d[0], d[1]))
    else:
        best_status = "neutral"
        best_date = fallback_date
        best_details = fallback_details

    result["status"] = best_status
    result["email_date"] = best_date
    result["details"] = best_details
    return result


def _now_stamp() -> str:
    """Current local time as YYYY-MM-DD HH:MM:SS.

    Timezone-aware so the scan log reads in the user's wall clock; these
    timestamps are only ever written for humans, never parsed back.
    """
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def load_scan_log() -> dict:
    """Load previous scan results to avoid re-scanning."""
    if GMAIL_SCAN_LOG.exists():
        return json.loads(GMAIL_SCAN_LOG.read_text(encoding="utf-8"))
    return {"scanned": {}, "last_scan": ""}


def save_scan_log(log: dict) -> None:
    """Save scan log."""
    log["last_scan"] = _now_stamp()
    GMAIL_SCAN_LOG.parent.mkdir(parents=True, exist_ok=True)
    GMAIL_SCAN_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")


def run_full_scan(dry_run: bool = False) -> dict:
    """Run Gmail scan on all 'awaiting response' companies.

    Two-phase approach to avoid Sheets API rate limits:
      Phase 1: Scan all Gmail (no Sheets API calls)
      Phase 2: Batch-move rows using minimal Sheets reads

    Args:
        dry_run: If True, classify but don't move rows.

    Returns summary dict with counts and per-company results.
    """
    import time

    print("Connecting to Gmail...")
    service = get_gmail_service()

    print("Connecting to Google Sheets...")
    from sheets import connect, group_rows_by_company, read_tab
    spreadsheet = connect()
    rows = read_tab(spreadsheet, "awaiting response")

    # Group rows by cleaned company name
    company_groups = group_rows_by_company(rows)

    print(f"Scanning {len(company_groups)} companies across {len(rows)} rows...\n")

    scan_log = load_scan_log()

    scanned = 0
    rejections = 0
    interviews = 0
    no_results = 0
    neutral = 0
    errors = 0
    rate_limited: list[str] = []
    details_list: list[dict] = []

    # ── Phase 1: Gmail scanning only (no Sheets API) ──
    print("Phase 1: Scanning Gmail...\n")
    actions: list[dict] = []  # {company, status, email_date, details, rows}

    batch_count = 0
    for key, group in sorted(company_groups.items()):
        company = group["company"]
        domain = group["domain"]

        try:
            result = scan_company(service, company, domain)
            scanned += 1
            batch_count += 1

            if result["status"] == "rejection":
                rejections += 1
                print(f"  ❌ {company}: REJECTED — {result['details']}")
                actions.append({
                    "company": company,
                    "target_tab": "rejected",
                    "email_date": result["email_date"],
                    "details": result["details"],
                    "rows": group["rows"],
                })
                details_list.append({
                    "company": company,
                    "action": "rejected",
                    "date": result["email_date"],
                    "subject": result["details"],
                })

            elif result["status"] == "interview":
                interviews += 1
                print(f"  ✅ {company}: INTERVIEW — {result['details']}")
                actions.append({
                    "company": company,
                    "target_tab": "received interview",
                    "email_date": result["email_date"],
                    "details": result["details"],
                    "rows": group["rows"],
                })
                details_list.append({
                    "company": company,
                    "action": "interview",
                    "date": result["email_date"],
                    "subject": result["details"],
                })

            elif result["status"] == "no_results":
                no_results += 1
            else:
                neutral += 1

            scan_log["scanned"][key] = {
                "company": company,
                "status": result["status"],
                "date": result["email_date"],
                "scanned_at": _now_stamp(),
            }

        except GmailRateLimited:
            # Quota never cleared: this company was NOT checked. Recording it
            # separately keeps it out of the "no email found" bucket, which
            # would otherwise read as a clean result.
            rate_limited.append(company)
            print(f"  ⏸  {company}: NOT SCANNED — Gmail quota exhausted")
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  ⚠️  {company}: ERROR — {e}")

        if batch_count % 10 == 0 and batch_count > 0:
            print(f"\n  --- {batch_count}/{len(company_groups)} scanned ---\n")

    save_scan_log(scan_log)

    # ── Phase 2: Batch move rows (rate-limited Sheets writes) ──
    if actions and not dry_run:
        print(f"\nPhase 2: Moving {sum(len(a['rows']) for a in actions)} rows...")

        # Collect all records to move, grouped by target tab
        moves_by_tab: dict[str, list[dict]] = {}
        for action in actions:
            tab = action["target_tab"]
            if tab not in moves_by_tab:
                moves_by_tab[tab] = []
            for row in action["rows"]:
                updated = row.record.model_copy(update={
                    "date_resolved": action["email_date"],
                    "source_details": f"Gmail: {action['details'][:80]}",
                })
                moves_by_tab[tab].append({
                    "row": row,
                    "updated_record": updated,
                })

        # Batch-add to destination tabs first, then batch-delete from source
        from sheets import bulk_add_rows, delete_rows_safe, normalize_url

        for target_tab, move_list in moves_by_tab.items():
            records = [m["updated_record"] for m in move_list]
            try:
                count = bulk_add_rows(spreadsheet, target_tab, records)
                print(f"  Added {count} rows to '{target_tab}'")
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️  Error adding to '{target_tab}': {e}")
                errors += len(records)
                continue

        # Now delete from source tab — read once, delete in reverse row order
        # to avoid row number shifting
        time.sleep(2)  # Brief pause before reading again
        try:
            source_ws = spreadsheet.worksheet("awaiting response")
            # Collect URLs to delete
            normalized_targets = {
                normalize_url(row.record.posting_url)
                for action in actions
                for row in action["rows"]
                if row.record.posting_url.strip()
            }

            # Re-read source tab once to get current row numbers
            current_rows = read_tab(spreadsheet, "awaiting response")
            rows_to_delete = [
                sr.row_number
                for sr in current_rows
                if normalize_url(sr.record.posting_url) in normalized_targets
            ]

            deleted = delete_rows_safe(source_ws, rows_to_delete)
            print(f"  Deleted {deleted} rows from 'awaiting response'")

        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  Error deleting from source: {e}")
            print("  Rows were added to destination but not removed from source.")
            print("  Run manually or re-run scan to clean up.")

    # Print summary
    print(f"\n{'='*50}")
    print("Gmail Scan Complete")
    print(f"{'='*50}")
    print(f"  Companies scanned:  {scanned}")
    print(f"  Rejections found:   {rejections}")
    print(f"  Interviews found:   {interviews}")
    print(f"  No email results:   {no_results}")
    print(f"  Neutral/unchanged:  {neutral}")
    if errors:
        print(f"  Errors:             {errors}")
    if rate_limited:
        print(f"  NOT scanned (quota): {len(rate_limited)}")
        print(f"    {', '.join(rate_limited[:15])}"
              + (" ..." if len(rate_limited) > 15 else ""))
        print("    Re-run the scan later to cover these.")

    if dry_run:
        print("\n  (DRY RUN — no rows moved)")

    return {
        "scanned": scanned,
        "rejections": rejections,
        "interviews": interviews,
        "no_results": no_results,
        "neutral": neutral,
        "errors": errors,
        "rate_limited": rate_limited,
        "details": details_list,
    }
