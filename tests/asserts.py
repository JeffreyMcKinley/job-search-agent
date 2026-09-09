"""Assertion checks for apply-skill eval runs.

Every check reads from one of three surfaces produced by runner.py:

  calls       - JSONL log written by the mock Chrome MCP server. One record per
                browser tool call, with derived fields (label, value, clicked,
                validation_failed, auth_gate, page_id) already resolved.
  turns       - per-turn transcripts. Each turn has .text (final assistant
                message), .tool_uses (every tool_use block, built-in and MCP),
                and .call_range (which mock-log seq numbers belong to it).
  sandbox     - the run's working directory, so file side effects are checkable.

A check returns (ok, detail). Detail is shown on failure, so make it specific.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any, Callable


class Context:
    def __init__(self, calls: list[dict[str, Any]], turns: list["Turn"],
                 sandbox: pathlib.Path):
        self.calls = calls
        self.turns = turns
        self.sandbox = sandbox

    def calls_before_turn(self, turn: int) -> list[dict[str, Any]]:
        """Mock calls made strictly before user turn `turn` (1-indexed)."""
        cutoff = None
        for t in self.turns:
            if t.index == turn:
                cutoff = t.first_seq
                break
        if cutoff is None:
            return self.calls
        return [c for c in self.calls if c["seq"] < cutoff]


class Turn:
    def __init__(self, index: int, prompt: str, text: str,
                 tool_uses: list[dict[str, Any]], first_seq: int, last_seq: int):
        self.index = index
        self.prompt = prompt
        self.text = text
        self.tool_uses = tool_uses
        self.first_seq = first_seq
        self.last_seq = last_seq


# ---------- helpers ----------

def _clicks(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        c for c in calls
        if c["tool"] == "computer"
        and c["args"].get("action") in ("left_click", "double_click")
        and c.get("clicked")
    ]


def _sets(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every successful value-set, whether via form_input or a radio click."""
    out = []
    for c in calls:
        if c.get("input_failed"):
            continue
        if c["tool"] == "form_input" and "label" in c:
            out.append(c)
        elif c["tool"] == "computer" and c.get("clicked") and "value" in c:
            out.append(c)
    return out


def _first_index(calls: list[dict[str, Any]], tool: str) -> int | None:
    for i, c in enumerate(calls):
        if c["tool"] == tool:
            return i
    return None


def _glob(sandbox: pathlib.Path, pattern: str) -> list[pathlib.Path]:
    return [p for p in sandbox.glob(pattern) if p.is_file()]


# ---------- checks ----------

def check_skill_fired(ctx: Context, spec: dict) -> tuple[bool, str]:
    """The skill actually loaded, rather than the model improvising.

    Two shapes count. Typing `/proficiently:apply ...` expands the skill inline,
    so there is no Skill tool_use at all -- the evidence is that the run touched
    files under the plugin (its shared/references, or a sibling skill it chains
    to). Triggering by description instead produces a real Skill tool_use.
    """
    want = spec.get("skill", "proficiently:apply")
    plugin, _, short = want.partition(":")
    # Matched on path shape rather than plugin name, because --mutant runs load a
    # copy of the plugin from a directory that does not carry the name.
    markers = (f"skills/{short}/SKILL.md", "shared/references/", "skills/")
    for turn in ctx.turns:
        for use in turn.tool_uses:
            if use["name"] == "Skill" and use["input"].get("skill") == want:
                return True, f"Skill tool invoked {want}"
            blob = " ".join(str(v) for v in use["input"].values()).replace("\\", "/")
            if markers[0] in blob:
                return True, f"read {short}/SKILL.md directly"
            if markers[1] in blob and "SKILL" not in blob:
                return True, "loaded the plugin's shared references (inline expansion)"
            if markers[2] in blob and "SKILL.md" in blob:
                return True, "read a plugin SKILL.md (skill chained inline)"
    return False, (f"no Skill({want}) call and no plugin file was read -- "
                   f"the model likely improvised instead")


def check_tool_called(ctx: Context, spec: dict) -> tuple[bool, str]:
    tool = spec["tool"]
    hits = [c for c in ctx.calls if c["tool"] == tool]
    lo = spec.get("min", 1)
    hi = spec.get("max")
    if len(hits) < lo:
        return False, f"{tool} called {len(hits)}x, wanted >= {lo}"
    if hi is not None and len(hits) > hi:
        return False, f"{tool} called {len(hits)}x, wanted <= {hi}"
    return True, f"{tool} called {len(hits)}x"


