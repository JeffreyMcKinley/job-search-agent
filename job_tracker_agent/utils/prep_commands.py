"""The prep pipeline commands: run, schema, validate, apply.

These own the contract with the /mass-apply prep skill. No sheet write depends
on the agent remembering to make one -- the agent produces a result file, this
validates it, re-checks the materials, and applies every change in one batch.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .prep_runner import (
    PREP_IDLE_TIMEOUT,
    PREP_MODEL,
    PREP_RETRY_BACKOFF,
    _reconstruct_from_sidecars,
    _run_agent,
    _transient_rejection,
)

if TYPE_CHECKING:
    from prep_models import PrepBatch


def cmd_prep(args: list[str]) -> None:
    """Prepare every job in 'to apply': run the agent, verify, update the sheet.

    The whole point of this command is that no sheet write depends on the agent
    remembering to make one. The agent produces a result file; Python validates
    it, re-checks the materials, and applies every change in one batch.
    """
    from bespoke import check_folder
    from config import JOBS_DIR, PREP_RUNS_DIR, VOICE_CARD
    from prep_models import PrepBatch, PrepStatus, reconcile
    from sheets import apply_prep_batch, connect, dedup_tab, read_tab

    limit = 0
    only_urls: list[str] = []
    timeout_override = 0
    no_apply = "--no-apply" in args
    dry_run = "--dry-run" in args

    model = PREP_MODEL
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        elif a == "--urls" and i + 1 < len(args):
            only_urls = [u.strip() for u in args[i + 1].split(",") if u.strip()]
        elif a == "--model" and i + 1 < len(args):
            model = args[i + 1]
        elif a == "--timeout" and i + 1 < len(args):
            timeout_override = int(args[i + 1])

    spreadsheet = connect()

    # Dedup here rather than trusting a skill step to do it first.
    # Skipped under --dry-run, which must not modify anything.
    if not dry_run:
        print("Deduplicating 'to apply'...")
        dedup = dedup_tab(spreadsheet, "to apply")
        if dedup["rows_removed"]:
            print(f"  Removed {dedup['rows_removed']} duplicate rows.")

    rows = read_tab(spreadsheet, "to apply")
    jobs = [
        {"url": r.record.posting_url, "company": r.record.company, "title": r.record.title}
        for r in rows
        if r.record.posting_url.strip()
    ]
    if only_urls:
        wanted = {u.rstrip("/").lower() for u in only_urls}
        jobs = [j for j in jobs if j["url"].rstrip("/").lower() in wanted]
    if limit:
        jobs = jobs[:limit]

    if not jobs:
        print(json.dumps({"success": True, "prepared": 0, "note": "nothing in 'to apply'"}, indent=2))
        return

    urls = [j["url"] for j in jobs]
    print(f"Preparing {len(urls)} job(s).")

    if dry_run:
        print(json.dumps({"dry_run": True, "urls": urls}, indent=2))
        return

    # --- run directory --------------------------------------------------
    PREP_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = PREP_RUNS_DIR / datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    urls_file = run_dir / "urls.json"
    schema_file = run_dir / "schema.json"
    out_file = run_dir / "result.json"

    urls_file.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    schema_file.write_text(json.dumps(PrepBatch.model_json_schema(), indent=2), encoding="utf-8")

    prompt = (
        f"/mass-apply prep --urls-file {urls_file} "
        f"--schema {schema_file} --out {out_file}"
        "\n\nHard requirements:"
        f"\n- Write the result envelope to exactly {out_file}."
        f"\n- Run `python main.py prep-validate {out_file}` and repair until it exits 0."
        "\n- Draft every resume, cover letter, and open-ended answer with"
        f" /unslop mimic in the {VOICE_CARD!r} voice, per the apply-prep skill's"
        " Writing standard section. Drafting time, not a cleanup pass."
        "\n- Cap every open-ended answer at two sentences. check-bespoke fails"
        " the folder otherwise; cut a fact rather than splicing sentences."
        "\n- Do not modify the Google Sheet; this process applies every sheet change itself."
        "\n- Do not open, fill, or submit any application form."
    )
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    total_budget = timeout_override or min(300 + 420 * len(urls), 4 * 60 * 60)
    print(f"Run directory: {run_dir}")
    print(f"Budget: {total_budget}s total, {PREP_IDLE_TIMEOUT}s idle.")

    # A turn-1 API rejection consumes nothing and clears on its own within
    # minutes, so retry it rather than surfacing a failure the user can only
    # respond to by running the same command again by hand.
    rc = _run_agent(prompt, run_dir, total_budget, PREP_IDLE_TIMEOUT, model)
    for attempt, backoff in enumerate(PREP_RETRY_BACKOFF, start=1):
        reason = _transient_rejection(run_dir)
        if not reason:
            break
        print(f"  Agent was refused before starting: {reason}")
        print(f"  Nothing was consumed. Retry {attempt} of "
              f"{len(PREP_RETRY_BACKOFF)} in {backoff}s.")
        # Keep the failed stream for diagnosis instead of overwriting it.
        failed = run_dir / f"stream.rejected-{attempt}.jsonl"
        try:
            (run_dir / "stream.jsonl").replace(failed)
        except OSError:
            pass
        time.sleep(backoff)
        rc = _run_agent(prompt, run_dir, total_budget, PREP_IDLE_TIMEOUT, model)

    # --- recover the result ---------------------------------------------
    raw = None
    if out_file.is_file():
        try:
            raw = json.loads(out_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  result.json is not valid JSON: {e}")
    if raw is None:
        print("  No usable result.json. Reconstructing from per-folder sidecars...")
        raw = _reconstruct_from_sidecars(urls)
        if raw is not None:
            out_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            print(f"  Recovered {len(raw['items'])} item(s) from sidecars.")

    if raw is None:
        print(json.dumps({
            "success": False, "error": "no_result",
            "agent_returncode": rc, "run_dir": str(run_dir),
            "note": "No sheet changes were made.",
        }, indent=2))
        sys.exit(2)

    try:
        batch = PrepBatch.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({
            "success": False, "error": "invalid_result", "run_dir": str(run_dir),
            "detail": str(e)[:3000], "note": "No sheet changes were made.",
        }, indent=2))
        sys.exit(2)

    batch, recon_errors = reconcile(batch, urls)
    if recon_errors:
        print(json.dumps({
            "success": False, "error": "result_does_not_match_request",
            "detail": recon_errors, "run_dir": str(run_dir),
        }, indent=2))
        sys.exit(2)

    # --- bespoke backstop -----------------------------------------------
    # The apply-prep skill runs this as its own gate; re-running it here means
    # the gate holds even if the skill skipped it.
    flagged: list[dict] = []
    for item in batch.items:
        if item.status is not PrepStatus.PREPARED or not item.folder_name:
            continue
        report = check_folder(JOBS_DIR / item.folder_name, item.company)
        if not report["passed"]:
            flagged.append(report)

    # A bespoke failure means our output was lazy, not that the posting is
    # dead, so these rows must stay in "to apply" rather than move anywhere.
    # Drop them from the batch and report them with their scores.
    if flagged:
        flagged_folders = {f["folder"] for f in flagged}
        batch.items = [
            i for i in batch.items
            if not (i.folder_name and i.folder_name in flagged_folders)
        ]

    if no_apply:
        print(json.dumps({
            "success": True, "applied": False, "run_dir": str(run_dir),
            "result": str(out_file), "items": len(batch.items),
            "bespoke_flagged": flagged,
        }, indent=2))
        return

    report = apply_prep_batch(spreadsheet, batch, urls)
    report["success"] = True
    report["run_dir"] = str(run_dir)
    report["bespoke_flagged"] = flagged
    print(json.dumps(report, indent=2))

    if flagged:
        sys.exit(2)


def cmd_prep_schema() -> None:
    """Print the JSON Schema for a prep result file.

    Generated from PrepBatch, so it can never drift from what prep-validate
    actually enforces.
    """
    from prep_models import PrepBatch

    print(json.dumps(PrepBatch.model_json_schema(), indent=2))


def _load_batch(path: str) -> tuple[PrepBatch | None, list[str]]:
    """Load and validate a result.json. Returns (batch_or_None, errors)."""
    import pathlib

    from prep_models import PrepBatch
    from pydantic import ValidationError

    p = pathlib.Path(path)
    if not p.is_file():
        return None, [f"result file not found: {p}"]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, [f"result file is not valid JSON: {e}"]
    try:
        return PrepBatch.model_validate(raw), []
    except ValidationError as e:
        return None, [str(e)]


def cmd_prep_validate(path: str) -> None:
    """Validate a prep result file. Exit 0 if usable, 2 if not.

    The prep skill runs this before finishing, so it gets a deterministic
    error string to repair against instead of guessing at the contract.
    """
    batch, errors = _load_batch(path)
    if errors or batch is None:
        print(json.dumps({"valid": False, "errors": errors}, indent=2))
        sys.exit(2)

    counts: dict[str, int] = {}
    for item in batch.items:
        key = item.status.value if item.status.value == "prepared" else (
            f"invalid/{item.invalid_reason.value}" if item.invalid_reason else "invalid"
        )
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({"valid": True, "items": len(batch.items), "breakdown": counts}, indent=2))


def cmd_prep_apply(path: str, requested: list[str] | None = None) -> None:
    """Apply a validated prep result to the sheet in one batch. Idempotent."""
    from prep_models import reconcile
    from sheets import apply_prep_batch, connect

    batch, errors = _load_batch(path)
    if errors or batch is None:
        print(json.dumps({"success": False, "errors": errors}, indent=2))
        sys.exit(2)

    urls = requested if requested else [i.url for i in batch.items]
    batch, recon_errors = reconcile(batch, urls)
    if recon_errors:
        print(json.dumps({"success": False, "errors": recon_errors}, indent=2))
        sys.exit(2)

    report = apply_prep_batch(connect(), batch, urls)
    report["success"] = True
    print(json.dumps(report, indent=2))
