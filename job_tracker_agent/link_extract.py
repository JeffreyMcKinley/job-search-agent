"""Pull job links out of alert emails: extract hrefs, unwrap trackers, strip secrets.

gmail_scanner._extract_body flattens HTML with re.sub(r"<[^>]+>", " ", html),
which throws every href away before anything can read it. This module keeps the
raw HTML part instead and walks it with html.parser, the same stdlib-only
approach utils/job_fetch.py uses.

Alert emails route every link through a click tracker, so a raw href is useless
until it is resolved. Resolving one registers a click event with the sender —
unavoidable, and the same event a human reading the mail would generate.

SECURITY: resolved URLs can carry credentials. A Welcome to the Jungle tracker
redirects to app.otta.com/jobs/<id>?token=<JWT>, and that JWT is a live
magic-link session for the account. Call strip_secrets() before a URL reaches a
log line, a print, the sheet, or a subprocess argument.
"""

from __future__ import annotations

import base64
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# Hosts that serve click-tracking redirects rather than content. Matched as a
# suffix against the hostname, so "ct.sendgrid.net" covers u9255466.ct.sendgrid.net.
TRACKER_HOSTS = (
    "ct.sendgrid.net",
    "sendgrid.net",
    "click.linkedin.com",
    "www.linkedin.com/comm",
    "t.glassdoor.com",
    "click.glassdoor.com",
    "click.appcast.io",
    "email.welcometothejungle.com",
    "links.indeed.com",
    "click.email.indeed.com",
    "cts.indeed.com",
    "match.indeed.com",
    "awstrack.me",
    "hs-sites.com",
    "t.hsms06.com",
)

# Path fragments that mark a tracker even on an otherwise normal host.
TRACKER_PATHS = ("/ls/click", "/rc/clk", "/tk/", "/CL0/")

# Query params that carry credentials or tracking. Dropped from every URL before
# it is stored or printed. `token` is the one that matters: see module docstring.
SECRET_PARAMS = frozenset({
    "token",
    "jwt",
    "auth",
    "authtoken",
    "access_token",
    "id_token",
    "refresh_token",
    "key",
    "apikey",
    "api_key",
    "sig",
    "signature",
    "hmac",
    "session",
    "sessionid",
    "_hsenc",
    "_hsmi",
    "mkt_tok",
    "trk",
    "trkemail",
    "midtoken",
    "midsig",
    "eid",
    # Glassdoor digest links carry these; uservalidationkey and jak
    # authenticate the click, the rest identify the mail send.
    "uservalidationkey",
    "jak",
    "uido",
    "guid",
    "jrtk",
})

# Tracking params that are noise rather than secrets. Kept in sync with
# sheets.normalize_url, which strips the same set for dedup comparison.
NOISE_PARAMS = frozenset({"ref", "source", "position", "count"})

