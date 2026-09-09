"""Verify generated application materials are actually bespoke.

The measure is 5-gram word-shingle containment against the master resume.

Why not difflib.SequenceMatcher: its ratio is dominated by ordering, so a
resume with identical bullets in a different section order scores low. That is
precisely the near-duplicate this has to catch.

Why not single-token overlap: every resume for the same person shares the same
vocabulary, so it saturates near 1.0 and discriminates nothing.

Why containment rather than Jaccard against the master: the master is short
(427 tokens) relative to a tailored resume, and Jaccard would be depressed by
the length asymmetry rather than by genuine difference.

No cross-folder comparison. Two React/Node postings legitimately produce
similar resumes, and the false-positive cost outweighs the catch.
"""

from __future__ import annotations

import html
import re
import zipfile
from functools import lru_cache
from pathlib import Path

SHINGLE_N = 5

# Measured over 40 existing job folders: min 0.035, median 0.127, max 0.380.
# 0.55 sits well clear of the observed maximum.
MASTER_CONTAINMENT_LIMIT = 0.55

# The cover-letter skill's own stated rule.
COVER_LETTER_MIN_WORDS = 250
COVER_LETTER_MAX_WORDS = 350

# A tailored resume should be in the same size class as the master.
LENGTH_MIN_RATIO = 0.60
LENGTH_MAX_RATIO = 1.60

# A one-page resume must also be a full page. Measured as the gap from the
# baseline of the last line of text to the bottom edge, in points (72/inch).
# Observed over the Chrome-rendered corpus: median 74pt, and everything a
# reader would call well filled sits under 110pt. 108pt (1.5in) is the line.
RESUME_MAX_BOTTOM_GAP_PT = 108.0

_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"[a-z0-9]+")
_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}|\[[A-Z][a-z]+\]|TODO|XXX")


def docx_text(path: Path) -> str:
    """Extract text from a .docx without python-docx (not installed).

    A .docx is a zip; word/document.xml holds the body. Paragraph ends become
    newlines so word boundaries survive.
    """
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = xml.replace("</w:p>", "\n")
    return html.unescape(_TAG_RE.sub("", xml))


def tokens(text: str) -> list[str]:
    """Lowercase alphanumeric words, dropping very short tokens."""
    return [w for w in _WORD_RE.findall(text.lower()) if len(w) > 2]


def shingles(text: str, n: int = SHINGLE_N) -> set[str]:
    toks = tokens(text)
    if len(toks) < n:
        return set()
    return {" ".join(toks[i : i + n]) for i in range(len(toks) - n + 1)}


def containment(candidate: str, reference: str) -> float:
    """Fraction of the candidate's shingles that also appear in the reference."""
    cand = shingles(candidate)
    if not cand:
        return 0.0
    return len(cand & shingles(reference)) / len(cand)


@lru_cache(maxsize=1)
def master_resume_text() -> str:
    from config import MASTER_RESUME

    if not MASTER_RESUME.is_file():
        return ""
    return docx_text(MASTER_RESUME)


@lru_cache(maxsize=1)
def master_cover_letter_text() -> str:
    from config import MASTER_COVER_LETTER

    if not MASTER_COVER_LETTER.is_file():
        return ""
    if MASTER_COVER_LETTER.suffix.lower() == ".docx":
        return docx_text(MASTER_COVER_LETTER)
    return MASTER_COVER_LETTER.read_text(encoding="utf-8", errors="replace")


def company_core(company: str) -> str:
    """Reduce a parsed company string to the name worth searching for.

    posting.md headers sometimes carry a descriptive tail, e.g.
    "Algolia (AI Search platform; 30B+ weekly requests)". Only the leading
    name is useful for checking that a document actually names the employer.
    """
    core = re.split(r"[(,;|]", company, 1)[0].strip()
    # Trim a trailing legal suffix, anchored at the end so it can never eat
    # a substring of the real name ("Confiant" starts with "co").
    core = re.sub(r"\s+(inc|llc|ltd|corp|corporation|co)\.?$", "", core, flags=re.I)
    return core.strip(" .-")


def names_company(text: str, company: str) -> bool:
    """True if the text plausibly names the employer."""
    core = company_core(company)
    if not core:
        return True  # nothing to check against
    low = text.lower()
    if core.lower() in low:
        return True
    # Fall back to the first distinctive word ("Caris" for "Caris Life Sciences").
    first = core.split()[0] if core.split() else ""
    return len(first) >= 4 and first.lower() in low



