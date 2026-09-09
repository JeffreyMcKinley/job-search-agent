"""CLI wrapper around bespoke.py's per-folder materials check."""

from __future__ import annotations

import json
import sys


def cmd_check_bespoke(args: list[str]) -> None:
    """Verify a job folder's materials are bespoke rather than a lightly edited master.

    The apply-prep skill runs this as its own completion gate; prep re-runs it
    as a backstop so the gate holds even if the skill skipped it.
    Exit 0 if every folder passes, 2 otherwise.
    """
    from pathlib import Path

    from bespoke import check_folder
    from config import JOBS_DIR

    targets: list[Path] = []
    for a in args:
        if a.startswith("--"):
            continue
        p = Path(a)
        targets.append(p if p.is_absolute() else JOBS_DIR / a)

    if not targets:
        targets = sorted(d for d in JOBS_DIR.iterdir() if d.is_dir())

    reports = [check_folder(t) for t in targets if t.is_dir()]
    failed = [r for r in reports if not r["passed"]]

    if len(reports) == 1:
        print(json.dumps(reports[0], indent=2))
    else:
        print(json.dumps({
            "checked": len(reports),
            "passed": len(reports) - len(failed),
            "failed": len(failed),
            "failures": failed,
        }, indent=2))

    sys.exit(2 if failed else 0)