# app.otta.com and app.welcometothejungle.com are the same product; job pages
# are /jobs/<externalId>, which is the key publicJob(externalId:) takes.
WTTJ_JOB_RE = re.compile(
    r"^https?://app\.(?:otta\.com|welcometothejungle\.com)/jobs/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class _LinkParser(HTMLParser):
    """Collect (anchor_text, href) pairs, keeping the text nested inside <a>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._depth = 0
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        if self._href is not None:
            # Nested <a> is invalid HTML but shows up in mail templates; treat
            # the inner one as a continuation rather than losing the outer href.
            self._depth += 1
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href
            self._depth = 1
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        text = re.sub(r"\s+", " ", "".join(self._text)).strip()
        self.links.append((text, self._href))
        self._href = None
        self._text = []

    def close(self) -> None:
        # Unclosed <a> at end of document still carries a usable href.
        if self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._text)).strip()
            self.links.append((text, self._href))
            self._href = None
        super().close()


def extract_html(payload: dict) -> str:
    """Return the raw text/html part of a Gmail message payload, undecoded tags intact.

    The sibling of gmail_scanner._extract_body: same recursion, but it keeps the
    markup instead of stripping it.
    """
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    parts = payload.get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in parts:
        html = extract_html(part)
        if html:
            return html

    return ""


def extract_links(html: str) -> list[tuple[str, str]]:
    """Extract (anchor_text, href) pairs from HTML, in document order.

    Only http(s) hrefs are returned; mailto:, tel: and anchors are dropped.
    Duplicate hrefs are collapsed, keeping the first non-empty anchor text —
    mail templates wrap the logo, the title and the button in separate links to
    the same URL, and only one of them carries the job title.
    """
    parser = _LinkParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed mail HTML must not kill a scan
        pass

    seen: dict[str, str] = {}
    order: list[str] = []
    for text, href in parser.links:
        href = href.strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        if href not in seen:
            seen[href] = text
            order.append(href)
        elif not seen[href] and text:
            seen[href] = text
    return [(seen[h], h) for h in order]


def is_tracker(url: str) -> bool:
    """True if the URL is a click-tracking redirect rather than a destination."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in TRACKER_HOSTS):
        return True
    return any(frag.lower() in parsed.path.lower() for frag in TRACKER_PATHS)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface the Location header instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def resolve_once(url: str, timeout: float = 10.0) -> str | None:
    """Return the Location a single redirect points at, or None if there is none."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=timeout) as resp:
            # 2xx: this is the destination, not a redirect.
            return resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return e.headers.get("Location")
        return None
    except Exception:  # noqa: BLE001 — a dead tracker link must not kill a scan
        return None


def unwrap(url: str, max_hops: int = 5) -> str:
    """Follow tracker redirects to the real destination.

    Stops at the first non-tracker URL, after max_hops, or when a hop fails —
    returning the best URL reached so far rather than raising.
    """
    current = url
    for _ in range(max_hops):
        if not is_tracker(current):
            return current
        location = resolve_once(current)
        if not location:
            return current
        current = urljoin(current, location)
    return current


def strip_secrets(url: str) -> str:
    """Remove credential and tracking params. Call before logging or storing.

    Drops SECRET_PARAMS, NOISE_PARAMS and anything utm_-prefixed, preserving the
    order of what remains so the URL stays recognisable. Params that identify the
    job (gh_jid, jobId, gh_src) are kept — only the deny-list goes.
    """
    url = url.strip()
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    query, hash_sep, fragment = query.partition("#")
    kept = []
    for param in query.split("&"):
        if not param:
            continue
        key = param.split("=", 1)[0].lower()
        if key in SECRET_PARAMS or key in NOISE_PARAMS or key.startswith("utm_"):
            continue
        kept.append(param)
    out = base + ("?" + "&".join(kept) if kept else "")
    return out + (hash_sep + fragment if fragment else "")


_INDEED_JK_RE = re.compile(r"[?&]jk=([A-Za-z0-9]+)")
_GLASSDOOR_ID_RE = re.compile(r"[?&]jobListingId=(\d+)")


def canonicalize(url: str) -> str:
    """Rewrite board redirect URLs into their stable public form.

    Indeed hands out /rc/clk?jk=<key>&tk=<session>&bb=<blob> links that resolve
    no further; the same posting is /viewjob?jk=<key>. Canonicalising here means
    url_parser recognises it and dedup sees one URL rather than one per email.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("indeed.com") and "/rc/clk" in parsed.path:
        match = _INDEED_JK_RE.search(url)
        if match:
            return f"https://www.indeed.com/viewjob?jk={match.group(1)}"
    if host.endswith("glassdoor.com"):
        # A digest link is jobListing.htm?pos=101&ao=...&jobListingId=N plus a
        # dozen send-tracking params. Only jobListingId identifies the posting;
        # without this, two different jobs in one email look like one URL.
        match = _GLASSDOOR_ID_RE.search(url)
        if match:
            return (
                "https://www.glassdoor.com/partner/jobListing.htm"
                f"?jobListingId={match.group(1)}"
            )
    return url


def wttj_external_id(url: str) -> str | None:
    """Return the Welcome to the Jungle externalId in a job URL, or None.

    The externalId is what publicJob(externalId:) takes; resolving it to the
    employer's ATS URL needs the authenticated GraphQL call in the Chrome phase.
    """
    match = WTTJ_JOB_RE.match(url.strip())
    return match.group(1) if match else None
