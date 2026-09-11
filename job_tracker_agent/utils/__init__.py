"""Everything main.py does, split by responsibility.

main.py holds only argument dispatch; each command lives in the module that
matches what it talks to:

    dates             — the shared today() helper
    sheet_commands    — pure Google Sheets reads and writes
    folder_commands   — ~/.proficiently/jobs/ reconciled into the sheet
    job_fetch         — HTTP fetch + HTML parse of a posting URL
    pdf               — headless-Chrome rendering and its template checks
    bespoke_commands  — the bespoke materials gate
    prep_runner       — the `claude -p` subprocess, budgets, stream handling
    prep_commands     — prep run/schema/validate/apply
    gmail_commands    — Gmail auth and scanning (outbound: application status)
    lead_commands     — Gmail scanning (inbound: new job leads)
    linkedin_commands — LinkedIn guest-endpoint search, straight to the board

Import from the package, not the modules: `from utils import cmd_prep`.
"""

from __future__ import annotations

from .bespoke_commands import cmd_check_bespoke
from .dates import today
from .folder_commands import (
    add_jobs_to_apply,
    cmd_backfill,
    cmd_prepare_move,
    cmd_scan_folders,
    cmd_search_add,
)
from .gmail_commands import cmd_gmail_auth, cmd_gmail_companies, cmd_scan_gmail
from .job_fetch import cmd_fetch_job
from .lead_commands import (
    cmd_lead_sources,
    cmd_resolve_wttj,
    cmd_scan_leads,
    cmd_wttj_deck_script,
    cmd_wttj_script,
)
from .linkedin_commands import cmd_linkedin_search
from .pdf import (
    CANDIDATE_NAME,
    COVER_CSS_SIGNATURE,
    DEFAULT_ACCENT,
    JOB_HEAD_SIGNATURE,
    RESUME_CSS_SIGNATURE,
    cmd_make_pdf,
    find_chrome,
    verify_pdf,
)
from .prep_commands import (
    _load_batch,
    cmd_prep,
    cmd_prep_apply,
    cmd_prep_schema,
    cmd_prep_validate,
)
from .prep_runner import (
    PREP_IDLE_TIMEOUT,
    PREP_MODEL,
    PREP_RETRY_BACKOFF,
    _console_safe,
    _reconstruct_from_sidecars,
    _report_progress,
    _run_agent,
    _transient_rejection,
)
from .run_commands import cmd_run_log, cmd_run_report
from .sheet_commands import (
    cmd_add_row,
    cmd_all_urls,
    cmd_auth,
    cmd_company_cap,
    cmd_dedup,
    cmd_find_url,
    cmd_list_rows,
    cmd_move_row,
    cmd_setup_sheet,
    cmd_status,
)

__all__ = [
    "CANDIDATE_NAME",
    "COVER_CSS_SIGNATURE",
    "DEFAULT_ACCENT",
    "JOB_HEAD_SIGNATURE",
    "PREP_IDLE_TIMEOUT",
    "PREP_MODEL",
    "PREP_RETRY_BACKOFF",
    "RESUME_CSS_SIGNATURE",
    "_console_safe",
    "_load_batch",
    "_reconstruct_from_sidecars",
    "_report_progress",
    "_run_agent",
    "_transient_rejection",
    "add_jobs_to_apply",
    "cmd_add_row",
    "cmd_all_urls",
    "cmd_auth",
    "cmd_backfill",
    "cmd_check_bespoke",
    "cmd_company_cap",
    "cmd_dedup",
    "cmd_fetch_job",
    "cmd_find_url",
    "cmd_gmail_auth",
    "cmd_gmail_companies",
    "cmd_lead_sources",
    "cmd_linkedin_search",
    "cmd_list_rows",
    "cmd_make_pdf",
    "cmd_move_row",
    "cmd_prep",
    "cmd_prep_apply",
    "cmd_prep_schema",
    "cmd_prep_validate",
    "cmd_prepare_move",
    "cmd_resolve_wttj",
    "cmd_run_log",
    "cmd_run_report",
    "cmd_scan_folders",
    "cmd_scan_gmail",
    "cmd_scan_leads",
    "cmd_search_add",
    "cmd_setup_sheet",
    "cmd_status",
    "cmd_wttj_deck_script",
    "cmd_wttj_script",
    "find_chrome",
    "today",
    "verify_pdf",
]
