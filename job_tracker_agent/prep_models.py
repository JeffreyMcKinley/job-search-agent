"""Wire protocol between `main.py prep` and the /mass-apply prep skill.

Deliberately separate from models.py: that file is the sheet-row domain, this
is an agent-facing contract. They version independently.

Every validator here closes a specific way an agent report can be wrong in a
way that would silently corrupt the spreadsheet. The point is that a bad report
fails loudly, before any sheet write, with an error message the agent can read
and repair (`main.py prep-validate`).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A LinkedIn job permalink. Matching it is what makes a URL "a wrapper" rather
# than a posting: the apply button behind it either opens LinkedIn's own form
# (Easy Apply) or redirects to the employer's ATS (offsite).
LINKEDIN_JOB_RE = re.compile(
    r"^https?://([a-z0-9-]+\.)*linkedin\.com/jobs/(view|collections)/", re.IGNORECASE
)

# cmd_fetch_job uses this as a title sentinel when the HTTP GET failed but the
# URL still matched an ATS pattern. It must never reach the spreadsheet.
FETCH_FAILED_SENTINEL = "(fetch failed)"

# Minimum length for the explanation on an invalid item. Forces a real account
# of what was tried instead of a bare "failed".
MIN_INVALID_NOTES = 40


class PrepStatus(str, Enum):
    PREPARED = "prepared"
    INVALID = "invalid"


class InvalidReason(str, Enum):
    EXPIRED = "expired"
    NOT_FOUND = "not_found"
    FETCH_FAILED = "fetch_failed"
    BLOCKED = "blocked"
    DUPLICATE = "duplicate"
    POOR_FIT = "poor_fit"
    OTHER = "other"


# Reasons that mean the posting itself is dead — these move to the "invalid"
# tab. Everything else is transient: the row stays in "to apply" with the
# diagnostic in Notes so the next prep run retries it.
TERMINAL_REASONS = {
    InvalidReason.EXPIRED,
    InvalidReason.NOT_FOUND,
    InvalidReason.DUPLICATE,
    InvalidReason.POOR_FIT,
}


def _is_plain_str_field(model: type[BaseModel], name: str) -> bool:
    """True if `name` is a field on `model` annotated exactly `str`."""
    field = model.model_fields.get(name)
    return field is not None and field.annotation is str


class PrepItem(BaseModel):
    """One job's outcome from a prep run."""

    model_config = ConfigDict(extra="forbid")

    # The URL exactly as requested. `reconcile` matches on it, so it must come
    # back unchanged even when the posting really lives somewhere else.
    url: str
    # Where the posting actually lives, when `url` was only a wrapper around it
    # -- a LinkedIn offsite listing being the case that matters. Empty means
    # `url` was already the real thing. On a prepared item the sheet row's
    # posting_url is rewritten to this, so the fill phase applies at the source
    # instead of bouncing off a login wall.
    resolved_url: str = ""
    status: PrepStatus
    folder_name: str = ""  # basename under JOBS_DIR, never an absolute path
    company: str = ""
    title: str = ""
    date_prepared: str = ""
    notes: str = ""
    ats: str = ""
    resume_pdf: str = ""  # filename within the folder
    cover_letter_pdf: str = ""
    application_plan: str = ""
    # Free-form on purpose. This is a diagnostic breadcrumb, not a control
    # value -- nothing branches on it and it never reaches the sheet. A closed
    # Literal here failed whole batches over strings like
    # "greenhouse_boards_api" that described the fetch perfectly well.
    fetch_method: str = "none"
    invalid_reason: InvalidReason | None = None

    @property
    def is_terminal(self) -> bool:
        """True if this item belongs in the 'invalid' tab rather than staying put."""
        return (
            self.status is PrepStatus.INVALID
            and self.invalid_reason in TERMINAL_REASONS
        )

    @model_validator(mode="before")
    @classmethod
    def _blank_nulls(cls, data: Any) -> Any:
        """Treat an explicit null as the empty default on the plain str fields.

        Agents use `null` and `""` interchangeably for "nothing here", in the
        envelope and in the per-folder sidecars alike. Rejecting null bought
        nothing and cost a whole 72-job batch once: "" is already the declared
        default on every field this touches. Only fields annotated exactly
        `str` are coerced, so `invalid_reason` keeps None as a real value and a
        null `status` still fails loudly.
        """
        if not isinstance(data, dict):
            return data
        return {
            k: "" if v is None and _is_plain_str_field(cls, k) else v
            for k, v in data.items()
        }

    @model_validator(mode="after")
    def _check(self) -> PrepItem:
        from config import JOBS_DIR

        errs: list[str] = []

        if not self.url.strip():
            errs.append("url is empty")

        if self.resolved_url.strip():
            if not self.resolved_url.startswith(("http://", "https://")):
                errs.append(f"resolved_url {self.resolved_url!r} is not an http(s) URL")
            elif LINKEDIN_JOB_RE.match(self.resolved_url):
                errs.append(
                    f"resolved_url {self.resolved_url!r} is another LinkedIn job link. "
                    "It must be the employer's own posting -- leave it empty for an "
                    "Easy Apply job instead of pointing it back at LinkedIn."
                )

        if self.title.strip() == FETCH_FAILED_SENTINEL:
            errs.append(
                f"title is the {FETCH_FAILED_SENTINEL!r} sentinel — the posting was "
                "never actually fetched; report this as status=invalid instead"
            )

        if self.date_prepared and not DATE_RE.match(self.date_prepared):
            errs.append(f"date_prepared {self.date_prepared!r} is not YYYY-MM-DD")

        if self.folder_name and ("/" in self.folder_name or "\\" in self.folder_name):
            errs.append(
                f"folder_name {self.folder_name!r} must be a bare folder name, not a path"
            )

        if self.status is PrepStatus.PREPARED:
            if not self.folder_name.strip():
                errs.append("status=prepared requires folder_name")
            else:
                folder = JOBS_DIR / self.folder_name
                if not folder.is_dir():
                    errs.append(f"folder does not exist on disk: {folder}")
                else:
                    if not self.application_plan.strip():
                        errs.append("status=prepared requires application_plan")
                    elif not (folder / self.application_plan).is_file():
                        errs.append(
                            f"application_plan not found: {folder / self.application_plan}"
                        )

                    resume = self.resume_pdf.strip()
                    if resume and (folder / resume).is_file():
                        pass
                    elif (folder / "resume.md").is_file():
                        pass
                    else:
                        errs.append(
                            f"no resume found in {folder}: resume_pdf={resume!r} "
                            "missing and no resume.md fallback"
                        )
            if self.invalid_reason is not None:
                errs.append("status=prepared must not set invalid_reason")

        if self.status is PrepStatus.INVALID:
            if self.invalid_reason is None:
                errs.append("status=invalid requires invalid_reason")
            if len(self.notes.strip()) < MIN_INVALID_NOTES:
                errs.append(
                    f"status=invalid requires notes of at least {MIN_INVALID_NOTES} "
                    "characters explaining why and what was attempted; got "
                    f"{len(self.notes.strip())}"
                )

        if errs:
            raise ValueError(f"[{self.url}] " + "; ".join(errs))
        return self


