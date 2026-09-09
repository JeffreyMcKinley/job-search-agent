"""Fixture page model for the mock Chrome MCP server.

A "page" is a JSON spec (tests/fixtures/*.json) rather than real HTML. The skill
under test only ever perceives a page through MCP tool output, so a spec that can
render that output is sufficient — and keeps fixtures readable/diffable.

Spec shape:
{
  "id": "lever-acme-form",
  "url": "https://jobs.lever.co/acme/eng-123/apply",
  "title": "...",
  "text": "plain text returned by get_page_text",
  "js": [{"match": "grnhse_iframe", "result": "{...}"}],
  "elements": [
    {"role": "textbox",  "label": "First name", "required": true},
    {"role": "combobox", "label": "Country", "options": ["United States", "Canada"]},
    {"role": "radio",    "label": "Authorized to work in the US?",
                          "options": ["Yes", "No"], "hidden_from_read_page": true},
    {"role": "file",     "label": "Resume/CV", "required": true},
    {"role": "button",   "label": "Submit application", "goto": "lever-acme-thanks",
                          "requires_complete": true}
  ]
}

Notes on the flags:
  hidden_from_read_page  -> element is omitted from read_page output but findable
                            via `find` (models Workday radios; see SKILL.md
                            "ATS-Specific Interaction Notes").
  viewport               -> integer band; read_page(filter="interactive") only
                            returns elements whose band matches the tab's current
                            scroll position (models Workday viewport-only reads).
  requires_complete      -> clicking fails with a validation error unless every
                            required element on the page has a value.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any


INPUT_ROLES = {"textbox", "textarea", "combobox", "radio", "checkbox", "file"}


class Element:
    def __init__(self, ref: str, spec: dict[str, Any]):
        self.ref = ref
        self.role: str = spec["role"]
        self.label: str = spec["label"]
        self.required: bool = bool(spec.get("required", False))
        self.options: list[str] = list(spec.get("options", []))
        self.goto: str | None = spec.get("goto")
        self.requires_complete: bool = bool(spec.get("requires_complete", False))
        self.hidden: bool = bool(spec.get("hidden_from_read_page", False))
        self.viewport: int = int(spec.get("viewport", 0))
        self.gate: str | None = spec.get("gate")  # e.g. "auth" — click is refused

    @property
    def is_input(self) -> bool:
        return self.role in INPUT_ROLES

    def render(self, value: Any) -> str:
        bits = [f"[{self.ref}]", self.role, f'"{self.label}"']
        if self.required:
            bits.append("(required)")
        if self.options:
            bits.append("options: " + ", ".join(self.options))
        if value not in (None, ""):
            bits.append(f'value: "{value}"')
        return " ".join(bits)


class Page:
    def __init__(self, spec: dict[str, Any]):
        self.id: str = spec["id"]
        self.url: str = spec["url"]
        self.title: str = spec.get("title", self.id)
        self.text: str = spec.get("text", "")
        self.js: list[dict[str, str]] = spec.get("js", [])
        self.viewports: int = int(spec.get("viewports", 1))
        self.elements: list[Element] = [
            Element(f"ref_{i + 1}", e) for i, e in enumerate(spec.get("elements", []))
        ]

    def by_ref(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def required_inputs(self) -> list[Element]:
        """Required fields a submit click validates against.

        File uploads are excluded: MCP cannot upload PDFs, so the skill hands the
        path to the user (SKILL.md Step 7). The mock treats those as satisfied
        rather than blocking every case on a limitation that is by design.
        """
        return [e for e in self.elements
                if e.is_input and e.required and e.role != "file"]

    def js_result(self, code: str) -> str:
        for rule in self.js:
            if re.search(rule["match"], code):
                return rule["result"]
        return "undefined"

    def render_tree(self, values: dict[str, Any], *, interactive_only: bool,
                    viewport: int) -> str:
        lines = [f"# {self.title}", f"# {self.url}", ""]
        shown = 0
        for el in self.elements:
            if el.hidden:
                continue
            if interactive_only and not (el.is_input or el.role == "button"):
                continue
            if self.viewports > 1 and el.viewport != viewport:
                continue
            lines.append(el.render(values.get(el.ref)))
            shown += 1
        if shown == 0:
            lines.append("(no matching elements in the current viewport)")
        if self.viewports > 1:
            lines.append("")
            lines.append(
                f"(viewport {viewport + 1} of {self.viewports}; "
                "scroll to reveal the rest of the form)"
            )
        return "\n".join(lines)

    def find(self, query: str, values: dict[str, Any]) -> str:
        words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2]
        hits = []
        for el in self.elements:
            hay = f"{el.label} {el.role} {' '.join(el.options)}".lower()
            if any(w in hay for w in words):
                hits.append(el)
        if not hits:
            return f'No elements matched "{query}".'
        return "\n".join(el.render(values.get(el.ref)) for el in hits[:20])


def load_pages(fixture_dir: pathlib.Path) -> dict[str, Page]:
    pages: dict[str, Page] = {}
    for path in sorted(fixture_dir.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        for one in spec if isinstance(spec, list) else [spec]:
            page = Page(one)
            pages[page.id] = page
    return pages


def resolve_url(pages: dict[str, Page], url: str) -> Page | None:
    """Map a navigated URL onto a fixture page.

    Exact match wins; otherwise the page whose URL shares the longest prefix,
    so a skill appending "/apply" or hitting a Greenhouse embed URL still lands.
    """
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    for page in pages.values():
        if page.url == url:
            return page
    best, best_len = None, 0
    for page in pages.values():
        common = 0
        for a, b in zip(page.url, url):
            if a != b:
                break
            common += 1
        if common > best_len and common >= len("https://x.com"):
            best, best_len = page, common
    return best
