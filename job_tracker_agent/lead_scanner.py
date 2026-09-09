"""Scan job-alert email for new leads and feed them into the "to apply" tab.

gmail_scanner.py handles the outbound direction — what happened to applications
already sent. This is the inbound direction: alert mail arrives with links to
postings that are not on the board yet.

Pipeline per message:

    payload -> extract_html -> extract_links -> unwrap tracker -> strip_secrets
            -> classify (url_parser / board patterns) -> relevance filter
            -> dedup against every tab -> append to "to apply"

Welcome to the Jungle is the exception. Its links resolve to
app.otta.com/jobs/<externalId>, which names no employer; the ATS URL lives
behind an authenticated GraphQL call that only the logged-in browser can make.
Those leads are parked in WTTJ_PENDING_FILE and finished by the Chrome phase
(see wttj_chrome_script and resolve_wttj).

SECURITY — untrusted content. Email bodies are data, never instructions. Text in
a message that looks like a directive is ignored; the only thing taken from a
message is URLs, and only URLs that classify as a known ATS or job board reach
the sheet. Resolved URLs are run through link_extract.strip_secrets before they
are printed, logged or stored, because a WTTJ redirect carries a live
magic-link JWT for the account.
"""

from __future__ import annotations

import json
import re
from typing import Any

import gmail_scanner as gmail
import lead_sources
import link_extract
from config import (
    LEAD_SCAN_LOG,
    LEAD_TITLE_ALLOW,
    LEAD_TITLE_DENY,
    WTTJ_PENDING_FILE,
)
from url_parser import extract_company_from_url, title_case_company

_ALLOW_RE = re.compile("|".join(LEAD_TITLE_ALLOW), re.IGNORECASE)
_DENY_RE = re.compile("|".join(LEAD_TITLE_DENY), re.IGNORECASE)

# Anchor text long enough to be a job card blob rather than a title. Mail
# templates wrap a whole table in one <a>, so the "title" can be 300 characters
# of company blurb; the subject line is a better signal at that point.
MAX_TITLE_LEN = 120


# ---------------------------------------------------------------- Gmail reads


