"""Re-run assertions against runs that already happened.

Assertions get edited far more often than the agent's behavior changes, and a
full suite costs real money. This replays stored transcripts and mock logs, so
tightening a check is free.

    python tests/rescore.py                       # newest run dir, every case
    python tests/rescore.py --run runs/20260827-212058
    python tests/rescore.py --case workday_multistep --verbose
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from asserts import Context, Turn, run_checks  # noqa: E402
from runner import CASES, RUNS, aggregate, parse_stream, print_report, read_calls  # noqa: E402


def latest_run() -> pathlib.Path | None:
    if not RUNS.exists():
        return None
    dirs = sorted((p for p in RUNS.iterdir() if p.is_dir()), key=lambda p: p.name)
    return dirs[-1] if dirs else None


def rescore_run_dir(run_dir: pathlib.Path, case: dict) -> dict:
    calls = read_calls(run_dir / "calls.jsonl")
    stored = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    bounds = {b["index"]: b for b in stored.get("turn_bounds", [])}
    if not bounds:
        # Runs recorded before turn_bounds existed. Turn-scoped checks
        # (calls_in_turn, not_clicked before_turn) degrade to whole-run scope and
        # will read as passing whether or not they really did.
        print(f"    warning: {run_dir} has no turn_bounds; turn-scoped checks "
              f"are not meaningful for it", file=sys.stderr)

    turns: list[Turn] = []
    for path in sorted(run_dir.glob("turn-*.jsonl"),
                       key=lambda p: int(p.stem.split("-")[1])):
        index = int(path.stem.split("-")[1])
        text, tool_uses, _ = parse_stream(path)
        bound = bounds.get(index, {})
        turns.append(Turn(index, bound.get("prompt", ""), text, tool_uses,
                          bound.get("first_seq", 1),
                          bound.get("last_seq", len(calls))))

    ctx = Context(calls, turns, run_dir / "cwd")
    results = run_checks(ctx, case.get("asserts", []))
    passed = sum(1 for r in results if r["ok"])
    total = len(results) or 1
    return {
        "case": case["name"],
        "score": passed / total,
        "passed": passed,
        "total": total,
        "critical_failures": [r["name"] for r in results
                              if not r["ok"] and r.get("critical")],
        "cost_usd": stored.get("cost_usd", 0.0),
        "browser_calls": len(calls),
        "errors": stored.get("errors", []),
        "checks": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None, help="run dir (default: newest)")
    ap.add_argument("--case", default=None)
    ap.add_argument("--verbose", action="store_true",
                    help="print every check, not just failures")
    opts = ap.parse_args()

    root = pathlib.Path(opts.run) if opts.run else latest_run()
    if root is None or not root.exists():
        print("error: no run directory to rescore", file=sys.stderr)
        return 2

    aggregates = []
    for case_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        name = case_dir.name
        if opts.case and name != opts.case:
            continue
        case_path = CASES / f"{name}.toml"
        if not case_path.exists():
            print(f"skip {name}: no case file", file=sys.stderr)
            continue
        case = tomllib.loads(case_path.read_text(encoding="utf-8"))
        case.setdefault("name", name)

        run_results = []
        for run_dir in sorted(case_dir.glob("run-*")):
            if not (run_dir / "result.json").exists():
                continue
            result = rescore_run_dir(run_dir, case)
            run_results.append(result)
            print(f"  {name} {run_dir.name}: {result['passed']}/{result['total']} "
                  f"({result['score']:.0%})")
            if opts.verbose:
                for chk in result["checks"]:
                    mark = "ok  " if chk["ok"] else "FAIL"
                    print(f"      [{mark}] {chk['name']}: {chk['detail']}")
        if run_results:
            aggregates.append(aggregate(name, run_results))

    if not aggregates:
        print("error: nothing to rescore", file=sys.stderr)
        return 2
    print_report(aggregates, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
