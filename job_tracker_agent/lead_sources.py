"""Which senders carry job leads, and how each one hides its links.

This is the one file to edit when a new job-alert sender shows up. Everything
else in the lead pipeline is driven by this table.

Scanning "all job-alert mail" cannot mean scanning all mail: a 21-day sample of
this inbox matched ~200 threads, most of them LinkedIn social digests, Glassdoor
content mailers, and genuinely off-target roles. So sources are an allowlist,
each one tagged with a quality that decides how hard lead_scanner filters it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LeadSource:
    """One job-alert sender and the shape of its links.

    strategy:
        direct — hrefs already point at an ATS or company careers page.
        unwrap — hrefs are click-trackers; resolve the redirect first.
        wttj   — unwrap, then hand the externalId to the Chrome GraphQL phase,
                 because the employer URL only exists behind the private API.

    quality:
        high — trusted; a lead passes unless the title hits the deny list.
        low  — noisy; a lead must also hit the title allow list to survive.
    """

    name: str
    sender: str
    strategy: str
    quality: str
    enabled: bool = True
    subject_filter: str = ""
    subject_exclude: str = ""

    def matches(self, from_header: str, subject: str) -> bool:
        """True if a message from this sender is worth extracting links from."""
        if not re.search(self.sender, from_header, re.IGNORECASE):
            return False
        if self.subject_filter and not re.search(self.subject_filter, subject, re.IGNORECASE):
            return False
        if self.subject_exclude and re.search(self.subject_exclude, subject, re.IGNORECASE):
            return False
        return True

    @property
    def gmail_from(self) -> str:
        """The `from:` term for a Gmail query — an address or domain, not a regex."""
        return _GMAIL_FROM[self.name]


# Gmail's `from:` operator takes addresses and domains, not regexes, so each
# source declares its query term separately from its matching pattern.
_GMAIL_FROM = {
    "wttj": "help@welcometothejungle.com",
    "indeed": "match.indeed.com",
    "greenhouse-alerts": "notifications@us.greenhouse-jobs.com",
    "amex": "recruitment.americanexpress.com",
    "honeywell": "honeywellaerospace.com",
    "glassdoor": "glassdoor.com",
    "linkedin": "linkedin.com",
    "germantechjobs": "germantechjobs.de",
}

# LinkedIn and Glassdoor send job digests down the same pipe as social noise and
# content marketing. Excluding by subject is cheaper and more accurate than
# trying to tell the two apart from the link soup.
_SOCIAL_SUBJECTS = (
    r"reshared a post|commented on|viewed your profile|appeared in \d+ search"
    r"|puzzles|crushed this|found your profile|thanks for being|Premium"
    r"|did you hear back|latest industry content|navigate the hiring process"
)

SOURCES: list[LeadSource] = [
    # Proven end to end: SendGrid tracker -> app.otta.com/jobs/<id> -> GraphQL.
    LeadSource(
        name="wttj",
        sender=r"help@welcometothejungle\.com",
        strategy="wttj",
        quality="high",
        subject_filter=r"^\s*New match:",
    ),
    LeadSource(
        name="indeed",
        sender=r"@match\.indeed\.com",
        strategy="unwrap",
        quality="high",
    ),
    LeadSource(
        name="greenhouse-alerts",
        sender=r"notifications@us\.greenhouse-jobs\.com",
        strategy="direct",
        quality="high",
        subject_filter=r"^\s*New Jobs at ",
    ),
    LeadSource(
        name="amex",
        sender=r"@recruitment\.americanexpress\.com",
        strategy="direct",
        quality="low",
    ),
    LeadSource(
        name="honeywell",
        sender=r"@honeywellaerospace\.com",
        strategy="direct",
        quality="low",
    ),
    # Off by default. Glassdoor digests are real postings but hostile to parse:
    # ~90 links per 10 days, the company and title fused into one blob
    # ("Infosys 3.6 <star>Java Backend DeveloperDenver, CO$94K"), and every
    # glassdoor.com URL answers 403 to a non-browser fetch — so each lead would
    # cost apply-prep a Chrome fallback. Canonicalisation and the action-link
    # guards are in place, so enabling this is a one-line change once the
    # blob-parsing is worth writing.
    LeadSource(
        name="glassdoor",
        sender=r"@glassdoor\.com",
        strategy="unwrap",
        quality="low",
        enabled=False,
        subject_exclude=_SOCIAL_SUBJECTS,
    ),
    LeadSource(
        name="linkedin",
        sender=r"@(?:em\.)?linkedin\.com",
        strategy="unwrap",
        quality="low",
        subject_exclude=_SOCIAL_SUBJECTS,
    ),
    # Germany-only listings; off-target for this search, kept for discoverability.
    LeadSource(
        name="germantechjobs",
        sender=r"@germantechjobs\.de",
        strategy="unwrap",
        quality="low",
        enabled=False,
    ),
]

# Job-page URLs on a job board's own domain. url_parser only knows ATS hosts, so
# these keep board-hosted postings (which are still applyable) from being
# discarded as "not a job link".
BOARD_JOB_PATTERNS = [
    r"indeed\.com/(?:viewjob|job/)",
    r"linkedin\.com/jobs/view/",
    r"glassdoor\.com/(?:job-listing|partner/jobListing)",
    r"germantechjobs\.de/job/",
]

# Anchor text on links that DO something when opened: rate the match, mute the
# sender, change notification frequency, unsubscribe. A click tracker hides its
# destination behind opaque base64, so SKIP_LINK_PATTERNS cannot see what it
# points at — by the time the URL is readable the request has already been made.
# Anchor text is the only signal available before the network call, so it is
# what gates it. Checked before any request; keep it broad.
ACTION_TEXT_PATTERNS = [
    r"^\s*(?:yes|no|maybe|never|daily|weekly|monthly)\s*$",
    r"unsubscribe|opt[- ]?out|remove me",
    r"not interested|bad match|wrong match|dismiss|hide this",
    r"pause|stop (?:these|receiving)|mute|snooze|block",
    r"preferences|frequency|notification settings|manage email",
    r"edit profile|my profile|view profile|account settings",
    r"report|feedback|rate this|help ?cent(?:er|re)|contact us",
    r"apply now with|one[- ]click apply|quick apply",
    r"^\s*create\s*$|create (?:a )?(?:job )?alert|save this search",
]

_ACTION_RE = re.compile("|".join(ACTION_TEXT_PATTERNS), re.IGNORECASE)


def is_action_link(anchor_text: str) -> bool:
    """True if opening this link would change something on the sender's side.

    Called before a tracker link is resolved. Erring toward True only costs a
    lead; erring toward False sends a "not interested" on the user's behalf.
    """
    return bool(_ACTION_RE.search(anchor_text or ""))


# Links every mail template carries that are never a job.
SKIP_LINK_PATTERNS = [
    r"/unsubscribe",
    r"/settings",
    r"/preferences",
    r"/privacy",
    r"/terms",
    r"/help",
    r"/support",
    r"/legal",
    r"/blog",
    r"/salaries",
    r"/reviews",
    # "Create a job alert" and one-click-apply endpoints. Opening either acts
    # on the account rather than showing a posting.
    r"rjtRedirect|oneClick=true|/job-listing/api/",
    r"apps\.apple\.com|play\.google\.com",
    r"(?:twitter|facebook|instagram|youtube)\.com",
    r"\.(?:png|jpe?g|gif|svg|css|js)(?:$|\?)",
]

_BOARD_RE = re.compile("|".join(BOARD_JOB_PATTERNS), re.IGNORECASE)
_SKIP_RE = re.compile("|".join(SKIP_LINK_PATTERNS), re.IGNORECASE)


def enabled_sources() -> list[LeadSource]:
    return [s for s in SOURCES if s.enabled]


def source_for(from_header: str, subject: str) -> LeadSource | None:
    """The first enabled source that claims this message, or None."""
    for source in enabled_sources():
        if source.matches(from_header, subject):
            return source
    return None


def is_board_job_url(url: str) -> bool:
    """True if the URL is a job page on a job board's own domain."""
    return bool(_BOARD_RE.search(url))


def is_skippable_link(url: str) -> bool:
    """True for boilerplate links (unsubscribe, settings, assets, social)."""
    return bool(_SKIP_RE.search(url))


def gmail_query(days: int, sources: list[LeadSource] | None = None) -> str:
    """Build the Gmail search query covering the given sources."""
    chosen = sources if sources is not None else enabled_sources()
    if not chosen:
        return ""
    terms = " OR ".join(f"from:{s.gmail_from}" for s in chosen)
    return f"({terms}) newer_than:{days}d"