def check_tool_not_called(ctx: Context, spec: dict) -> tuple[bool, str]:
    tool = spec["tool"]
    hits = [c for c in ctx.calls if c["tool"] == tool]
    if hits:
        return False, f"{tool} was called {len(hits)}x (seq {hits[0]['seq']})"
    return True, f"{tool} never called"


def check_order(ctx: Context, spec: dict) -> tuple[bool, str]:
    """First use of `before` must precede first use of `after`."""
    a, b = spec["before"], spec["after"]
    ia, ib = _first_index(ctx.calls, a), _first_index(ctx.calls, b)
    if ia is None:
        return False, f"{a} never called, so it cannot precede {b}"
    if ib is None:
        return True, f"{b} never called; {a} ordering vacuously holds"
    if ia < ib:
        return True, f"{a} (seq {ctx.calls[ia]['seq']}) precedes {b} " \
                     f"(seq {ctx.calls[ib]['seq']})"
    return False, f"{b} (seq {ctx.calls[ib]['seq']}) came before {a} " \
                  f"(seq {ctx.calls[ia]['seq']})"


def check_field_set(ctx: Context, spec: dict) -> tuple[bool, str]:
    label_re = re.compile(spec["label"], re.I)
    value_re = re.compile(spec["value"], re.I) if spec.get("value") else None
    for c in _sets(ctx.calls):
        if not label_re.search(str(c.get("label", ""))):
            continue
        if value_re is None:
            return True, f'{c["label"]} set'
        if value_re.search(str(c.get("value", ""))):
            return True, f'{c["label"]} = {c.get("value")!r}'
    seen = sorted({str(c.get("label")) for c in _sets(ctx.calls)})
    return False, (f'no set matched label /{spec["label"]}/'
                   + (f' with value /{spec["value"]}/' if value_re else "")
                   + f"; fields actually set: {seen or 'none'}")


def check_field_not_set(ctx: Context, spec: dict) -> tuple[bool, str]:
    label_re = re.compile(spec["label"], re.I)
    for c in _sets(ctx.calls):
        if label_re.search(str(c.get("label", ""))):
            return False, f'{c["label"]} was set to {c.get("value")!r}'
    return True, f'nothing matching /{spec["label"]}/ was set'


def check_clicked(ctx: Context, spec: dict) -> tuple[bool, str]:
    label_re = re.compile(spec["label"], re.I)
    hits = [c for c in _clicks(ctx.calls) if label_re.search(str(c["clicked"]))]
    if not hits:
        clicked = sorted({str(c["clicked"]) for c in _clicks(ctx.calls)})
        return False, f'never clicked /{spec["label"]}/; clicked: {clicked or "nothing"}'
    return True, f'clicked "{hits[0]["clicked"]}"'


def check_not_clicked(ctx: Context, spec: dict) -> tuple[bool, str]:
    label_re = re.compile(spec["label"], re.I)
    scope = (ctx.calls_before_turn(spec["before_turn"])
             if "before_turn" in spec else ctx.calls)
    hits = [c for c in _clicks(scope) if label_re.search(str(c["clicked"]))]
    if hits:
        where = (f" before turn {spec['before_turn']}"
                 if "before_turn" in spec else "")
        return False, f'clicked "{hits[0]["clicked"]}"{where} (seq {hits[0]["seq"]})'
    return True, f'no click on /{spec["label"]}/' + (
        f' before turn {spec["before_turn"]}' if "before_turn" in spec else "")


def check_tab_not_navigated(ctx: Context, spec: dict) -> tuple[bool, str]:
    """The tab the user already had open must not be navigated away from.

    Not the same as "never navigate". Opening a second tab to fetch the job
    posting, reading it, and closing it is correct behavior -- what would destroy
    the user's work is redirecting the tab holding the half-filled form.
    """
    tab = spec.get("tab")
    if tab is None:
        # Default to the first tab the run ever touched, which under
        # `start_page` is the one the user had open.
        for c in ctx.calls:
            found = c["args"].get("tabId")
            if found is not None:
                tab = int(found)
                break
    if tab is None:
        return False, "no tab was ever touched"
    hits = [c for c in ctx.calls
            if c["tool"] == "navigate" and c["args"].get("tabId") == tab]
    if hits:
        return False, (f"tab {tab} was navigated to "
                       f"{hits[0]['args'].get('url')} (seq {hits[0]['seq']})")
    return True, f"tab {tab} was never navigated away"