class PrepBatch(BaseModel):
    """The single object a prep run returns."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    generated_at: str
    items: list[PrepItem]

    @model_validator(mode="after")
    def _no_dupes(self) -> PrepBatch:
        from sheets import normalize_url

        seen: set[str] = set()
        for item in self.items:
            key = normalize_url(item.url)
            if key in seen:
                raise ValueError(f"duplicate url in items: {item.url}")
            seen.add(key)
        return self


def reconcile(batch: PrepBatch, requested: list[str]) -> tuple[PrepBatch, list[str]]:
    """Ensure every requested URL has an item; report extras.

    A URL the agent silently dropped becomes an invalid/other item so it stays
    in "to apply" and gets retried, rather than vanishing from the report.
    Returns (batch, errors); a non-empty errors list means do not touch the sheet.
    """
    from sheets import normalize_url

    by_url = {normalize_url(i.url): i for i in batch.items}
    requested_keys = {normalize_url(u): u for u in requested}

    errors = [
        f"result contains a URL that was never requested: {i.url}"
        for k, i in by_url.items()
        if k not in requested_keys
    ]

    for key, original in requested_keys.items():
        if key not in by_url:
            batch.items.append(
                PrepItem(
                    url=original,
                    status=PrepStatus.INVALID,
                    invalid_reason=InvalidReason.OTHER,
                    notes=(
                        "The prep agent produced no report for this URL. It was "
                        "requested but absent from the result file, so nothing is "
                        "known about whether it was attempted."
                    ),
                )
            )
    return batch, errors
