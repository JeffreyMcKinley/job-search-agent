"""Extract company name and job title from ATS job URLs."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# ATS URL patterns: (regex_pattern, company_group_index)
ATS_PATTERNS: list[tuple[str, str]] = [
    # Greenhouse: boards.greenhouse.io/{company}/jobs/{id}
    (r"boards\.greenhouse\.io/([^/]+)", "greenhouse"),
    # Greenhouse EU: job-boards.eu.greenhouse.io/
    (r"job-boards\.eu\.greenhouse\.io/([^/]+)", "greenhouse"),
    # MyGreenhouse job alerts: my.greenhouse.io/jobs/{company}/{id}
    (r"my\.greenhouse\.io/jobs/([^/]+)/", "greenhouse"),
    # Lever: jobs.lever.co/{company}/{id}
    (r"jobs\.lever\.co/([^/]+)", "lever"),
    # Ashby: jobs.ashbyhq.com/{company}/{id}
    (r"jobs\.ashbyhq\.com/([^/]+)", "ashby"),
    # Workday: {company}.myworkdayjobs.com or {company}.wd{n}.myworkdayjobs.com
    (r"(?:^|[/.])([a-z0-9-]+)\.(?:wd\d+\.)?myworkdayjobs\.com", "workday"),
    # SmartRecruiters: jobs.smartrecruiters.com/{company}/{id}
    (r"jobs\.smartrecruiters\.com/([^/]+)", "smartrecruiters"),
    # Workable: apply.workable.com/([^/]+)
    (r"apply\.workable\.com/([^/]+)", "workable"),
    # Breezy HR: {company}.breezy.hr
    (r"(?:^|[/.])([a-z0-9-]+)\.breezy\.hr", "breezy"),
    # iCIMS: careers-{company}.icims.com
    (r"careers-([^.]+)\.icims\.com", "icims"),
    # Gem: jobs.gem.com/{company}
    (r"jobs\.gem\.com/([^/]+)", "gem"),
    # Dover: app.dover.com/apply/{company}
    (r"app\.dover\.com/apply/([^/]+)", "dover"),
    # Rippling: ats.rippling.com/{company}
    (r"ats\.rippling\.com/([^/]+)", "rippling"),
    # Recruitee: {company}.recruitee.com
    (r"(?:^|[/.])([a-z0-9-]+)\.recruitee\.com", "recruitee"),
    # Personio: {company}.jobs.personio.de/job/{id}
    (r"(?:^|[/.])([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", "personio"),
    # Paylocity: recruiting.paylocity.com/recruiting/jobs/.../{company}
    (r"recruiting\.paylocity\.com", "paylocity"),
    # Oracle Cloud recruiting, as used by American Express
    (r"careers\.americanexpress\.com", "oracle_cx"),
    # Charles Schwab's own board
    (r"(schwabjobs)\.com", "schwab"),
    # Indeed-hosted posting
    (r"indeed\.com/viewjob", "indeed"),
    # Welcome to the Jungle / Otta. A marker, not a destination: these carry no
    # employer URL, so lead_scanner resolves them through the authenticated
    # GraphQL call before anything reaches the sheet.
    (r"app\.(?:otta\.com|welcometothejungle\.com)/jobs/", "wttj"),
    # hiring.cafe links (redirect to actual ATS)
    (r"hiring\.cafe", "hiring_cafe"),
    # Greenhouse embedded on a company's own careers page: ?gh_jid={id}.
    # Last, so a real greenhouse.io host matches its own pattern first.
    (r"[?&]gh_jid=", "greenhouse"),
]


def slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _slug_from_domain(url: str) -> str:
    """Best-effort company slug from a hostname, minus the usual careers prefixes."""
    hostname = urlparse(url).hostname or ""
    for prefix in ["www.", "careers.", "jobs.", "apply.", "hiring.", "recruiting."]:
        hostname = hostname.removeprefix(prefix)
    return slugify(hostname.split(".")[0]) if hostname else ""


def extract_company_from_url(url: str) -> tuple[str, str]:
    """Extract (company_slug, ats_type) from a job URL.

    Returns:
        Tuple of (company_slug, ats_type). company_slug is best-effort
        extracted from URL patterns. ats_type is the detected ATS platform.
    """
    for pattern, ats_type in ATS_PATTERNS:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            company_slug = match.group(1) if match.lastindex else ""
            # Clean up the slug
            company_slug = slugify(company_slug)
            if company_slug:
                return company_slug, ats_type
            # Patterns that identify the ATS without capturing a company —
            # a ?gh_jid= embed on the company's own site, or a single-tenant
            # board — still name the company in their hostname.
            return _slug_from_domain(url), ats_type

    # Fallback: try to extract from domain
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    # Remove common prefixes
    for prefix in ["www.", "careers.", "jobs.", "apply.", "hiring."]:
        hostname = hostname.removeprefix(prefix)

    # Take first part of domain
    company_slug = hostname.split(".")[0] if hostname else "unknown"
    return slugify(company_slug), "unknown"


def extract_company_domain(url: str) -> str:
    """Extract the likely company email domain from a job URL.

    Used for Gmail scanning — maps ATS URLs to company domains.
    """
    company_slug, ats_type = extract_company_from_url(url)

    # For ATS-hosted URLs, the company slug IS the company
    # but we need to guess their email domain
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    # If it's a company's own domain (not an ATS)
    if ats_type == "unknown":
        # Remove www/careers/jobs prefix
        for prefix in ["www.", "careers.", "jobs.", "apply.", "hiring."]:
            hostname = hostname.removeprefix(prefix)
        return hostname

    # For ATS-hosted, return the company slug (best we can do without more data)
    return company_slug


def title_case_company(slug: str) -> str:
    """Convert a company slug back to title case for display.

    Examples:
        'neo-tax' -> 'Neo Tax'
        'acorns' -> 'Acorns'
        'american-express' -> 'American Express'
    """
    return " ".join(word.capitalize() for word in slug.split("-"))
