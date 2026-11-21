"""Driving one `claude -p` prep run and making sense of what came back.

Everything here is about the subprocess: budgets, stream tee, progress lines,
transient-rejection detection, and rebuilding a result from sidecars when the
agent died before writing the envelope. Nothing here touches the sheet.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

# Overridable with `prep --model`. Empty means the CLI default.
PREP_MODEL = ""
# Kill the agent if no stream output arrives for this long. This is the
# budget that actually catches a hang, as opposed to a slow run.
PREP_IDLE_TIMEOUT = 900
# Waits between retries of a turn-1 API rejection. Spread out rather than
# tight, because what clears the refusal is the usage window recovering, and
# hammering it just burns the headroom that recovery produces.
PREP_RETRY_BACKOFF = (60, 180, 420)
# How long `claude -p` waits on still-running background tasks before it gives
# up and terminates. The default 600s is far shorter than a full batch's PDF
# rendering tail, and hitting it killed the agent before it could write the
# result envelope. 0 means wait indefinitely, which is safe here because the
# total and idle budgets below are the guards that actually catch a hang.
PREP_BG_WAIT_CEILING_MS = "0"


def _reconstruct_from_sidecars(requested: list[str]) -> dict | None:
    """Rebuild a result envelope from per-folder prep-result.json files.

    Recovery path for when the agent died before writing the envelope. The
    sidecars are written per job, so most of a long run survives.
    """
    from config import JOBS_DIR
    from sheets import normalize_url

    if not JOBS_DIR.exists():
        return None

    wanted = {normalize_url(u) for u in requested}
    items: list[dict] = []
    for folder in JOBS_DIR.iterdir():
        sidecar = folder / "prep-result.json"
        if not sidecar.is_file():
            continue
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if normalize_url(data.get("url", "")) in wanted:
            data.setdefault("folder_name", folder.name)
            items.append(data)

    if not items:
        return None
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "items": items,
    }


def _run_agent(prompt: str, run_dir, total_budget: int, idle_budget: int,
               model: str = PREP_MODEL) -> int:
    """Run one `claude -p` and tee its stream-json to disk. Returns the exit code.

    subprocess.run(timeout=) is the wrong shape here: a twenty-job run can
    legitimately take an hour, so a single fixed timeout is either too short for
    the good case or useless for the hung case. Two budgets are enforced -- a
    total ceiling, and an idle timeout on new stream output, which is the one
    that actually catches a hang.
    """
    import os
    import subprocess
    import threading
    import time

    from config import PROFICIENTLY_DIR, PROJECT_DIR, UNSLOP_SKILL_DIR

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        # A -p run gets no browser tools unless --chrome is passed; without it
        # the apply-prep fetch ladder loses its last tier and a JS-rendered or
        # bot-walled posting can only come back blocked. The tools attach to a
        # browser that is already running -- this does not launch one.
        "--chrome",
        # Reading a posting is the only thing Chrome is for here. Filling or
        # submitting a form belongs to `mass-apply fill`, so those three stay
        # denied even though the browser is now reachable.
        "--disallowed-tools",
        "mcp__claude-in-chrome__form_input,"  # noqa: ISC004
        "mcp__claude-in-chrome__file_upload,"
        "mcp__claude-in-chrome__computer",
        "--add-dir", str(PROFICIENTLY_DIR),
        # apply-prep reads the unslop contract files and runs its gate
        # scripts from here; the skill directory is outside the project.
        "--add-dir", str(UNSLOP_SKILL_DIR),
    ]
    if model:
        cmd += ["--model", model]

    stream_path = run_dir / "stream.jsonl"
    stderr_path = run_dir / "stderr.log"
    last_output = [time.monotonic()]
    started = time.monotonic()

    # An ANTHROPIC_API_KEY in the environment — from a shell profile, or from a
    # .env that some other tool loaded — is stripped before the child starts.
    # Nothing in this project reads it, but the CLI prefers an API key over the
    # Claude Code subscription. That silently bills the API account instead of
    # the plan and
    # fails outright with "Credit balance is too low" once the key has no funds
    # -- while `claude` run by hand, with no such key in scope, works fine.
    # It also disables claude.ai connectors, which is why an inherited-key run
    # reports 0 MCP servers and 28 tools against 67 for a subscription run.
    child_env = {
        k: v for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    }
    child_env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = PREP_BG_WAIT_CEILING_MS

    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT_DIR.parent), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    def pump() -> None:
        with stream_path.open("w", encoding="utf-8") as fh:
            assert proc.stdout is not None
            for line in proc.stdout:
                fh.write(line)
                fh.flush()
                last_output[0] = time.monotonic()
                _report_progress(line)

    def pump_err() -> None:
        """Drain stderr to disk.

        This thread is not optional. stderr is a pipe, so leaving it unread
        lets the child block once the OS buffer fills -- a hang with no
        diagnostic. It is also the only place a CLI-level failure (bad flag,
        missing binary, auth problem) is ever reported, since those never
        reach the stream-json stdout.
        """
        with stderr_path.open("w", encoding="utf-8") as fh:
            assert proc.stderr is not None
            for line in proc.stderr:
                fh.write(line)
                fh.flush()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    t_err = threading.Thread(target=pump_err, daemon=True)
    t_err.start()

    while proc.poll() is None:
        time.sleep(2)
        now = time.monotonic()
        if now - started > total_budget:
            print(f"  Total budget of {total_budget}s exceeded. Stopping the agent.")
            proc.kill()
            break
        if now - last_output[0] > idle_budget:
            print(f"  No output for {idle_budget}s. Stopping the agent.")
            proc.kill()
            break

    t.join(timeout=10)
    t_err.join(timeout=10)

    err_tail = ""
    if stderr_path.is_file():
        err_tail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    if err_tail:
        # The console here is cp1252; the CLI emits U+26A0 and friends. Printing
        # them raw kills the run with a UnicodeEncodeError at the exact moment
        # it is trying to report a different failure.
        print("  agent stderr: " + _console_safe(err_tail[-500:]))

    return proc.returncode if proc.returncode is not None else -1


def _console_safe(text: str) -> str:
    """Make text printable on whatever encoding stdout actually has."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(enc, errors="replace").decode(enc, errors="replace")


