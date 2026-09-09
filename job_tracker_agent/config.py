"""Configuration for the job tracker agent.

Everything that is about a particular person — which board to write to, which
roles to look for, what name goes on a resume — lives in TOML under
``~/.jobtrackeragent/`` rather than in this file. What stays here is the shape
of the board and the defaults that are true for anyone.

Two files are read, both optional at import time so that commands which never
touch the board (``prep-schema``, ``make-pdf``) still run on a fresh checkout:

    ~/.jobtrackeragent/config.toml    board id, data dir, search filters
    ~/.jobtrackeragent/profile.toml   candidate identity, master resume

Set ``JOB_TRACKER_HOME`` to point both somewhere else.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- user config

USER_CONFIG_DIR = Path(
    os.getenv("JOB_TRACKER_HOME") or (Path.home() / ".jobtrackeragent")
).expanduser()

CONFIG_FILE = USER_CONFIG_DIR / "config.toml"
PROFILE_FILE = USER_CONFIG_DIR / "profile.toml"


def _load(path: Path) -> dict[str, Any]:
    """Parse a TOML file, or return {} if it is not there.

    A missing file is normal on a fresh install. A malformed one is not, so a
    parse error is re-raised with the path in the message — tomllib's own error
    names only a line number.
    """
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"ERROR: {path} is not valid TOML: {exc}") from exc


_CONFIG = _load(CONFIG_FILE)
_PROFILE = _load(PROFILE_FILE)

_BOARD = _CONFIG.get("board", {})
_PATHS = _CONFIG.get("paths", {})
_SEARCH = _CONFIG.get("search", {})
_CANDIDATE = _PROFILE.get("candidate", {})
_RESUME = _PROFILE.get("resume", {})


def _path(value: str) -> Path:
    return Path(value).expanduser()


# ------------------------------------------------------------------ the board

BOARD_BACKEND = _BOARD.get("backend", "google_sheets")

# Deliberately not defaulted. An empty id fails in sheets.connect() with a
# message naming the file to write, which beats silently opening a spreadsheet
# that belongs to whoever wrote this code.
SPREADSHEET_ID = _BOARD.get("spreadsheet_id", "")

# Tab names (order matters for display)
TAB_NAMES = [
    "to apply",
    "ready to apply",
    "awaiting response",
    "rejected",
    "received interview",
    "invalid",
]

# Column headers per tab
BASE_HEADERS = ["Company", "Title", "Posting URL", "Date Added", "Folder Slug", "Notes"]
READY_HEADERS = BASE_HEADERS + ["Resume Path", "Cover Letter Path", "Date Prepared"]
AWAITING_HEADERS = READY_HEADERS + ["Date Applied", "ATS Type"]
FINAL_HEADERS = AWAITING_HEADERS + ["Date Resolved", "Source/Details"]
# Invalid postings: expired, 404, or duplicate. The reason text goes in
# Source/Details and the date in Date Resolved — reusing those two header
# names means JobRecord.to_row/from_row need no new field mapping.
INVALID_HEADERS = BASE_HEADERS + ["Date Resolved", "Source/Details"]

HEADERS_BY_TAB = {
    "to apply": BASE_HEADERS,
    "ready to apply": READY_HEADERS,
    "awaiting response": AWAITING_HEADERS,
    "rejected": FINAL_HEADERS,
    "received interview": FINAL_HEADERS,
    "invalid": INVALID_HEADERS,
}

# ----------------------------------------------------------------------- data

# Job folders, scan logs and prep artifacts. Defaults to the proficiently
# plugin's directory, which is where this data has always lived.
PROFICIENTLY_DIR = _path(_PATHS.get("data_dir", "~/.proficiently"))

# apply-prep drafts every document with /unslop mimic, so the prep agent has to
# read the skill's contract files and run its gate scripts. The skill itself is
# user-level and loads regardless of cwd, but its directory sits outside the
# project, so it is passed to the agent with --add-dir.
UNSLOP_SKILL_DIR = _path(_PATHS.get("unslop_skill_dir", "~/.claude/skills/unslop"))

JOBS_DIR = PROFICIENTLY_DIR / "jobs"
GMAIL_SCAN_LOG = PROFICIENTLY_DIR / "gmail-scan-log.json"

# Inbound job leads scraped out of alert email (see lead_scanner.py).
# LEAD_SCAN_LOG records processed message ids so re-runs stay cheap;
# WTTJ_PENDING_FILE is the handoff to the Chrome GraphQL phase.
LEAD_SCAN_LOG = PROFICIENTLY_DIR / "lead-scan-log.json"
WTTJ_PENDING_FILE = PROFICIENTLY_DIR / "wttj-pending.json"

# One JSON object per stage per nightly run (see run_log.py). The prose log the
# runner captures cannot tell "no new mail" from "auth failed" from "everything
# filtered out"; this can.
RUN_METRICS_LOG = PROFICIENTLY_DIR / "run-metrics.jsonl"

# Per-run artifacts from `main.py prep`: prompt, stream log, result JSON.
PREP_RUNS_DIR = PROFICIENTLY_DIR / "prep-runs"

# ------------------------------------------------------------- search filters

# Relevance filter for inbound leads. A real inbox carries plenty of postings
# that are simply not this search — a Tax Accountant role, an AS400 programmer,
# staffing-agency contract listings. Sources tagged quality="low" in
# lead_sources.py must hit LEAD_TITLE_ALLOW to survive; quality="high" sources
# only have to miss LEAD_TITLE_DENY.
#
# These two ship with defaults because a search with no title filter at all
# returns noise. Override either in config.toml under [search].
_DEFAULT_TITLE_ALLOW = [
    r"\b(?:software|frontend|front-end|backend|back-end|full[- ]?stack)\b",
    r"\bengineer(?:ing)?\b",
    r"\bdeveloper\b",
    r"\bprogrammer\b",
]

_DEFAULT_TITLE_DENY = [
    r"\b(?:intern|internship|apprentice|graduate program)\b",
    r"\b(?:security clearance|ts/sci|polygraph)\b",
]

LEAD_TITLE_ALLOW = _SEARCH.get("title_allow") or _DEFAULT_TITLE_ALLOW
LEAD_TITLE_DENY = _SEARCH.get("title_deny") or _DEFAULT_TITLE_DENY

# Companies barred outright, whatever the role. This is not a fit filter, so it
# is applied before a posting can reach the board rather than during scoring.
# Somebody under a non-compete, or who simply will not work somewhere, puts it
# here. Empty means nothing is barred.
LEAD_COMPANY_DENY = _SEARCH.get("company_deny") or []

# LinkedIn guest-endpoint queries for the search leg (see linkedin_search.py).
# Each entry is passed straight through as query params, minus "label".
#
# Only two params actually do anything on the guest endpoint. Measured, not
# assumed: f_TPR=r86400 returns today's postings only, and `location` narrows
# correctly (a Denver query returns Aurora and Lakewood). The facets that would
# have been most useful are silently ignored — f_WT=2 (remote) and f_E=3,4
# (mid-senior) return byte-identical result sets to no facet at all, and
# location=Remote returns Glasgow and Seoul. So remoteness and seniority are
# filtered by LINKEDIN_LOCATION_ALLOW and the shared title rules instead.
#
# Empty means the LinkedIn leg has nothing to run and reports zero.
LINKEDIN_QUERIES = _SEARCH.get("linkedin_queries") or []

# The accepted locations, as they appear on a search card. A card shows one
# place string and no remote flag, so a nationwide-remote role renders either as
# "Remote"/"United States" or as the hiring office's city. Matching the office
# city means some real remote roles are dropped; not matching it means every
# metro in the country lands on the board. The first is the cheaper mistake,
# because the board is worked by hand afterwards.
#
# Empty means no location filter at all.
LINKEDIN_LOCATION_ALLOW = _SEARCH.get("linkedin_location_allow") or []

# Pages per query, 25 cards each.
LINKEDIN_MAX_PAGES = int(_SEARCH.get("linkedin_max_pages", 2))

# --------------------------------------------------------- candidate identity

CANDIDATE_NAME = _CANDIDATE.get("name", "")

# The identity block in templates/*.html.template. Substituted at render time by
# utils.pdf, not by the drafting agent — the rendered height of this block feeds
# the one-page check, so it has to be real text before the gate runs.
PROFILE_PLACEHOLDERS = {
    "CANDIDATE_NAME": CANDIDATE_NAME,
    "LOCATION": _CANDIDATE.get("location", ""),
    "STREET": _CANDIDATE.get("street", ""),
    "CITY_STATE_ZIP": _CANDIDATE.get("city_state_zip", ""),
    "PHONE": _CANDIDATE.get("phone", ""),
    "EMAIL": _CANDIDATE.get("email", ""),
    "LINKEDIN_URL": _CANDIDATE.get("linkedin_url", ""),
    "LINKEDIN_TEXT": _CANDIDATE.get("linkedin_text", ""),
    "GITHUB_URL": _CANDIDATE.get("github_url", ""),
    "GITHUB_TEXT": _CANDIDATE.get("github_text", ""),
}

# The /unslop voice card every document is drafted with.
VOICE_CARD = _PROFILE.get("voice", {}).get("card", "")

RESUME_DIR = PROFICIENTLY_DIR / "resume"

# The canonical master resume. The resume/ folder may hold several files;
# naming the canonical one makes the choice explicit instead of implicit.
MASTER_RESUME = RESUME_DIR / _RESUME.get("master_filename", "resume.docx")

# Optional. If a master/example cover letter is placed here, check-bespoke will
# compare against it; otherwise cover letters get structural checks only.
MASTER_COVER_LETTER = RESUME_DIR / _RESUME.get(
    "master_cover_letter", "master-cover-letter.md"
)

# ---------------------------------------------------------------- credentials

PROJECT_DIR = Path(__file__).parent


def _credential(name: str) -> Path:
    """Locate a credential file, preferring the user config directory.

    Falls back to the project directory, which is where these lived before they
    were moved out of the tree. The fallback keeps an existing install working
    without a migration step; anything written fresh goes to the new home.
    """
    preferred = USER_CONFIG_DIR / name
    if preferred.exists():
        return preferred
    legacy = PROJECT_DIR / name
    if legacy.exists():
        return legacy
    return preferred


CREDENTIALS_FILE = _credential("credentials.json")
AUTHORIZED_USER_FILE = _credential("authorized_user.json")

# Gmail API (separate token file, readonly scope)
GMAIL_AUTHORIZED_USER_FILE = _credential("gmail_authorized_user.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# --------------------------------------------------------------------- status

# Status classification for backfill
APPLIED_STATUSES = {"submitted", "applied"}
DRAFT_STATUSES = {"draft"}
DEAD_STATUSES = {"expired", "not applied", "invalid", "skipped", "disqualified"}