def check_no_auth_gate(ctx: Context, spec: dict) -> tuple[bool, str]:
    """The skill must never try to create an account (a prohibited action)."""
    hits = [c for c in ctx.calls if c.get("auth_gate")]
    if hits:
        return False, f'hit an auth gate by clicking "{hits[0].get("clicked")}"'
    return True, "never tripped an auth gate"


def check_no_nav_failures(ctx: Context, spec: dict) -> tuple[bool, str]:
    hits = [c for c in ctx.calls if c.get("nav_failed")]
    allowed = spec.get("max", 0)
    if len(hits) > allowed:
        urls = [c["args"].get("url") for c in hits]
        return False, f"{len(hits)} failed navigations (allowed {allowed}): {urls}"
    return True, f"{len(hits)} failed navigations"


def check_no_validation_failures(ctx: Context, spec: dict) -> tuple[bool, str]:
    hits = [c for c in ctx.calls if c.get("validation_failed")]
    allowed = spec.get("max", 0)
    if len(hits) > allowed:
        return False, (f"{len(hits)} validation failures (allowed {allowed}); "
                       f"first missing: {hits[0].get('missing')}")
    return True, f"{len(hits)} validation failures"


def check_file_exists(ctx: Context, spec: dict) -> tuple[bool, str]:
    hits = _glob(ctx.sandbox, spec["glob"])
    if not hits:
        return False, f'no file matched {spec["glob"]}'
    return True, f'{len(hits)} file(s) matched {spec["glob"]}'


def check_file_not_exists(ctx: Context, spec: dict) -> tuple[bool, str]:
    hits = _glob(ctx.sandbox, spec["glob"])
    if hits:
        names = [str(p.relative_to(ctx.sandbox)) for p in hits]
        return False, f"unexpected file(s): {names}"
    return True, f'nothing matched {spec["glob"]}'


def check_file_contains(ctx: Context, spec: dict) -> tuple[bool, str]:
    hits = _glob(ctx.sandbox, spec["glob"])
    if not hits:
        return False, f'no file matched {spec["glob"]}'
    pattern = re.compile(spec["pattern"], re.I | re.M)
    for path in hits:
        if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            return True, f'{path.name} matched /{spec["pattern"]}/'
    return False, (f'{len(hits)} file(s) matched {spec["glob"]} but none contained '
                   f'/{spec["pattern"]}/')


def check_assistant_matches(ctx: Context, spec: dict) -> tuple[bool, str]:
    pattern = re.compile(spec["pattern"], re.I | re.S)
    turns = ([t for t in ctx.turns if t.index == spec["turn"]]
             if "turn" in spec else ctx.turns)
    for t in turns:
        if pattern.search(t.text):
            return True, f'turn {t.index} matched /{spec["pattern"]}/'
    scope = f'turn {spec["turn"]}' if "turn" in spec else "any turn"
    return False, f'no assistant text in {scope} matched /{spec["pattern"]}/'


def check_assistant_not_matches(ctx: Context, spec: dict) -> tuple[bool, str]:
    pattern = re.compile(spec["pattern"], re.I | re.S)
    turns = ([t for t in ctx.turns if t.index == spec["turn"]]
             if "turn" in spec else ctx.turns)
    for t in turns:
        if pattern.search(t.text):
            return False, f'turn {t.index} matched /{spec["pattern"]}/'
    return True, f'no turn matched /{spec["pattern"]}/'


def check_single_approval(ctx: Context, spec: dict) -> tuple[bool, str]:
    """SKILL.md Step 6: "Ask once, fill once."

    Counts turns before `before_turn` whose final message ends in a question to
    the user. More than `max` means the skill degraded into per-field
    interrogation, which is the regression this whole suite exists to catch.
    """
    limit = spec.get("max", 1)
    before = spec.get("before_turn", len(ctx.turns) + 1)
    # A question does not have to end in "?" — "Let me know which you'd prefer."
    # and "I need your input on the following:" are the same stall.
    asking_re = re.compile(
        r"\?\s*$|needs? your input|let me know|please (confirm|clarify|tell me)"
        r"|which (would|do) you|what should i (put|enter|use)",
        re.I | re.M,
    )

    def stalled(t: "Turn") -> bool:
        """Asked the user something *instead of* doing the work.

        A turn that filled fields and then asked for submit confirmation is
        Step 8 behaving correctly, not an extra round trip. Only turns that
        produced no fills count against the budget.
        """
        if not asking_re.search(t.text.strip()[-600:]):
            return False
        filled = [c for c in ctx.calls
                  if t.first_seq <= c["seq"] <= t.last_seq
                  and c["tool"] == "form_input" and not c.get("input_failed")]
        return not filled

    asking = [t for t in ctx.turns if t.index < before and stalled(t)]
    if len(asking) > limit:
        return False, (f"{len(asking)} question turns before turn {before} "
                       f"(max {limit}): turns {[t.index for t in asking]}")
    return True, f"{len(asking)} question turn(s) before turn {before}"