def _transient_rejection(run_dir) -> str:
    """Return the error text if the run died on a turn-1 API rejection.

    The signature is specific: the very first request is refused, so the run
    ends with num_turns == 1, is_error true, an api_error terminal reason, and
    zero tokens consumed. Observed repeatedly as a 400 'Credit balance is too
    low' when the subscription usage window has no headroom at that instant --
    the identical command succeeds minutes later. Distinguishing this from a
    real failure matters because retrying costs nothing when it is transient
    and is pointless when it is not.
    """
    stream_path = run_dir / "stream.jsonl"
    if not stream_path.is_file():
        return ""
    last_result = None
    for line in stream_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            last_result = event
    if not last_result:
        return ""
    if not last_result.get("is_error"):
        return ""
    if last_result.get("terminal_reason") != "api_error":
        return ""
    if last_result.get("num_turns", 0) > 1:
        return ""
    # Any real consumption means the agent actually started working; a retry
    # would then duplicate effort rather than recover from a refusal.
    if (last_result.get("total_cost_usd") or 0) > 0:
        return ""
    return str(last_result.get("result") or "api error")


def _report_progress(line: str) -> None:
    """Print a short progress line so an hour-long run is not a black box."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if event.get("type") == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                if name == "Task":
                    desc = (block.get("input", {}) or {}).get("description", "")
                    print(f"  subagent started: {desc}")
                elif name == "Bash":
                    cmd = (block.get("input", {}) or {}).get("command", "")
                    if "make-pdf" in cmd or "check-bespoke" in cmd or "prep-validate" in cmd:
                        print(f"  {cmd.split('main.py')[-1].strip()[:70]}")
    elif event.get("type") == "result":
        cost = event.get("total_cost_usd")
        turns = event.get("num_turns")
        print(f"  agent finished: {turns} turns"
              + (f", ${cost:.2f}" if isinstance(cost, (int, float)) else ""))
