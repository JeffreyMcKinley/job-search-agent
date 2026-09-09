"""Fetch a job posting over HTTP and pull company + title out of the HTML.

No model interaction and no sheet access — stdlib urllib plus the URL pattern
table in url_parser.py.
"""

from __future__ import annotations

import json


def cmd_fetch_job(url: str) -> None:
    """Fetch a job posting URL and extract company + title from HTML.

    No model interaction — just HTTP fetch + HTML parsing.
    Returns JSON: {success, company, title, source}
    """
    import re
    import urllib.request
    from html.parser import HTMLParser

    from url_parser import extract_company_from_url, title_case_company

    success = False
    fetched = False  # strictly "the HTTP body came back", so callers know
    company = ""     # whether to escalate to the Chrome fallback
    title = ""
    source = ""
    error = ""

    # Try URL pattern extraction first (works without fetching)
    company_slug, ats_type = extract_company_from_url(url)
    if company_slug and ats_type != "unknown":
        company = title_case_company(company_slug)
        source = f"url_pattern ({ats_type})"

    # Fetch the page
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read(200_000).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        # The URL pattern may still have given us a company name, but nothing
        # was actually fetched. success stays False so the caller escalates to
        # the Chrome fallback instead of trusting a title it never read.
        error = str(e)[:200]
        title = "(fetch failed)"
        source += " + fetch_failed"
        print(json.dumps({
            "success": False, "fetched": False, "company": company,
            "title": title, "source": source, "error": error,
        }, indent=2))
        return

    fetched = True  # the body came back; the Chrome fallback is unnecessary

    # Extract <title> tag
    class TitleParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.in_title = False
            self.title = ""
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.lower() == "title":
                self.in_title = True
        def handle_endtag(self, tag: str) -> None:
            if tag.lower() == "title":
                self.in_title = False
        def handle_data(self, data: str) -> None:
            if self.in_title:
                self.title += data

    parser = TitleParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001, S110
        pass

    page_title = parser.title.strip()

    # Also try og:title meta tag
    og_title = ""
    og_match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    )
    if not og_match:
        og_match = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
            html, re.IGNORECASE
        )
    if og_match:
        og_title = og_match.group(1).strip()

    # Use whichever title is more informative
    raw_title = og_title or page_title

    if raw_title:
        # Common patterns: "Title - Company", "Title | Company", "Title at Company"
        # Try splitting
        for sep in [" - ", " | ", " — ", " – ", " at ", " @ "]:
            if sep in raw_title:
                parts = raw_title.split(sep)
                if len(parts) >= 2:
                    # Heuristic: shorter part is usually company
                    # But "at" separator: "Title at Company"
                    if sep in (" at ", " @ "):
                        candidate_title = parts[0].strip()
                        candidate_company = parts[-1].strip()
                    else:
                        # Last part is usually company for " - " and " | "
                        candidate_title = sep.join(parts[:-1]).strip()
                        candidate_company = parts[-1].strip()

                    # Skip if "company" is generic ATS name
                    skip_companies = {"greenhouse", "lever", "ashby", "jobs", "careers",
                                      "apply", "job board", "workable", "smartrecruiters"}
                    if candidate_company.lower() not in skip_companies:
                        if not title:
                            title = candidate_title
                        if not company:
                            company = candidate_company
                    elif not title:
                        title = candidate_title
                    break
        else:
            # No separator found — use whole title
            if not title:
                title = raw_title

        success = bool(company or title)
        if not source:
            source = "html_title"
        elif "fetch_failed" not in source:
            source += " + html_title"

    elif company:
        success = True

    print(json.dumps({
        "success": success, "fetched": fetched, "company": company,
        "title": title, "source": source, "error": error,
    }, indent=2))