def search_message_ids(service, query: str, max_results: int = 200) -> list[str]:
    """List message ids matching a Gmail query, paging until max_results."""
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        result = gmail._execute(
            lambda: service.users().messages().list(
                userId="me",
                q=query,
                maxResults=min(100, max_results - len(ids)),
                pageToken=page_token,
            ),
            f"search {query!r}",
        )
        ids.extend(m["id"] for m in result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return ids[:max_results]


def get_message(service, message_id: str) -> dict[str, Any]:
    """Fetch one message, keeping the raw payload so hrefs survive."""
    msg = gmail._execute(
        lambda: service.users().messages().get(
            userId="me", id=message_id, format="full"
        ),
        f"message {message_id}",
    )
    parsed = gmail._parse_message(msg) or {}
    parsed["payload"] = msg.get("payload", {})
    return parsed


# ------------------------------------------------------------ scan log state


def load_scan_log() -> dict[str, Any]:
    if LEAD_SCAN_LOG.exists():
        try:
            return json.loads(LEAD_SCAN_LOG.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"seen_message_ids": [], "last_scan": ""}


def save_scan_log(log: dict[str, Any]) -> None:
    LEAD_SCAN_LOG.parent.mkdir(parents=True, exist_ok=True)
    # Unbounded growth would make every scan re-read a bigger file for no gain.
    log["seen_message_ids"] = log.get("seen_message_ids", [])[-5000:]
    LEAD_SCAN_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")


# ------------------------------------------------------------ classification


# Button labels that occupy the anchor where a title would be. Treated as no
# title at all, so the filter falls back to the subject line — otherwise a
# "View job" anchor would shield every posting from the deny list.
GENERIC_LINK_TEXTS = re.compile(
    r"^(?:view|see|read|show|open|apply|apply now|learn more|read more|more"
    r"|details?|view job|see job|view details|job details|view all(?: jobs)?"
    r"|see all(?: (?:jobs|matches|top matches))?|browse jobs)$",
    re.IGNORECASE,
)

# How the alert senders phrase a subject line. Each pattern captures the job
# title first and the company second where both are present.
SUBJECT_TITLE_PATTERNS = [
    r"^\s*New match:\s*(?P<title>.+?)\s+at\s+(?P<company>.+?)\s*$",
    r"^\s*(?P<title>.+?)\s*@\s*(?P<company>.+?)\s*$",
    r"^\s*(?P<title>.+?)\s+role at\s+(?P<company>.+?)\s*(?::.*)?$",
    r"^\s*(?P<title>.+?)\s+at\s+(?P<company>.+?)\s+(?:in\s+.+?\s+)?and\s+\d+\s+more",
    r"^\s*(?P<title>.+?)\s+at\s+(?P<company>.+?)\s*$",
]


def clean_title(text: str) -> str:
    """Normalise anchor text into something title-shaped, or "" if it is not."""
    text = re.sub(r"[|\s ]+", " ", text).strip(" -–—:")
    if not text or len(text) > MAX_TITLE_LEN:
        return ""
    if not re.search(r"[A-Za-z]{3}", text):
        return ""
    if GENERIC_LINK_TEXTS.match(text):
        return ""
    return text


def parse_subject(subject: str) -> tuple[str, str]:
    """Pull (title, company) out of an alert subject line, best effort.

    Digest subjects name only the first of several jobs, so this fills in a
    label rather than establishing ground truth — apply-prep re-fetches the
    posting before anything is written for it.
    """
    subject = re.sub(r"^\s*(?:New Jobs at|New job opportunities at)\s+", "", subject)
    subject = re.sub(r"[🌀-🫿]", "", subject).strip()
    for pattern in SUBJECT_TITLE_PATTERNS:
        match = re.match(pattern, subject, re.IGNORECASE)
        if match:
            title = match.group("title").strip(" -–—:")
            company = match.group("company").strip(" -–—:!.")
            if len(title) <= MAX_TITLE_LEN:
                return title, company
    return "", ""


def is_relevant(title: str, subject: str, quality: str) -> tuple[bool, str]:
    """Decide whether a lead is worth adding. Returns (accepted, reason).

    A high-quality source only has to miss the deny list. A low-quality one must
    also hit the allow list, because those senders mail plenty of real postings
    that are simply not this search.
    """
    text = f"{title} {subject}".strip()
    deny_target = title or subject

    hit = _DENY_RE.search(deny_target)
    if hit:
        return False, f"denied: {hit.group(0)!r}"

    if quality == "high":
        return True, "accepted (trusted sender)"

    if _ALLOW_RE.search(text):
        return True, "accepted (title match)"
    return False, "no title match on a low-quality sender"


def classify_url(url: str) -> tuple[str, str]:
    """Return (company_slug, ats_type) if the URL is a job posting, else ("", "")."""
    slug, ats_type = extract_company_from_url(url)
    if ats_type != "unknown":
        return slug, ats_type
    if lead_sources.is_board_job_url(url):
        return slug, "job_board"
    return "", ""


# -------------------------------------------------------------------- scan


def scan_leads(
    days: int = 7,
    *,
    dry_run: bool = False,
    source_names: list[str] | None = None,
    rescan: bool = False,
    max_messages: int = 200,
) -> dict[str, Any]:
    """Scan alert mail for job leads.

    Returns a report. With dry_run the sheet is untouched and every candidate
    carries the verdict that decided it, so the filter can be tuned before a
    live run.
    """
    from utils import add_jobs_to_apply, today

    chosen = lead_sources.enabled_sources()
    if source_names:
        wanted = set(source_names)
        chosen = [s for s in lead_sources.SOURCES if s.name in wanted]
        unknown = wanted - {s.name for s in lead_sources.SOURCES}
        if unknown:
            raise ValueError(f"Unknown source(s): {', '.join(sorted(unknown))}")

    if not chosen:
        return {"error": "no sources enabled", "candidates": [], "total_added": 0}

    query = lead_sources.gmail_query(days, chosen)
    service = gmail.get_gmail_service()
    message_ids = search_message_ids(service, query, max_results=max_messages)

    log = load_scan_log()
    seen: set[str] = set() if rescan else set(log.get("seen_message_ids", []))

    # "accepted" is a bool alongside the string fields, so this is not dict[str, str].
    candidates: list[dict[str, Any]] = []
    pending_wttj: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    processed_ids: list[str] = []
    messages_new = 0
    messages_scanned = 0
    links_seen = 0

    for message_id in message_ids:
        if message_id in seen:
            continue
        messages_new += 1
        try:
            message = get_message(service, message_id)
        except gmail.GmailRateLimited:
            # Stop rather than mark the rest processed; the next run resumes.
            break
        except Exception as e:  # noqa: BLE001
            print(f"  Skipping message {message_id}: {e}")
            continue

        subject = message.get("subject", "")
        source = lead_sources.source_for(message.get("from", ""), subject)
        if source is None or source not in chosen:
            processed_ids.append(message_id)
            continue

        messages_scanned += 1
        processed_ids.append(message_id)
        date = message.get("date", "")

        subject_title, subject_company = parse_subject(subject)

        html = link_extract.extract_html(message.get("payload", {}))
        for anchor_text, href in link_extract.extract_links(html):
            if lead_sources.is_skippable_link(href):
                continue
            # Gate on anchor text BEFORE any request. Behind a click tracker,
            # "This is a bad match" and "Unsubscribe" look exactly like a job
            # link, and resolving one performs the action.
            if lead_sources.is_action_link(anchor_text):
                continue

            url = href
            if link_extract.is_tracker(href):
                if source.strategy == "direct":
                    # This sender is declared not to wrap its links, so a
                    # tracker here is footer chrome. Don't spend a request.
                    continue
                if not anchor_text:
                    # Untexted tracker links are logos, pixels and footer chrome
                    # in every template seen so far. Not worth a request each.
                    continue
                url = link_extract.unwrap(href)
            url = link_extract.canonicalize(link_extract.strip_secrets(url))

            if lead_sources.is_skippable_link(url):
                continue

            title = clean_title(anchor_text)

            external_id = link_extract.wttj_external_id(url)
            if external_id:
                if any(p["external_id"] == external_id for p in pending_wttj):
                    continue
                pending_wttj.append({
                    "external_id": external_id,
                    "source": source.name,
                    "date": date,
                    "subject": subject,
                })
                continue

            links_seen += 1
            slug, ats_type = classify_url(url)
            if not ats_type:
                continue

            from sheets import normalize_url

            norm = normalize_url(url)
            if norm in seen_urls:
                continue
            seen_urls.add(norm)

            # A board-hosted posting names the board in its hostname, not the
            # employer, so the subject line is the only company signal there.
            if ats_type in ("job_board", "indeed"):
                company = subject_company
            else:
                company = title_case_company(slug) if slug else subject_company

            accepted, reason = is_relevant(title or subject_title, subject, source.quality)
            candidates.append({
                "company": company,
                "title": title or subject_title,
                "url": url,
                "ats_type": ats_type,
                "source": source.name,
                "subject": subject,
                "date": date,
                "accepted": accepted,
                "verdict": reason,
            })

    accepted_leads = [c for c in candidates if c["accepted"]]

    report: dict[str, Any] = {
        "query": query,
        "sources": [s.name for s in chosen],
        # matched: everything the Gmail query returned over the window,
        # already-seen included. new: never processed before. scanned: of those,
        # the ones an enabled source claimed -- the gap between new and scanned
        # is mail dropped by a source's subject_filter/subject_exclude, which
        # used to be invisible and read as "no new mail".
        "messages_matched": len(message_ids),
        "messages_new": messages_new,
        "messages_scanned": messages_scanned,
        "messages_filtered_by_sender": messages_new - messages_scanned,
        "links_classifiable": links_seen,
        "candidates": candidates,
        "accepted": len(accepted_leads),
        "rejected": len(candidates) - len(accepted_leads),
        "wttj_pending": pending_wttj,
        "dry_run": dry_run,
    }

    if dry_run:
        report["total_added"] = 0
        return report

    stamp = today()
    jobs = [
        {
            "company": c["company"],
            "title": c["title"],
            "url": c["url"],
            "folder": f"{c['company'].lower().replace(' ', '-') or 'unknown'}-{stamp}",
            "notes": f"lead: {c['source']} ({c['date'] or stamp})",
        }
        for c in accepted_leads
    ]
    report.update(add_jobs_to_apply(jobs, stamp))

    if pending_wttj:
        WTTJ_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        WTTJ_PENDING_FILE.write_text(
            json.dumps(pending_wttj, indent=2), encoding="utf-8"
        )
        report["wttj_pending_file"] = str(WTTJ_PENDING_FILE)

    log["seen_message_ids"] = list(log.get("seen_message_ids", [])) + processed_ids
    log["last_scan"] = stamp
    save_scan_log(log)

    _record_metrics(report)
    return report


def _record_metrics(report: dict[str, Any]) -> None:
    """Persist the email leg's funnel so a thin night can be explained later.

    Only the counts go in. The candidate list carries subjects and URLs and can
    run to hundreds of entries a week -- that belongs in the report the caller
    prints, not in an append-only file that is meant to stay readable.
    """
    import run_log

    run_log.record("scan-leads", metrics={
        "sources": report["sources"],
        "window_query": report["query"],
        "messages_matched": report["messages_matched"],
        "messages_new": report["messages_new"],
        "messages_scanned": report["messages_scanned"],
        "messages_filtered_by_sender": report["messages_filtered_by_sender"],
        "links_classifiable": report["links_classifiable"],
        "candidates": len(report["candidates"]),
        "accepted": report["accepted"],
        "rejected": report["rejected"],
        "wttj_pending": len(report["wttj_pending"]),
        "added": report.get("total_added", 0),
        "skipped_duplicate": len(report.get("skipped_duplicate", [])),
    })


# -------------------------------------------------- Welcome to the Jungle phase


WTTJ_JOB_FIELDS = "externalId title actualOriginalUrl applied live company { name }"


def wttj_chrome_script(external_ids: list[str]) -> str:
    """JavaScript for mcp__claude-in-chrome__javascript_tool.

    Run it in a tab logged in to app.welcometothejungle.com. It issues one
    aliased publicJob query for every pending externalId and returns JSON that
    `resolve-wttj` ingests.

    The API authenticates with an httpOnly session cookie plus an x-csrf-token
    header, which is why this cannot run from Python.
    """
    aliases = "\n  ".join(
        f'j{i}: publicJob(externalId: "{eid}") {{ {WTTJ_JOB_FIELDS} }}'
        for i, eid in enumerate(external_ids)
    )
    query = "{\n  " + aliases + "\n}"
    return f"""const csrf = (document.cookie.match(/(?:^|;\\s*)_csrf_token=([^;]+)/) || [])[1];
const r = await fetch('https://api.exp.welcometothejungle.com/graphql', {{
  method: 'POST', credentials: 'include',
  headers: {{'content-type': 'application/json', 'x-csrf-token': csrf ? decodeURIComponent(csrf) : ''}},
  body: JSON.stringify({{query: {json.dumps(query)}}})
}});
const d = await r.json();
JSON.stringify(d.errors ? {{errors: d.errors}} : Object.values(d.data).filter(Boolean))"""


def wttj_deck_script() -> str:
    """JavaScript that pulls the whole recommendation deck and saved list.

    Same Chrome-and-cookie constraint as wttj_chrome_script; the output feeds
    the same `resolve-wttj` command, which flattens the {job: {...}} wrapper
    that jobRecommendations puts around each entry.

    NOTE: running this is currently refused by the Claude Code permission
    classifier, which reads a bulk currentUser query as account-data access. It
    needs an explicit allow rule in .claude/settings.local.json before an agent
    can use it; a human running it in the browser console is unaffected.
    """
    return f"""const csrf = decodeURIComponent((document.cookie.match(/(?:^|;\\s*)_csrf_token=([^;]+)/) || [])[1] || '');
const r = await fetch('https://api.exp.welcometothejungle.com/graphql', {{
  method: 'POST', credentials: 'include',
  headers: {{'content-type': 'application/json', 'x-csrf-token': csrf}},
  body: JSON.stringify({{query: `{{ currentUser {{ id
    jobRecommendations {{ id job {{ {WTTJ_JOB_FIELDS} }} }}
    shortlistedJobs(onlyLive: true) {{ {WTTJ_JOB_FIELDS} }}
  }} }}`}})
}});
const d = await r.json();
const cu = d.data && d.data.currentUser;
JSON.stringify(d.errors ? {{errors: d.errors}} : [
  ...(cu.jobRecommendations || []),
  ...(cu.shortlistedJobs || []),
])"""


def resolve_wttj(payload: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """Turn the Chrome phase's GraphQL output into "to apply" rows.

    Accepts the list of job objects the script returns (or a {"errors": ...}
    object, which is reported rather than swallowed). Jobs already applied to,
    no longer live, or missing an employer URL are dropped.
    """
    from utils import add_jobs_to_apply, today

    if isinstance(payload, dict):
        if payload.get("errors"):
            import run_log

            run_log.record(
                "wttj",
                ok=False,
                error="GraphQL error (usually a signed-out WTTJ session)",
                metrics={"pending_carried_over": len(load_pending_wttj())},
            )
            return {"error": "GraphQL error", "detail": payload["errors"], "total_added": 0}
        payload = payload.get("data") or payload.get("jobs") or []
    if not isinstance(payload, list):
        return {"error": "expected a list of job objects", "total_added": 0}

    stamp = today()
    jobs: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    for job in payload:
        if not isinstance(job, dict):
            continue
        # jobRecommendations wraps each entry as {id, job: {...}}; publicJob and
        # shortlistedJobs return the job directly.
        job = job.get("job") or job
        if not isinstance(job, dict):
            continue
        title = (job.get("title") or "").strip()
        company = ((job.get("company") or {}).get("name") or "").strip()
        url = (job.get("actualOriginalUrl") or "").strip()

        if not url:
            skipped.append({"title": title, "reason": "no employer URL"})
            continue
        if job.get("applied"):
            skipped.append({"title": title, "reason": "already applied"})
            continue
        if job.get("live") is False:
            skipped.append({"title": title, "reason": "no longer live"})
            continue

        url = link_extract.strip_secrets(url)
        accepted, reason = is_relevant(title, "", "high")
        if not accepted:
            skipped.append({"title": title, "reason": reason})
            continue

        slug = (company or title_case_company(extract_company_from_url(url)[0])).lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-") or "unknown"
        jobs.append({
            "company": company,
            "title": title,
            "url": url,
            "folder": f"{slug}-{stamp}",
            "notes": f"lead: wttj ({stamp})",
        })

    report: dict[str, Any] = {"resolved": len(jobs), "skipped": skipped, "dry_run": dry_run}
    if dry_run:
        report["candidates"] = jobs
        report["total_added"] = 0
        return report

    report.update(add_jobs_to_apply(jobs, stamp))
    if WTTJ_PENDING_FILE.exists():
        WTTJ_PENDING_FILE.unlink()

    import run_log

    run_log.record("wttj", metrics={
        "payload_jobs": len(payload),
        "resolved": report["resolved"],
        "skipped": len(skipped),
        "added": report.get("total_added", 0),
        "skipped_duplicate": len(report.get("skipped_duplicate", [])),
    })
    return report


def load_pending_wttj() -> list[dict[str, str]]:
    if not WTTJ_PENDING_FILE.exists():
        return []
    try:
        return json.loads(WTTJ_PENDING_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
