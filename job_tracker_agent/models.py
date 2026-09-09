"""Pydantic models for job tracker data."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class JobState(str, Enum):
    TO_APPLY = "to apply"
    READY = "ready to apply"
    AWAITING = "awaiting response"
    REJECTED = "rejected"
    INTERVIEW = "received interview"
    INVALID = "invalid"
    DEAD = "Dead"  # expired, invalid, skipped — not tracked in sheet


class JobRecord(BaseModel):
    """A job parsed from ~/.proficiently/jobs/ folder."""

    company: str = ""
    title: str = ""
    posting_url: str = ""
    folder_slug: str = ""
    date_added: str = ""
    notes: str = ""
    resume_path: str = ""
    cover_letter_path: str = ""
    date_prepared: str = ""
    date_applied: str = ""
    ats_type: str = ""
    date_resolved: str = ""
    source_details: str = ""
    state: JobState = JobState.TO_APPLY

    def to_row(self, tab: str) -> list[str]:
        """Convert to spreadsheet row for the given tab."""
        from config import HEADERS_BY_TAB

        headers = HEADERS_BY_TAB[tab]
        field_map = {
            "Company": self.company,
            "Title": self.title,
            "Posting URL": self.posting_url,
            "Date Added": self.date_added,
            "Folder Slug": self.folder_slug,
            "Notes": self.notes,
            "Resume Path": self.resume_path,
            "Cover Letter Path": self.cover_letter_path,
            "Date Prepared": self.date_prepared,
            "Date Applied": self.date_applied,
            "ATS Type": self.ats_type,
            "Date Resolved": self.date_resolved,
            "Source/Details": self.source_details,
        }
        return [field_map.get(h, "") for h in headers]

    @classmethod
    def from_row(cls, row: list[str], tab: str) -> JobRecord:
        """Parse a spreadsheet row from the given tab."""
        from config import HEADERS_BY_TAB

        headers = HEADERS_BY_TAB[tab]
        data: dict[str, str] = {}
        for i, header in enumerate(headers):
            data[header] = row[i] if i < len(row) else ""

        return cls(
            company=data.get("Company", ""),
            title=data.get("Title", ""),
            posting_url=data.get("Posting URL", ""),
            date_added=data.get("Date Added", ""),
            folder_slug=data.get("Folder Slug", ""),
            notes=data.get("Notes", ""),
            resume_path=data.get("Resume Path", ""),
            cover_letter_path=data.get("Cover Letter Path", ""),
            date_prepared=data.get("Date Prepared", ""),
            date_applied=data.get("Date Applied", ""),
            ats_type=data.get("ATS Type", ""),
            date_resolved=data.get("Date Resolved", ""),
            source_details=data.get("Source/Details", ""),
            state=JobState(tab),
        )


class SheetRow(BaseModel):
    """A row with its position in the spreadsheet."""

    row_number: int
    tab: str
    record: JobRecord
