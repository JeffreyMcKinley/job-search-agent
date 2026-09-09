"""Mock `claude-in-chrome` MCP server (stdio, JSON-RPC 2.0).

Stands in for the real Chrome extension so `proficiently:apply` can be evaluated
offline and deterministically. Every tool call is appended to a JSONL log; that
log is the assertion surface for tests/asserts.py.

Usage (from tests/runner.py, via --mcp-config):
    python mock_chrome/server.py --fixtures ./fixtures --log ./run/calls.jsonl

Tool names mirror the real server so SKILL.md instructions resolve unchanged:
mcp__claude-in-chrome__{tabs_context_mcp,tabs_create_mcp,navigate,read_page,
form_input,find,computer,javascript_tool,get_page_text,tabs_close_mcp}.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pages import Page, load_pages, resolve_url  # noqa: E402


PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "tabs_context_mcp",
        "description": "Get context about the current MCP tab group.",
        "inputSchema": {
            "type": "object",
            "properties": {"createIfEmpty": {"type": "boolean"}},
        },
    },
    {
        "name": "tabs_create_mcp",
        "description": "Create a new empty tab in the MCP tab group.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "tabs_close_mcp",
        "description": "Close a tab in the MCP tab group by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"tabId": {"type": "integer"}},
            "required": ["tabId"],
        },
    },
    {
        "name": "navigate",
        "description": "Navigate a tab to a URL, or 'back'/'forward'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "tabId": {"type": "number"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "read_page",
        "description": (
            "Accessibility tree for the page. filter=interactive for "
            "buttons/links/inputs only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "number"},
                "filter": {"type": "string", "enum": ["interactive", "all"]},
                "max_chars": {"type": "number"},
                "depth": {"type": "number"},
                "ref_id": {"type": "string"},
            },
            "required": ["tabId"],
        },
    },
    {
        "name": "form_input",
        "description": "Set a value on a form element by ref id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "number"},
                "ref": {"type": "string"},
                "value": {"type": ["string", "boolean", "number"]},
            },
            "required": ["tabId", "ref", "value"],
        },
    },
    {
        "name": "find",
        "description": "Find elements on the page using natural language.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "number"},
                "query": {"type": "string"},
            },
            "required": ["tabId", "query"],
        },
    },
    {
        "name": "computer",
        "description": "Mouse/keyboard/screenshot actions on a tab.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "number"},
                "action": {"type": "string"},
                "ref": {"type": "string"},
                "text": {"type": "string"},
                "coordinate": {"type": "array", "items": {"type": "number"}},
                "scroll_direction": {"type": "string"},
                "scroll_amount": {"type": "number"},
                "duration": {"type": "number"},
            },
            "required": ["tabId", "action"],
        },
    },
    {
        "name": "javascript_tool",
        "description": "Execute JavaScript in the page context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tabId": {"type": "number"},
                "action": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["tabId", "action", "text"],
        },
    },
    {
        "name": "get_page_text",
        "description": "Extract raw text content from the page.",
        "inputSchema": {
            "type": "object",
            "properties": {"tabId": {"type": "number"}},
            "required": ["tabId"],
        },
    },
]


class Tab:
    def __init__(self, tab_id: int):
        self.id = tab_id
        self.page: Page | None = None
        self.viewport = 0
        self.values: dict[str, dict[str, Any]] = {}  # page id -> {ref: value}

    def page_values(self) -> dict[str, Any]:
        assert self.page is not None
        return self.values.setdefault(self.page.id, {})


class MockChrome:
    def __init__(self, fixtures: pathlib.Path, log_path: pathlib.Path,
                 start_page: str | None, state_path: pathlib.Path | None):
        self.pages = load_pages(fixtures)
        self.log_path = log_path
        self.start_page = start_page
        self.state_path = state_path
        self.tabs: dict[int, Tab] = {}
        self.next_tab_id = 1001
        self.seq = 0
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.write_text("", encoding="utf-8")
        self.load_state()
        if start_page and not self.tabs:
            # `/proficiently:apply current` means "the form the user already has
            # open". That tab has to exist before the skill first calls
            # tabs_context_mcp, or the skill correctly reports there is nothing
            # to work with and stops.
            self.create_tab()
            self.save_state()

    # ---------- persistence ----------
    # Each user turn is a separate `claude -p --resume` process, which spawns a
    # fresh MCP server. Browser state has to outlive the process or turn 2 would
    # start on about:blank with an empty form.

    def load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            blob = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.next_tab_id = blob.get("next_tab_id", self.next_tab_id)
        self.seq = blob.get("seq", 0)
        for raw in blob.get("tabs", []):
            tab = Tab(int(raw["id"]))
            page_id = raw.get("page_id")
            tab.page = self.pages.get(page_id) if page_id else None
            tab.viewport = int(raw.get("viewport", 0))
            tab.values = {k: dict(v) for k, v in raw.get("values", {}).items()}
            self.tabs[tab.id] = tab

    def save_state(self) -> None:
        if self.state_path is None:
            return
        blob = {
            "next_tab_id": self.next_tab_id,
            "seq": self.seq,
            "tabs": [
                {
                    "id": tab.id,
                    "page_id": tab.page.id if tab.page else None,
                    "viewport": tab.viewport,
                    "values": tab.values,
                }
                for tab in self.tabs.values()
            ],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(blob, indent=2), encoding="utf-8")

    # ---------- logging ----------

    def log(self, tool: str, args: dict[str, Any], derived: dict[str, Any],
            result: str) -> None:
        self.seq += 1
        record = {
            "seq": self.seq,
            "ts": time.time(),
            "tool": tool,
            "args": args,
            **derived,
            "result_preview": result[:400],
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    # ---------- helpers ----------

    def create_tab(self) -> Tab:
        tab = Tab(self.next_tab_id)
        self.next_tab_id += 1
        self.tabs[tab.id] = tab
        if self.start_page and self.start_page in self.pages:
            tab.page = self.pages[self.start_page]
        return tab

    def get_tab(self, args: dict[str, Any]) -> Tab:
        tab_id = args.get("tabId")
        if tab_id is not None and int(tab_id) in self.tabs:
            return self.tabs[int(tab_id)]
        if self.tabs:
            return next(iter(self.tabs.values()))
        return self.create_tab()

    def tab_listing(self) -> str:
        if not self.tabs:
            return "No tabs in the MCP tab group."
        rows = []
        for tab in self.tabs.values():
            where = tab.page.url if tab.page else "about:blank"
            title = tab.page.title if tab.page else "New Tab"
            rows.append(f"tabId {tab.id}: {title} - {where}")
        return "\n".join(rows)

    # ---------- tools ----------

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        handler = getattr(self, f"t_{name}", None)
        if handler is None:
            return f"Unknown tool: {name}", {}
        return handler(args)

    def t_tabs_context_mcp(self, args):
        if not self.tabs and args.get("createIfEmpty"):
            self.create_tab()
        return self.tab_listing(), {}

    def t_tabs_create_mcp(self, args):
        tab = self.create_tab()
        return f"Created tabId {tab.id}.\n{self.tab_listing()}", {"tab_id": tab.id}

    def t_tabs_close_mcp(self, args):
        tab_id = int(args["tabId"])
        self.tabs.pop(tab_id, None)
        return f"Closed tabId {tab_id}.", {}

    def t_navigate(self, args):
        url = str(args["url"])
        if not self.tabs:
            self.create_tab()
        tab = self.get_tab(args)
        if url in ("back", "forward"):
            here = tab.page.url if tab.page else "about:blank"
            return f"Navigated {url}. Current page: {here}", {}
        page = resolve_url(self.pages, url)
        if page is None:
            return (
                f"Navigation to {url} failed: no fixture page is registered "
                f"for this URL."
            ), {"nav_failed": True}
        tab.page = page
        tab.viewport = 0
        return (
            f"Navigated tabId {tab.id} to {page.url}\nTitle: {page.title}",
            {"page_id": page.id, "url": page.url},
        )

    def t_read_page(self, args):
        tab = self.get_tab(args)
        if tab.page is None:
            return "about:blank - nothing to read.", {}
        interactive = args.get("filter") == "interactive"
        tree = tab.page.render_tree(
            tab.page_values(), interactive_only=interactive, viewport=tab.viewport
        )
        return tree, {"page_id": tab.page.id, "viewport": tab.viewport}

    def t_get_page_text(self, args):
        tab = self.get_tab(args)
        if tab.page is None:
            return "about:blank", {}
        return tab.page.text or tab.page.title, {"page_id": tab.page.id}

    def t_find(self, args):
        tab = self.get_tab(args)
        if tab.page is None:
            return "about:blank - nothing to find.", {}
        return (
            tab.page.find(str(args["query"]), tab.page_values()),
            {"page_id": tab.page.id},
        )

    def t_form_input(self, args):
        tab = self.get_tab(args)
        if tab.page is None:
            return "about:blank - no form.", {}
        ref = str(args["ref"])
        el = tab.page.by_ref(ref)
        if el is None:
            return f"No element with ref {ref} on this page.", {"input_failed": True}
        if el.role == "file":
            return (
                f'"{el.label}" is a file upload; it cannot be set with form_input.'
            ), {"label": el.label, "input_failed": True}
        value = args["value"]
        if el.options and str(value) not in el.options:
            return (
                f'"{value}" is not an option for "{el.label}". '
                f"Options: {', '.join(el.options)}"
            ), {"label": el.label, "input_failed": True}
        tab.page_values()[ref] = value
        return (
            f'Set "{el.label}" to "{value}".',
            {"page_id": tab.page.id, "label": el.label, "value": value},
        )

    def t_javascript_tool(self, args):
        tab = self.get_tab(args)
        if tab.page is None:
            return "undefined", {}
        return tab.page.js_result(str(args.get("text", ""))), {"page_id": tab.page.id}

    def t_computer(self, args):
        tab = self.get_tab(args)
        action = str(args["action"])
        if tab.page is None:
            return "about:blank", {}
        if action in ("screenshot", "zoom"):
            body = tab.page.render_tree(
                tab.page_values(), interactive_only=True, viewport=tab.viewport
            )
            return "[screenshot omitted in mock]\n" + body, {"page_id": tab.page.id}
        if action == "scroll":
            direction = str(args.get("scroll_direction", "down"))
            if direction == "down":
                tab.viewport = min(tab.viewport + 1, tab.page.viewports - 1)
            elif direction == "up":
                tab.viewport = max(tab.viewport - 1, 0)
            return (
                f"Scrolled {direction}. Now at viewport "
                f"{tab.viewport + 1}/{tab.page.viewports}."
            ), {"page_id": tab.page.id, "viewport": tab.viewport}
        if action in ("left_click", "double_click"):
            return self._click(tab, args)
        if action == "type":
            return f'Typed "{args.get("text", "")}".', {"page_id": tab.page.id}
        return f"{action} ok.", {"page_id": tab.page.id}

    def _click(self, tab: Tab, args) -> tuple[str, dict[str, Any]]:
        assert tab.page is not None
        el = tab.page.by_ref(str(args.get("ref", ""))) if args.get("ref") else None
        if el is None:
            return (
                "Clicked at coordinates. Nothing actionable is bound to that "
                "point in the mock - click by ref instead."
            ), {"page_id": tab.page.id, "click_unbound": True}
        derived: dict[str, Any] = {
            "page_id": tab.page.id,
            "label": el.label,
            "clicked": el.label,
        }
        if el.gate == "auth":
            return (
                "Sign in required. This page needs an account before the "
                "application can continue."
            ), {**derived, "auth_gate": True}
        if el.role in ("radio", "checkbox"):
            tab.page_values()[el.ref] = True
            return f'Selected "{el.label}".', {**derived, "value": True}
        if el.requires_complete:
            missing = [
                e.label
                for e in tab.page.required_inputs()
                if tab.page_values().get(e.ref) in (None, "")
            ]
            if missing:
                return (
                    "Validation failed. Required fields are still empty: "
                    + ", ".join(missing)
                ), {**derived, "validation_failed": True, "missing": missing}
        if el.goto and el.goto in self.pages:
            tab.page = self.pages[el.goto]
            tab.viewport = 0
            return (
                f'Clicked "{el.label}".\nNavigated to {tab.page.url}\n'
                f"Title: {tab.page.title}"
            ), {**derived, "goto": tab.page.id}
        return f'Clicked "{el.label}".', derived


# ---------- JSON-RPC plumbing ----------


def respond(msg_id: Any, result: Any) -> None:
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n"
    )
    sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument(
        "--start-page",
        default=None,
        help="fixture page id preloaded into new tabs (models `/apply current`)",
    )
    ap.add_argument(
        "--state",
        default=None,
        help="JSON file that carries tab/form state across turns",
    )
    opts = ap.parse_args()

    chrome = MockChrome(
        pathlib.Path(opts.fixtures),
        pathlib.Path(opts.log),
        opts.start_page,
        pathlib.Path(opts.state) if opts.state else None,
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            respond(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "claude-in-chrome", "version": "mock-1"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            respond(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            try:
                text, derived = chrome.call(name, args)
            except Exception as exc:  # surface mock bugs as tool errors, don't die
                text, derived = f"Mock error in {name}: {exc!r}", {"mock_error": True}
            chrome.log(name, args, derived, text)
            chrome.save_state()
            respond(msg_id, {"content": [{"type": "text", "text": text}]})
        elif msg_id is not None:
            respond(msg_id, {})


if __name__ == "__main__":
    main()
