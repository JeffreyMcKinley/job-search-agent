"""LinkedIn as a second search source, via the guest endpoint.

hiring.cafe is the only source the search leg had, and it does not index every
employer. LinkedIn does, but the logged-in job search is a poor fit for an
unattended 1am run: it needs the real account's session, its DOM class names
rotate, and heavy scraping on a personal account risks a login challenge on the
one account the whole job hunt depends on.

So this uses the guest endpoint instead:

    /jobs-guest/jobs/api/seeMoreJobPostings/search

It is the fragment LinkedIn serves to logged-out visitors for infinite scroll.
It returns plain HTML cards, needs no cookie, and has been stable for years.
The tradeoff is no salary data and coarser filters, which is fine here, because
every posting is re-fetched by `prep` before anything is written for it.

No sheet access and no model interaction: fetch, parse, filter. The caller
decides what to do with the results.
"""

from __future__ import annotations

import html
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from config import (
    LEAD_COMPANY_DENY,
    LINKEDIN_LOCATION_ALLOW,
    LINKEDIN_MAX_PAGES,
    LINKEDIN_QUERIES,
)

GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

# The page size is LinkedIn's to choose, not ours: the same endpoint has been
# observed returning 10 cards for one query and 25 for another. So `start`
# advances by the number of cards actually returned. Assuming a fixed 25 would
# silently skip 15 postings per page on the 10-card queries.
REQUEST_PAUSE_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 20

_CARD_RE = re.compile(r"<li\b.*?</li>", re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(r'href="(https://[^"]*?/jobs/view/[^"]+)"', re.IGNORECASE)
_TITLE_RE = re.compile(
    r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)<', re.DOTALL | re.IGNORECASE
)
_COMPANY_RE = re.compile(
    r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(?:\s*<a[^>]*>)?(.*?)<',
    re.DOTALL | re.IGNORECASE,
)
_LOCATION_RE = re.compile(
    r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)<',
    re.DOTALL | re.IGNORECASE,
)

# An empty list must not become re.compile(""), which matches every string.
# For the deny list that would bar every company on the board; for the allow
# list it happens to mean "allow everything", which is right, but only by
# accident. Both are None when unconfigured, and the predicates check for it.
def _any_of(patterns: list[str]) -> re.Pattern[str] | None:
    return re.compile("|".join(patterns), re.IGNORECASE) if patterns else None


_COMPANY_DENY_RE = _any_of(LEAD_COMPANY_DENY)
_LOCATION_ALLOW_RE = _any_of(LINKEDIN_LOCATION_ALLOW)


@dataclass
class Posting:
    """One card off the guest endpoint, already cleaned."""

    title: str
    company: str
    location: str
    url: str
    query: str = ""


@dataclass
class SearchReport:
    """What a full run saw, in the shape run-log wants.

    zero_result_queries is the number that matters: a query whose filters
    over-AND and return nothing looks exactly like a night with no new postings
    unless it is counted separately.
    """

    postings: list[Posting] = field(default_factory=list)
    queries: int = 0
    zero_result_queries: int = 0
    postings_scanned: int = 0
    rejected_title: int = 0
    rejected_location: int = 0
    rejected_company: int = 0
    duplicate_url: int = 0
    errors: list[str] = field(default_factory=list)

    def metrics(self) -> dict[str, int]:
        return {
            "queries": self.queries,
            "zero_result_queries": self.zero_result_queries,
            "postings_scanned": self.postings_scanned,
            "kept": len(self.postings),
            "rejected_title": self.rejected_title,
            "rejected_location": self.rejected_location,
            "rejected_company": self.rejected_company,
            "duplicate_url": self.duplicate_url,
            "errors": len(self.errors),
        }


def clean_job_url(url: str) -> str:
    """Drop LinkedIn's per-impression tracking params.

    A card link carries refId, trackingId and position, which differ on every
    fetch. Left on, the same posting would look new to the sheet's URL dedup
    every single night.
    """
    split = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, "", ""))


def _text(raw: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", raw)).strip()


def parse_cards(fragment: str, query: str = "") -> list[Posting]:
    """Pull postings out of one guest-endpoint HTML fragment.

    A card missing a title, company or link is skipped rather than guessed at.
    If LinkedIn changes its class names this returns nothing, which surfaces as
    zero_result_queries in the run log instead of as silent success.
    """
    postings: list[Posting] = []
    for card in _CARD_RE.findall(fragment):
        link = _LINK_RE.search(card)
        title = _TITLE_RE.search(card)
        company = _COMPANY_RE.search(card)
        if not (link and title and company):
            continue
        location = _LOCATION_RE.search(card)
        postings.append(
            Posting(
                title=_text(title.group(1)),
                company=_text(company.group(1)),
                location=_text(location.group(1)) if location else "",
                url=clean_job_url(html.unescape(link.group(1))),
                query=query,
            )
        )
    return postings


def fetch_page(params: dict[str, str], start: int) -> str:
    """One page of the guest endpoint. Raises on transport failure."""
    query = dict(params)
    query["start"] = str(start)
    url = f"{GUEST_SEARCH_URL}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return resp.read(1_000_000).decode("utf-8", errors="replace")


def is_accepted_location(location: str) -> bool:
    """True for a place the configured location allowlist accepts.

    The guest endpoint ignores its own remote facet, so this is the only thing
    standing between the board and every posting in the country. An unset
    allowlist accepts everything, which is a choice the config makes, not a
    failure to filter.
    """
    if _LOCATION_ALLOW_RE is None:
        return True
    return bool(location and _LOCATION_ALLOW_RE.search(location.strip()))


def is_denied_company(company: str) -> bool:
    """True for a company barred outright in config.toml.

    The bar is not about fit — a non-compete, or somewhere the candidate simply
    will not work — so it is applied before a posting can reach the board.
    """
    if _COMPANY_DENY_RE is None:
        return False
    return bool(company and _COMPANY_DENY_RE.search(company))


def search(
    queries: list[dict[str, str]] | None = None,
    *,
    max_pages: int = LINKEDIN_MAX_PAGES,
    title_filter: Callable[[str], tuple[bool, str]] | None = None,
) -> SearchReport:
    """Run every configured query and return the surviving postings.

    `title_filter` takes a title and returns (accepted, reason); the caller
    passes lead_scanner.is_relevant so email leads and LinkedIn results are
    judged by exactly one rule set.
    """
    report = SearchReport()
    seen: set[str] = set()

    for spec in queries if queries is not None else LINKEDIN_QUERIES:
        params = {k: v for k, v in spec.items() if k != "label"}
        label = spec.get("label", params.get("keywords", ""))
        report.queries += 1
        found_for_query = 0
        start = 0

        for _page in range(max_pages):
            try:
                fragment = fetch_page(params, start)
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"{label}: {str(e)[:200]}")
                break

            cards = parse_cards(fragment, query=label)
            if not cards:
                break

            found_for_query += len(cards)
            report.postings_scanned += len(cards)

            for posting in cards:
                if posting.url in seen:
                    report.duplicate_url += 1
                    continue
                seen.add(posting.url)

                if is_denied_company(posting.company):
                    report.rejected_company += 1
                    continue

                if not is_accepted_location(posting.location):
                    report.rejected_location += 1
                    continue

                if title_filter is not None:
                    accepted, _reason = title_filter(posting.title)
                    if not accepted:
                        report.rejected_title += 1
                        continue

                report.postings.append(posting)

            start += len(cards)
            time.sleep(REQUEST_PAUSE_SECONDS)

        if found_for_query == 0:
            report.zero_result_queries += 1

    return report
