"""Behavioral eval runner for the `proficiently:apply` skill.

Each case is a scripted multi-turn conversation run against a sandboxed working
directory and a mock Chrome MCP server. Assertions run over the mock's tool-call
log, the transcripts, and the files the skill left behind.

    python tests/runner.py                       # every case, 3 runs each
    python tests/runner.py --case lever_basic    # one case
    python tests/runner.py --tag workday --runs 1
    python tests/runner.py --save-baseline       # record current scores
    python tests/runner.py --threshold 0.9       # exit 1 below this

Why a mock instead of real Chrome: the real thing would submit real job
applications, and no assertion about "did it ask before submitting" is safe to
test against a live ATS.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from asserts import Context, Turn, run_checks  # noqa: E402
from mock_chrome.pages import load_pages  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
CASES = ROOT / "cases"
SANDBOX_TEMPLATE = ROOT / "sandbox"
RUNS = ROOT / "runs"
BASELINES = ROOT / "baselines"

# The mock registers under this name because "claude-in-chrome" is a reserved
# MCP server name and is silently dropped by --mcp-config.
MCP_SERVER_NAME = "claude-in-chrome-mock"

DEFAULT_PLUGIN_DIR = pathlib.Path(
    os.environ.get(
        "PROFICIENTLY_PLUGIN_DIR",
        pathlib.Path.home() / ".claude" / "plugins" / "marketplaces" / "proficiently",
    )
)
PYTHON = os.environ.get("EVAL_PYTHON", sys.executable)
TURN_TIMEOUT = int(os.environ.get("EVAL_TURN_TIMEOUT", "900"))


# ---------- case loading ----------

def load_cases(name_filter: str | None, tags: list[str]) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(CASES.glob("*.toml")):
        case = tomllib.loads(path.read_text(encoding="utf-8"))
        case["path"] = path
        case.setdefault("name", path.stem)
        if name_filter and case["name"] != name_filter:
            continue
        if tags and not set(tags) & set(case.get("tags", [])):
            continue
        cases.append(case)
    return cases


# ---------- sandbox ----------

def build_sandbox(dest: pathlib.Path, case: dict[str, Any]) -> None:
    shutil.copytree(SANDBOX_TEMPLATE, dest)
    sb = case.get("sandbox", {})
    for rel in sb.get("remove", []):
        target = dest / rel
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for spec in sb.get("write", []):
        target = dest / spec["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(spec["content"], encoding="utf-8")


def build_mutant(plugin_dir: pathlib.Path, mutant: str,
                 dest: pathlib.Path) -> tuple[pathlib.Path, dict[str, Any]]:
    """Copy the plugin and damage one instruction in apply/SKILL.md.

    Mutation testing for prompts: if removing an instruction does not turn some
    assertion red, that assertion is not really protecting the instruction.
    """
    spec = tomllib.loads((ROOT / "mutants.toml").read_text(encoding="utf-8"))
    if mutant not in spec:
        raise SystemExit(f"unknown mutant '{mutant}'; have: {', '.join(spec)}")
    conf = spec[mutant]

    shutil.copytree(plugin_dir, dest, ignore=shutil.ignore_patterns(".git"))
    skill_md = dest / "skills" / "apply" / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    for edit in conf["edits"]:
        if edit["find"] not in text:
            raise SystemExit(
                f"mutant '{mutant}' no longer applies: its anchor text is not in "
                f"SKILL.md. The upstream skill changed; update mutants.toml.\n"
                f"--- anchor ---\n{edit['find'][:200]}"
            )
        text = text.replace(edit["find"], edit["replace"], 1)
    skill_md.write_text(text, encoding="utf-8")
    return dest, conf


def apply_attachments(run_dir: pathlib.Path, case: dict[str, Any],
                      turn: int) -> list[str]:
    """Simulate the user attaching a file between turns.

    MCP cannot upload a PDF, so SKILL.md Step 7 hands the path to the user and
    they attach it themselves while reviewing. Without modelling that, the agent
    sees a required Resume/CV field that is still empty and correctly refuses to
    submit -- which reads as a failure when it is the fixture that is wrong.
    """
    specs = [a for a in case.get("attachments", []) if a["turn"] == turn]
    if not specs:
        return []
    state_path = run_dir / "browser-state.json"
    if not state_path.exists():
        return []
    pages = load_pages(FIXTURES)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    done = []
    for spec in specs:
        label_re = re.compile(spec["label"], re.I)
        # Applied across every fixture page, not just the one currently open: on
        # Workday the upload field lives on a wizard page the run has not reached
        # yet when the user attaches the file.
        for tab in state.get("tabs", []):
            for page in pages.values():
                for el in page.elements:
                    if el.role == "file" and label_re.search(el.label):
                        tab.setdefault("values", {}).setdefault(
                            page.id, {})[el.ref] = spec["value"]
                        done.append(f"{page.id}:{el.label}")
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return done


def write_mcp_config(run_dir: pathlib.Path, case: dict[str, Any]) -> pathlib.Path:
    args = [
        str(ROOT / "mock_chrome" / "server.py"),
        "--fixtures", str(FIXTURES),
        "--log", str(run_dir / "calls.jsonl"),
        "--state", str(run_dir / "browser-state.json"),
    ]
    if case.get("start_page"):
        args += ["--start-page", case["start_page"]]
    config = {
        "mcpServers": {
            MCP_SERVER_NAME: {"type": "stdio", "command": PYTHON, "args": args}
        }
    }
    path = run_dir / "mcp.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


# ---------- running ----------

def run_turn(*, prompt: str, cwd: pathlib.Path, mcp_config: pathlib.Path,
             session_id: str, first: bool, model: str, plugin_dir: pathlib.Path,
             out_path: pathlib.Path) -> dict[str, Any]:
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--strict-mcp-config",
        "--mcp-config", str(mcp_config),
        # Keep the operator's own hooks, CLAUDE.md and settings out of the run,
        # and load the plugin under test explicitly instead of via user config.
        "--setting-sources", "project",
        "--plugin-dir", str(plugin_dir),
        "--permission-mode", "bypassPermissions",
        "--disallowed-tools", "Bash,WebFetch,WebSearch",
    ]
    cmd += ["--session-id", session_id] if first else ["--resume", session_id]

    with out_path.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(
            cmd, cwd=str(cwd), stdout=fh, stderr=subprocess.PIPE,
            text=True, timeout=TURN_TIMEOUT,
        )
    return {"returncode": proc.returncode, "stderr": (proc.stderr or "")[-2000:]}


def parse_stream(path: pathlib.Path) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    text_parts: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    final = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tool_uses.append({"name": block.get("name", ""),
                                      "input": block.get("input", {}) or {}})
        elif event.get("type") == "result":
            final = event.get("result", "") or ""
            meta = {
                "cost_usd": event.get("total_cost_usd"),
                "num_turns": event.get("num_turns"),
                "is_error": event.get("is_error"),
                "duration_ms": event.get("duration_ms"),
            }
    return (final or "\n".join(text_parts)), tool_uses, meta


def read_calls(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def run_once(case: dict[str, Any], run_dir: pathlib.Path, model: str,
             plugin_dir: pathlib.Path) -> dict[str, Any]:
    sandbox = run_dir / "cwd"
    build_sandbox(sandbox, case)
    mcp_config = write_mcp_config(run_dir, case)
    calls_path = run_dir / "calls.jsonl"
    session_id = str(uuid.uuid4())

    turns: list[Turn] = []
    cost = 0.0
    errors: list[str] = []

    for i, prompt in enumerate(case["turns"], start=1):
        attached = apply_attachments(run_dir, case, i)
        if attached:
            print(f"    (user attached file before turn {i}: {', '.join(attached)})")
        before = len(read_calls(calls_path))
        out_path = run_dir / f"turn-{i}.jsonl"
        try:
            status = run_turn(
                prompt=prompt, cwd=sandbox, mcp_config=mcp_config,
                session_id=session_id, first=(i == 1), model=model,
                plugin_dir=plugin_dir, out_path=out_path,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"turn {i} timed out after {TURN_TIMEOUT}s")
            break
        if status["returncode"] != 0:
            errors.append(f"turn {i} exited {status['returncode']}: "
                          f"{status['stderr'][-400:]}")
        text, tool_uses, meta = parse_stream(out_path)
        cost += float(meta.get("cost_usd") or 0.0)
        after = len(read_calls(calls_path))
        turns.append(Turn(i, prompt, text, tool_uses, before + 1, after))
        if status["returncode"] != 0:
            break

    calls = read_calls(calls_path)
    ctx = Context(calls, turns, sandbox)
    results = run_checks(ctx, case.get("asserts", []))
    passed = sum(1 for r in results if r["ok"])
    total = len(results) or 1
    critical_failed = [r for r in results if not r["ok"] and r.get("critical")]

    summary = {
        "case": case["name"],
        # Turn boundaries are recorded so rescore.py can re-run assertions over a
        # stored run without paying for the model again.
        "turn_bounds": [
            {"index": t.index, "prompt": t.prompt,
             "first_seq": t.first_seq, "last_seq": t.last_seq}
            for t in turns
        ],
        "score": passed / total,
        "passed": passed,
        "total": total,
        "critical_failures": [r["name"] for r in critical_failed],
        "cost_usd": round(cost, 4),
        "browser_calls": len(calls),
        "errors": errors,
        "checks": results,
    }
    (run_dir / "result.json").write_text(json.dumps(summary, indent=2),
                                         encoding="utf-8")
    return summary


# ---------- reporting ----------

def aggregate(case_name: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [r["score"] for r in runs]
    per_check: dict[str, dict[str, Any]] = {}
    for run in runs:
        for chk in run["checks"]:
            slot = per_check.setdefault(
                chk["name"], {"pass": 0, "fail": 0, "critical": chk.get("critical"),
                              "details": []}
            )
            slot["pass" if chk["ok"] else "fail"] += 1
            if not chk["ok"] and chk["detail"] not in slot["details"]:
                slot["details"].append(chk["detail"])
    return {
        "case": case_name,
        "runs": len(runs),
        "score_mean": round(sum(scores) / len(scores), 4),
        "score_min": round(min(scores), 4),
        "score_max": round(max(scores), 4),
        "flaky": len(set(round(s, 4) for s in scores)) > 1,
        "cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
        "errors": [e for r in runs for e in r["errors"]],
        "checks": per_check,
    }


def print_report(aggregates: list[dict[str, Any]],
                 baseline: dict[str, Any] | None) -> None:
    print()
    print("=" * 78)
    print(f"{'case':<28}{'score':>8}{'runs':>6}{'flaky':>7}{'delta':>9}{'cost':>10}")
    print("-" * 78)
    for agg in aggregates:
        delta = ""
        if baseline and agg["case"] in baseline.get("cases", {}):
            before = baseline["cases"][agg["case"]]["score_mean"]
            diff = agg["score_mean"] - before
            delta = f"{diff:+.2f}" if abs(diff) > 1e-9 else "  0.00"
        print(f"{agg['case']:<28}{agg['score_mean']:>8.2f}{agg['runs']:>6}"
              f"{'yes' if agg['flaky'] else '-':>7}{delta:>9}"
              f"{'$' + format(agg['cost_usd'], '.2f'):>10}")
    print("=" * 78)

    for agg in aggregates:
        failures = {k: v for k, v in agg["checks"].items() if v["fail"]}
        if not failures and not agg["errors"]:
            continue
        print(f"\n{agg['case']}:")
        for name, info in failures.items():
            mark = "CRITICAL" if info["critical"] else "fail"
            print(f"  [{mark}] {name}  ({info['fail']}/{agg['runs']} runs)")
            for detail in info["details"][:3]:
                print(f"        {detail}")
        for err in agg["errors"][:3]:
            print(f"  [error] {err}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case")
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--runs", type=int)
    ap.add_argument("--model", default=os.environ.get("EVAL_MODEL", "sonnet"))
    ap.add_argument("--plugin-dir", default=str(DEFAULT_PLUGIN_DIR))
    ap.add_argument("--threshold", type=float, default=None,
                    help="exit 1 if any case scores below this")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--no-compare", action="store_true")
    ap.add_argument("--mutant", default=None,
                    help="damage one SKILL.md instruction first (see mutants.toml) "
                         "to verify the suite actually detects its loss")
    ap.add_argument("--keep", type=int, default=10,
                    help="how many timestamped run dirs to keep")
    opts = ap.parse_args()

    plugin_dir = pathlib.Path(opts.plugin_dir)
    if not (plugin_dir / "skills" / "apply" / "SKILL.md").exists():
        print(f"error: no apply skill under {plugin_dir}", file=sys.stderr)
        return 2

    cases = load_cases(opts.case, opts.tag)
    if not cases:
        print("error: no cases matched", file=sys.stderr)
        return 2

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    root = RUNS / stamp
    root.mkdir(parents=True, exist_ok=True)

    mutant_conf = None
    if opts.mutant:
        plugin_dir, mutant_conf = build_mutant(
            plugin_dir, opts.mutant, root / f"_plugin-{opts.mutant}"
        )
        print(f"MUTANT {opts.mutant}: {mutant_conf['description']}")
        print(f"  expected to fail: {', '.join(mutant_conf['expect_failing'])}")

    baseline = None
    baseline_path = BASELINES / "latest.json"
    if baseline_path.exists() and not opts.no_compare:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    aggregates = []
    for case in cases:
        n = opts.runs or case.get("runs", 3)
        model = case.get("model", opts.model)
        print(f"\n--- {case['name']}  ({n} run(s), {model}) ---")
        run_results = []
        for i in range(1, n + 1):
            run_dir = root / case["name"] / f"run-{i}"
            run_dir.mkdir(parents=True, exist_ok=True)
            result = run_once(case, run_dir, model, plugin_dir)
            run_results.append(result)
            flag = " CRITICAL" if result["critical_failures"] else ""
            print(f"  run {i}: {result['passed']}/{result['total']} "
                  f"({result['score']:.0%}) "
                  f"{result['browser_calls']} browser calls "
                  f"${result['cost_usd']:.2f}{flag}")
        aggregates.append(aggregate(case["name"], run_results))

    report = {
        "timestamp": stamp,
        "mutant": opts.mutant,
        "model": opts.model,
        "plugin_dir": str(plugin_dir),
        "cases": {a["case"]: a for a in aggregates},
    }
    (root / "aggregate.json").write_text(json.dumps(report, indent=2),
                                         encoding="utf-8")
    print_report(aggregates, baseline)

    if mutant_conf is not None:
        # Inverted verdict: under a mutant, the named assertions MUST fail.
        failing = {name for agg in aggregates
                   for name, info in agg["checks"].items() if info["fail"]}
        missed = [n for n in mutant_conf["expect_failing"]
                  if n in {name for agg in aggregates for name in agg["checks"]}
                  and n not in failing]
        print()
        print(f"mutant {opts.mutant}: "
              f"{len(mutant_conf['expect_failing']) - len(missed)}/"
              f"{len(mutant_conf['expect_failing'])} expected failures detected")
        if missed:
            print(f"  NOT DETECTED: {', '.join(missed)}")
            print("  Those assertions do not actually protect the removed "
                  "instruction - tighten them.")
        # A mutant run never doubles as a regression check.
        return 1 if missed else 0

    if opts.save_baseline:
        BASELINES.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        (BASELINES / f"{stamp}.json").write_text(json.dumps(report, indent=2),
                                                 encoding="utf-8")
        print(f"baseline saved: {baseline_path}")

    # prune old run dirs
    old = sorted(RUNS.iterdir(), key=lambda p: p.name)[: -opts.keep or None]
    for path in old:
        if path.is_dir() and path.name != stamp:
            shutil.rmtree(path, ignore_errors=True)

    exit_code = 0
    for agg in aggregates:
        if any(v["fail"] and v["critical"] for v in agg["checks"].values()):
            exit_code = 1
        if opts.threshold is not None and agg["score_mean"] < opts.threshold:
            exit_code = 1
        if baseline and agg["case"] in baseline.get("cases", {}):
            before = baseline["cases"][agg["case"]]["score_mean"]
            if agg["score_mean"] < before - 0.05:
                print(f"REGRESSION: {agg['case']} {before:.2f} -> "
                      f"{agg['score_mean']:.2f}")
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
