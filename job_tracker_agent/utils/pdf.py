"""HTML to PDF rendering via headless Chrome, plus the checks around it.

The template signatures and the post-render verification live together because
neither half works alone: the template does not stop drift, and the converter
does not know what correct looks like.
"""

from __future__ import annotations

import json
import re
import sys

import config as _config

# Signatures every generated HTML must carry, proving it came from the template
# in templates/ rather than from whatever the model improvised this session.
RESUME_CSS_SIGNATURE = "@page { size: letter; margin: 0.5in; }"
COVER_CSS_SIGNATURE = "@page { size: letter; margin: 1in; }"
JOB_HEAD_SIGNATURE = ".job-head { display: flex; justify-content: space-between;"
DEFAULT_ACCENT = "#1f6f54"

# The candidate's own name and contact block come from ~/.jobtrackeragent/
# profile.toml, not from the template. CANDIDATE_NAME is re-exported here
# because it names the output PDF and is what the fidelity check looks for.
CANDIDATE_NAME = _config.CANDIDATE_NAME


def fill_identity(html: str) -> str:
    """Replace the identity placeholders with the configured candidate.

    Run before the template-fidelity checks and before the render, never after.
    The contact block's rendered height feeds the one-page check, so filling it
    late would let a resume pass the gate and then overflow onto page two.

    A link whose URL is not configured is removed rather than left as an empty
    anchor, so a candidate with no GitHub does not ship a dead one.
    """
    for placeholder, value in _config.PROFILE_PLACEHOLDERS.items():
        if not value and placeholder.endswith("_URL"):
            stem = placeholder[: -len("_URL")]
            # The separator sits between the two links, so it may be on either
            # side of the one being removed. Eat whichever is there, or the
            # contact line ends on a dangling middot.
            sep = r"(?:\s*&nbsp;&middot;&nbsp;)?"
            html = re.sub(
                rf'{sep}\s*<a href="\{{\{{{stem}_URL\}}\}}">'
                rf"\{{\{{{stem}_TEXT\}}\}}</a>{sep}",
                "",
                html,
            )
            continue
        html = html.replace("{{" + placeholder + "}}", value)
    return html


def find_chrome() -> str | None:
    """Locate a Chrome executable for headless printing."""
    import os
    import shutil
    from pathlib import Path

    env = os.environ.get("CHROME_PATH")
    if env and Path(env).is_file():
        return env

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return shutil.which("chrome") or shutil.which("chrome.exe")


def verify_pdf(path, max_pages: int | None) -> list[str]:
    """Chrome exits 0 on several failure modes, so check the artifact itself."""
    errs: list[str] = []
    if not path.is_file():
        return [f"no PDF produced at {path}"]
    size = path.stat().st_size
    if size < 5_000:
        errs.append(f"PDF is only {size} bytes; expected > 5000")
    with path.open("rb") as fh:
        if fh.read(4) != b"%PDF":
            errs.append("file does not start with %PDF")
    if not errs:
        try:
            from pypdf import PdfReader

            pages = len(PdfReader(str(path)).pages)
            if pages < 1:
                errs.append("PDF has no pages")
            elif max_pages and pages > max_pages:
                errs.append(
                    f"PDF is {pages} pages; expected at most {max_pages}. "
                    "Shorten the content until it fits -- cut the weakest "
                    "bullet or tighten a paragraph; do not shrink the font "
                    "or margins, which are fixed by the template."
                )
        except Exception as e:  # noqa: BLE001
            errs.append(f"pypdf could not open the PDF: {e}")
    return errs