def pdf_pages(path: Path) -> int:
    """Page count of a PDF, or -1 if it cannot be read."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001
        return -1


def pdf_bottom_gap_pt(path: Path) -> float:
    """Points between the baseline of the lowest text on page 1 and the page bottom.

    Every glyph run is placed by the text matrix `tm` inside the current
    graphics matrix `cm`, so neither alone gives a page coordinate. Chrome
    emits a single flipping `cm` and varies `tm`; the older renderer in this
    corpus does the opposite. Composing the two is what makes the number
    comparable across both.

    Returns -1.0 if the PDF cannot be read or carries no text.
    """
    try:
        from pypdf import PdfReader

        page = PdfReader(str(path)).pages[0]
        ys: list[float] = []

        def visit(text, cm, tm, font_dict, font_size):  # noqa: ANN001, ARG001
            if text.strip():
                ys.append(cm[1] * tm[4] + cm[3] * tm[5] + cm[5])

        page.extract_text(visitor_text=visit)
        return min(ys) if ys else -1.0
    except Exception:  # noqa: BLE001
        return -1.0


def check_one_page(folder: Path, stem: str, label: str) -> tuple[list[str], dict[str, float]]:
    """Both deliverables must export to exactly one page.

    Checked here as well as in make-pdf because the two answer different
    questions: make-pdf validates what it just rendered, this validates what is
    actually sitting in the folder -- including a PDF rendered before the rule
    existed, or one left behind by an earlier run.
    """
    failures: list[str] = []
    scores: dict[str, float] = {}
    matches = sorted(folder.glob(f"*{stem}*.pdf"))
    if not matches:
        return failures, scores
    pages = pdf_pages(matches[0])
    if pages < 0:
        failures.append(f"{label} PDF could not be read")
    else:
        scores[f"{label.replace(' ', '_')}_pages"] = pages
        if pages != 1:
            failures.append(
                f"{label} PDF is {pages} pages; it must be exactly 1. "
                "Shorten the content until it fits -- the font sizes and "
                "margins are fixed by the template and must not be changed."
            )
        elif label == "resume":
            gap = pdf_bottom_gap_pt(matches[0])
            if gap >= 0:
                scores["resume_bottom_gap_in"] = round(gap / 72.0, 2)
                if gap > RESUME_MAX_BOTTOM_GAP_PT:
                    failures.append(
                        f"resume PDF ends {gap / 72.0:.2f}in above the bottom of "
                        f"the page (limit {RESUME_MAX_BOTTOM_GAP_PT / 72.0:.2f}in). "
                        "One page means one full page -- add back the strongest "
                        "relevant material until it fills out. Do not pad with "
                        "irrelevant roles, and do not change the font sizes or "
                        "margins, which are fixed by the template."
                    )
    return failures, scores


ANSWER_SENTENCE_CAP = 2


def over_length_answers(plan: str) -> list[tuple[str, int]]:
    """Open-ended answers running past the sentence cap.

    Screening boxes are frequently a single line, and a long answer reads as
    one no matter how good it is. Only the first paragraph of a block counts:
    later paragraphs are internal prep notes for the person filling the form,
    not text that gets pasted in.
    """
    m = re.search(r"^## Open-ended answers.*$", plan, re.MULTILINE)
    if not m:
        return []
    rest = plan[m.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    body = rest[: nxt.start()] if nxt else rest

    over: list[tuple[str, int]] = []
    label = "(before the first question)"
    blocks: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in body.split("\n"):
        if line.startswith("**") and line.count("**") >= 2:
            label = line.strip().strip("*").strip()[:60]
            current = []
            blocks.append((label, current))
        elif current is not None:
            current.append(line)

    for label, lines in blocks:
        paragraphs = [p for p in "\n".join(lines).strip().split("\n\n") if p.strip()]
        if not paragraphs:
            continue
        answer = " ".join(paragraphs[0].split())
        if answer.lstrip().startswith(("-", "*", "|")):
            continue  # bullet lists and tables are not prose answers
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", answer) if s.strip()]
        if len(sentences) > ANSWER_SENTENCE_CAP:
            over.append((label, len(sentences)))
    return over


# --- volunteered gaps ---------------------------------------------------
#
# A cover letter sells. It never opens the candidate's own case for the
# defence. Every pattern below was lifted from a real generated letter in the
# corpus, where the model conceded a missing technology, a short tenure, or a
# shallow area before the reader had asked for any of it. The rule these
# enforce is apply-prep Step 3 and Step 6: advertise what he has done, never
# volunteer what he has not.
#
# Honest gaps still get recorded. They live in the application plan's
# "## Known gap to be ready for" section, which is internal prep and is never
# scanned here.
_GAP_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bI have (?:not|never) (?:shipped|used|built|worked|written|run|published|managed|led|maintained|owned|touched|done|set up)\b",
     "concedes a technology or task he has not done"),
    (r"\bI have no(?! doubt)\b", "concedes something he does not have"),
    (r"\bI (?:do not|don't|dont) have\b", "concedes something he does not have"),
    (r"\bwhat I have not\b", "frames the letter around what is missing"),
    (r"\b(?:the|one|two|three|another|a real|my only|any) (?:real |honest |obvious |big(?:gest)? )?gaps?\b",
     "names a gap outright"),
    (r"\bnam(?:e|es|ing) the gap\b", "names a gap outright"),
    (r"\bgap (?:first|plainly|here|worth naming)\b", "names a gap outright"),
    (r"\bto be (?:straight|plain|honest|clear|upfront|candid|direct|precise|blunt)\b",
     "concessive throat-clearing before a disclosure"),
    (r"\bthe honest part\b", "concessive throat-clearing before a disclosure"),
    (r"\b(?:say|saying|said|stated|state|naming|named) (?:this |that |it )?(?:plainly|up front|upfront|before you ask)\b",
     "concessive throat-clearing before a disclosure"),
    (r"\bWhere I do (?:not|n't) match\b", "a heading for the candidate's own weaknesses"),
    (r"\bnot senior[- ]level\b", "argues against his own seniority"),
    (r"\bno production (?:experience|Kubernetes|Terraform|Kafka|K8s)\b",
     "concedes a missing production technology"),
    (r"\bno (?:direct|formal|hands-on|prior|professional|clinical|real) experience\b",
     "concedes missing experience"),
    (r"\bno [A-Za-z.+#]+, no [A-Za-z.+#]+\b", "stacks up missing technologies"),
    (r"\b(?:cannot|can ?not|can't|is not something I can) claim\b", "argues against his own record"),
    (r"\b(?:than|rather than) claim(?:ing)?\b", "argues against his own record"),
    (r"\bclaim depth\b", "argues against his own record"),
    (r"\bwhere my depth (?:is|lies)\b", "ranks his own skills against each other"),
    (r"\b(?:is )?not on my (?:record|resume)\b", "concedes a missing line item"),
    (r"\bI (?:will not|won't|would not) overstate\b", "concessive throat-clearing"),
    (r"\bnot my strength\b", "argues against his own record"),
    (r"\bI lack\b", "concedes a shortfall"),
    (r"\b(?:I am|I'm) light on\b", "concedes a shortfall"),
    (r"\bis new to me\b", "concedes unfamiliarity"),
    (r"\bramp(?:ing)? (?:up )?on\b", "promises to learn instead of showing what he has done"),
    (r"\bagainst the (?:\w+) you ask\b", "measures himself short of the posting"),
    (r"\bexperience is (?:five|four|three|two|six|seven|eight|nine|ten|a few|only) months\b",
     "volunteers a short tenure"),
    (r"\bI would rather (?:tell you|say that|name)\b", "pre-emptive confession"),
    (r"\b(?:than|before) (?:it |somebody |someone |you )?(?:else )?(?:surface|surfaces|find|finds)\b",
     "pre-emptive confession"),
    (r"\bsurface in week two\b", "pre-emptive confession"),
)

_GAP_RES = tuple((re.compile(p, re.IGNORECASE), why) for p, why in _GAP_PATTERNS)


def volunteered_gaps(text: str) -> list[tuple[str, str]]:
    """Spans where the writing argues against the candidate.

    Returns `(quoted span with context, reason)` for each hit, deduplicated on
    the span. An empty list is a pass.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    flat = " ".join(text.split())
    for rx, why in _GAP_RES:
        for m in rx.finditer(flat):
            start = max(0, m.start() - 40)
            end = min(len(flat), m.end() + 40)
            span = flat[start:end].strip()
            key = m.group(0).lower()
            if key in seen:
                continue
            seen.add(key)
            found.append((span, why))
    return found