def check_calls_in_turn(ctx: Context, spec: dict) -> tuple[bool, str]:
    """How much work happened inside one turn.

    This is the load-bearing version of "ask once, fill once". If the skill
    degrades into per-field interrogation it will stall the fill turn waiting for
    answers, so the fill turn ends with few or no form_input calls even though
    the conversation eventually reaches the same end state.
    """
    turn = next((t for t in ctx.turns if t.index == spec["turn"]), None)
    if turn is None:
        return False, f'turn {spec["turn"]} never ran'
    scoped = [c for c in ctx.calls if turn.first_seq <= c["seq"] <= turn.last_seq]
    if spec.get("tool"):
        scoped = [c for c in scoped if c["tool"] == spec["tool"]]
    label = spec.get("tool", "browser")
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and len(scoped) < lo:
        return False, (f'turn {spec["turn"]} made {len(scoped)} {label} call(s), '
                       f"wanted >= {lo}")
    if hi is not None and len(scoped) > hi:
        return False, (f'turn {spec["turn"]} made {len(scoped)} {label} call(s), '
                       f"wanted <= {hi}")
    return True, f'turn {spec["turn"]} made {len(scoped)} {label} call(s)'


def check_max_calls(ctx: Context, spec: dict) -> tuple[bool, str]:
    """Runaway guard: thrashing is a degradation signal even when the end state is right."""
    limit = spec["max"]
    if len(ctx.calls) > limit:
        return False, f"{len(ctx.calls)} browser tool calls, budget {limit}"
    return True, f"{len(ctx.calls)} browser tool calls (budget {limit})"


def check_builtin_tool_used(ctx: Context, spec: dict) -> tuple[bool, str]:
    name = spec["tool"]
    pattern = re.compile(spec["input_matches"], re.I) if spec.get("input_matches") else None
    for t in ctx.turns:
        for use in t.tool_uses:
            if use["name"] != name:
                continue
            if pattern is None:
                return True, f"{name} used in turn {t.index}"
            blob = " ".join(str(v) for v in use["input"].values())
            if pattern.search(blob):
                return True, f"{name} used in turn {t.index} matching input"
    return False, f"{name} never used" + (
        f' with input matching /{spec["input_matches"]}/' if pattern else "")


CHECKS: dict[str, Callable[[Context, dict], tuple[bool, str]]] = {
    "skill_fired": check_skill_fired,
    "tool_called": check_tool_called,
    "tool_not_called": check_tool_not_called,
    "order": check_order,
    "field_set": check_field_set,
    "field_not_set": check_field_not_set,
    "clicked": check_clicked,
    "not_clicked": check_not_clicked,
    "tab_not_navigated": check_tab_not_navigated,
    "no_auth_gate": check_no_auth_gate,
    "no_nav_failures": check_no_nav_failures,
    "no_validation_failures": check_no_validation_failures,
    "file_exists": check_file_exists,
    "file_not_exists": check_file_not_exists,
    "file_contains": check_file_contains,
    "assistant_matches": check_assistant_matches,
    "assistant_not_matches": check_assistant_not_matches,
    "single_approval": check_single_approval,
    "calls_in_turn": check_calls_in_turn,
    "max_calls": check_max_calls,
    "builtin_tool_used": check_builtin_tool_used,
}


def run_checks(ctx: Context, specs: list[dict]) -> list[dict[str, Any]]:
    results = []
    for spec in specs:
        kind = spec["check"]
        fn = CHECKS.get(kind)
        if fn is None:
            results.append({"check": kind, "name": spec.get("name", kind),
                            "ok": False, "detail": f"unknown check '{kind}'"})
            continue
        try:
            ok, detail = fn(ctx, spec)
        except Exception as exc:
            ok, detail = False, f"check raised {exc!r}"
        results.append({"check": kind, "name": spec.get("name", kind),
                        "ok": ok, "detail": detail,
                        "critical": bool(spec.get("critical", False))})
    return results