def cmd_make_pdf(args: list[str]) -> None:
    """Render a job folder's HTML to PDF via headless Chrome.

    Pure HTML to PDF. The agent writes the HTML from templates/; this validates
    that it really did, then prints it. Both halves are needed: the template
    alone does not stop drift, and the converter alone does not know what
    correct looks like.
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from config import JOBS_DIR, PROJECT_DIR

    if not args:
        print("Usage: make-pdf <folder> [--kind resume|cover-letter] [--accent #RRGGBB]")
        sys.exit(1)

    folder_arg = args[0]
    kind = "resume"
    accent = DEFAULT_ACCENT
    for i, a in enumerate(args):
        if a == "--kind" and i + 1 < len(args):
            kind = args[i + 1]
        elif a == "--accent" and i + 1 < len(args):
            accent = args[i + 1]

    if kind not in ("resume", "cover-letter"):
        print(json.dumps({"success": False, "error": f"unknown kind: {kind}"}))
        sys.exit(1)

    folder = Path(folder_arg)
    if not folder.is_absolute():
        folder = JOBS_DIR / folder_arg
    if not folder.is_dir():
        print(json.dumps({"success": False, "error": f"folder not found: {folder}"}))
        sys.exit(1)

    src = folder / f"{kind}.html"
    if not src.is_file():
        print(json.dumps({
            "success": False,
            "error": f"{src.name} not found. Render it from "
                     f"job_tracker_agent/templates/{kind}.html.template first.",
        }))
        sys.exit(1)

    raw = src.read_text(encoding="utf-8", errors="replace")
    html = fill_identity(raw)

    # --- template fidelity ------------------------------------------------
    problems: list[str] = []
    if not CANDIDATE_NAME:
        problems.append(
            f"no candidate name configured — set [candidate] name in {_config.PROFILE_FILE}"
        )
    if kind == "resume":
        if RESUME_CSS_SIGNATURE not in html:
            problems.append(f"missing the template page rule: {RESUME_CSS_SIGNATURE}")
        if JOB_HEAD_SIGNATURE not in html:
            problems.append("missing the template .job-head flex rule")
    else:
        if COVER_CSS_SIGNATURE not in html:
            problems.append(f"missing the template page rule: {COVER_CSS_SIGNATURE}")
    if CANDIDATE_NAME not in html:
        problems.append(f"candidate name {CANDIDATE_NAME!r} does not appear in the document")
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", html)
    if leftover:
        problems.append(f"unreplaced template placeholders: {sorted(set(leftover))}")

    if problems:
        print(json.dumps({
            "success": False,
            "error": "html_does_not_match_template",
            "problems": problems,
            "template": str(PROJECT_DIR / "templates" / f"{kind}.html.template"),
        }, indent=2))
        sys.exit(1)

    if accent != DEFAULT_ACCENT:
        html = html.replace(DEFAULT_ACCENT, accent)

    chrome = find_chrome()
    if not chrome:
        print(json.dumps({
            "success": False,
            "error": "chrome_not_found",
            "hint": "Set CHROME_PATH to the chrome.exe location.",
        }))
        sys.exit(1)

    company = folder.name.rsplit("-", 3)[0].replace("-", " ").title()
    label = "Resume" if kind == "resume" else "Cover Letter"
    out = folder / f"{CANDIDATE_NAME} {label} - {company}.pdf"

    # Chrome needs its own profile dir; it contends with the live browser
    # profile lock otherwise and either fails or attaches to the running one.
    profile = Path(tempfile.mkdtemp(prefix="prep-chrome-"))
    # Chrome renders a file, not a string, so anything this function changed —
    # the identity block, a non-default accent — has to reach disk first. The
    # source file keeps its placeholders; the scratch copy is deleted below.
    render_src = src
    if html != raw:
        render_src = folder / f".{kind}.render.html"
        render_src.write_text(html, encoding="utf-8")

    try:
        proc = subprocess.run(
            [
                chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", f"--user-data-dir={profile}",
                "--virtual-time-budget=2000", "--no-pdf-header-footer",
                f"--print-to-pdf={out}", render_src.resolve().as_uri(),
            ],
            check=False, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"success": False, "error": "chrome_timeout"}))
        sys.exit(1)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        if render_src != src:
            render_src.unlink(missing_ok=True)

    # Both documents must be exactly one page. A recruiter sees page one;
    # anything past it is either never read or reads as padding.
    errs = verify_pdf(out, max_pages=1)
    if errs:
        print(json.dumps({
            "success": False, "error": "pdf_verification_failed",
            "problems": errs, "chrome_returncode": proc.returncode,
            "chrome_stderr": (proc.stderr or "")[-500:],
        }, indent=2))
        sys.exit(1)

    print(json.dumps({
        "success": True, "kind": kind, "pdf": out.name,
        "path": str(out), "bytes": out.stat().st_size,
    }, indent=2))