def open_ended_section(plan: str) -> str:
    """Just the submitted prose of the open-ended answers, or an empty string.

    Stops at the next `## ` heading, which keeps the internal
    "## Known gap to be ready for" notes out of any scan.
    """
    m = re.search(r"^## Open-ended answers.*$", plan, re.MULTILINE)
    if not m:
        return ""
    rest = plan[m.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def check_folder(folder: Path, company: str = "") -> dict:
    """Run every bespoke and structural check on one job folder.

    Returns a report dict with `passed` plus the scores, so a caller can show
    the numbers whether it passed or failed.
    """
    failures: list[str] = []
    warnings: list[str] = []
    scores: dict[str, float] = {}

    if not company:
        from scanner import parse_posting_md

        posting = folder / "posting.md"
        if posting.is_file():
            company = parse_posting_md(posting).get("company", "")

    # --- resume ---------------------------------------------------------
    resume_path = folder / "resume.md"
    if not resume_path.is_file():
        failures.append("resume.md is missing")
    else:
        resume = resume_path.read_text(encoding="utf-8", errors="replace")
        master = master_resume_text()

        if not master:
            warnings.append("master resume not readable; containment check skipped")
        else:
            score = containment(resume, master)
            scores["resume_vs_master"] = round(score, 4)
            if score > MASTER_CONTAINMENT_LIMIT:
                failures.append(
                    f"resume is {score:.0%} copied from the master resume "
                    f"(limit {MASTER_CONTAINMENT_LIMIT:.0%}); it needs to be "
                    "rewritten for this specific posting, not lightly edited"
                )

            n_res, n_mas = len(tokens(resume)), len(tokens(master))
            if n_mas:
                ratio = n_res / n_mas
                scores["length_ratio"] = round(ratio, 3)
                if not LENGTH_MIN_RATIO <= ratio <= LENGTH_MAX_RATIO:
                    failures.append(
                        f"resume is {n_res} tokens vs the master's {n_mas} "
                        f"(ratio {ratio:.2f}); expected between "
                        f"{LENGTH_MIN_RATIO} and {LENGTH_MAX_RATIO}"
                    )

        if company and not names_company(resume, company):
            warnings.append(f"resume never mentions the company ({company_core(company)})")
        if _PLACEHOLDER_RE.search(resume):
            failures.append("resume still contains placeholder text")

    # --- cover letter ---------------------------------------------------
    cl_path = folder / "cover-letter.md"
    if not cl_path.is_file():
        failures.append("cover-letter.md is missing")
    else:
        cl = cl_path.read_text(encoding="utf-8", errors="replace")
        words = len(cl.split())
        scores["cover_letter_words"] = words
        if not COVER_LETTER_MIN_WORDS <= words <= COVER_LETTER_MAX_WORDS:
            failures.append(
                f"cover letter is {words} words; expected "
                f"{COVER_LETTER_MIN_WORDS}-{COVER_LETTER_MAX_WORDS}"
            )
        if "Dear Hiring Manager" not in cl:
            warnings.append("cover letter does not open with 'Dear Hiring Manager,'")
        if "Regards" not in cl:
            warnings.append("cover letter does not close with 'Regards,'")
        if company and not names_company(cl, company):
            failures.append(f"cover letter never names the company ({company_core(company)})")
        if _PLACEHOLDER_RE.search(cl):
            failures.append("cover letter still contains placeholder text")

        gaps = volunteered_gaps(cl)
        scores["cover_letter_volunteered_gaps"] = float(len(gaps))
        for span, why in gaps:
            failures.append(
                f"cover letter {why}: \"{span}\" -- a cover letter advertises "
                "what he has done and never argues the other side "
                "(see apply-prep Step 3). Move the honest note to the "
                "application plan's '## Known gap to be ready for'."
            )

        master_cl = master_cover_letter_text()
        if master_cl:
            score = containment(cl, master_cl)
            scores["cover_letter_vs_master"] = round(score, 4)
            if score > MASTER_CONTAINMENT_LIMIT:
                failures.append(
                    f"cover letter is {score:.0%} copied from the master cover "
                    f"letter (limit {MASTER_CONTAINMENT_LIMIT:.0%})"
                )

    # --- application plan -----------------------------------------------
    plan_path = folder / "application-plan.md"
    if not plan_path.is_file():
        failures.append("application-plan.md is missing")
    else:
        plan = plan_path.read_text(encoding="utf-8", errors="replace")
        # Matched as line-start prefixes so a parenthetical suffix is fine
        # (the corpus uses both "(draft responses)" and "(humanized drafts)").
        for section in ("## Field answers", "## Open-ended answers"):
            if not re.search(rf"^{re.escape(section)}", plan, re.MULTILINE):
                failures.append(
                    f"application-plan.md is missing a '{section}' section "
                    "(a parenthetical suffix after it is fine)"
                )
        for span, why in volunteered_gaps(open_ended_section(plan)):
            warnings.append(
                f"open-ended answer {why}: \"{span}\" -- Step 6 says never "
                "volunteer a gap in submitted text"
            )
        long_answers = over_length_answers(plan)
        scores["open_ended_over_two_sentences"] = float(len(long_answers))
        for label, count in long_answers:
            failures.append(
                f"open-ended answer {label} runs {count} sentences; "
                "the cap is 2 (see apply-prep Step 6)"
            )

    # --- one page each -------------------------------------------------
    for stem, label in (("Resume", "resume"), ("Cover Letter", "cover letter")):
        f, sc = check_one_page(folder, stem, label)
        failures.extend(f)
        scores.update(sc)

    return {
        "folder": folder.name,
        "company": company,
        "passed": not failures,
        "scores": scores,
        "failures": failures,
        "warnings": warnings,
    }
