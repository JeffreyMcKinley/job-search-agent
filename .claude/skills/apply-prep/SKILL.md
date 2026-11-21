---
name: apply-prep
description: "Prepare one job application offline: fetch the posting, tailor a resume, write a cover letter, plan the form answers, render PDFs. All prose is drafted in the candidate's taught voice with /unslop mimic. No browser forms, no submitting."
argument-hint: "<job posting URL>"
---

# Apply Prep

Prepare the materials for exactly **one** job application. No form filling, no
submitting. Fan-out across many URLs is the `mass-apply prep` skill's job, not
this one.

This is the offline half of `proficiently:apply`. That skill interleaves browser
work from its Step 3 onward, which makes it non-reproducible in a headless
batch. Two of its steps depend on a live form, and this skill resolves both
couplings explicitly — see Steps 3 and 6.

Paths below use `TRACKER` for `D:\Code\HelperAgents\job_tracker_agent`,
`PY` for `D:\Code\HelperAgents\venv\Scripts\python.exe`,
`UNSLOP` for `~\.claude\skills\unslop`, `VOICE` for
`D:\Code\HelperAgents\.unslop\voice\jeff`, and `FACTS` for
`D:\Code\HelperAgents\.personal\candidate-facts.md` (gitignored; the README
explains how to write it). Read `FACTS` before drafting anything: it names the
candidate's roles and headline results, which this skill refers to but does not
repeat.

---

## Step 0: Load the candidate's data

Read all three, every run:

- `~/.proficiently/profile.md` — work history. The source of truth for anything
  not on the master resume.
- `~/.proficiently/preferences.md` — locations, per-location comp floors,
  must-haves, dealbreakers.
- `~/.proficiently/application-data.md` — identity, standard answers, EEO, and
  a **"Custom Answers"** section.

**Read the Custom Answers section in full.** It is a read-and-append cache that
has grown to roughly 40 entries, and it carries two kinds of load-bearing
information that appear nowhere else:

- **Operational warnings**, e.g. Greenhouse forms auto-populating EEO fields
  from a resume parse, which must be re-verified rather than trusted.
- **Negative capability notes**, e.g. no production Kubernetes, Terraform, or
  gRPC. These are things the candidate must **not** be described as having.
  Violating one produces a resume that misrepresents them.

If a new standing answer is established during this run, append it there.

## Writing standard: every generated document is drafted with `/unslop mimic`

Every word this skill writes for a human reader is drafted in the candidate's
taught voice using `/unslop mimic`. That covers two documents:

- `cover-letter.md`
- `application-plan.md` — the `## Open-ended answers` prose

`resume.md` is not voice-drafted. Its summary has its own fixed style and its
bullets follow `tailor-resume`; see Step 4.

**Draft in voice; do not write freehand and clean up afterward.** A generic
draft run through a cleanup pass keeps the shape of a generic draft: the windup
survives, the cadence stays uniform, and the pass can only delete. The card is
cheap to load and it is what makes the three documents read like one person.

### Load the card

Read `UNSLOP/references/commands/mimic.md` for the flow and
`UNSLOP/references/core-contract.md` for the behavior contract. The core
contract is authoritative and outranks every preset. **Pass no `--preset`** —
the voice card supplies the voice, and a preset would fight it.

Then read `VOICE/card.md` once per run for the always-on core: median sentence
18 words, IQR 10, moderately bursty, contraction rate 0.003, never
parentheticals, openers `i`, `the`, `we`, `this`, `my`.

Load only the situation sheets the document at hand needs. The card is layered
so drafting stays cheap; reading all ten sheets wastes the saving.

| Document | Sheets |
|---|---|
| `cover-letter.md` | `card/openings.md`, `card/argument.md`, `card/closings.md`, `card/addressing-reader.md` |
| Open-ended answers | `card/anecdote.md`, `card/explaining-technical.md`, `card/numbers-data.md` |

**Never load `card/hedging-uncertainty.md` for application text.** It is
measured from personal essays, where hedging is a real habit. A screening
answer that imports it violates Step 6's voice rules, and Step 6 wins.

### What the card can and cannot do

The card models rhythm, punctuation habits, openers, and sentence shape. It is
not a content source. It never supplies an anecdote, a number, a scope, or a
claim — those come only from `profile.md`, the master resume, and
`application-data.md`.

Precedence, highest first, when two of these disagree:

1. **Step 0's negative capability notes.** Absolute. No voice match is worth a
   claim the candidate cannot make.
2. **Accuracy.** A rhythm fix that changes a number, a technology, a role, or
   an ownership boundary is reverted, not tuned.
3. **Step 6's voice rules.** No hedges, no concessive openers, no volunteered
   gaps, lead with the strongest true thing.
4. **The voice card.** Everything the first three leave open.

### The mimic rule

A draft that scores well on voice but trips a removal gate is **rejected**.
Voice never buys an exemption from the gates. Step 7 runs them.

## Step 1: Resolve the job folder

Check `~/.proficiently/jobs/` for a folder whose `posting.md` already matches
this URL:

```bash
PY TRACKER/main.py fetch-job "<url>"
```

If a folder exists, reuse it — re-runs are then idempotent and cheap. Otherwise
the folder is `~/.proficiently/jobs/[company-slug]-[YYYY-MM-DD]/`, created in
Step 2. Add a short disambiguator when a slug+date pair is already taken
(`baseten-role2-2026-08-02`, `amex-26010289-2026-07-28`).

## Step 2: Fetch the posting

### First, triage a LinkedIn URL

A `linkedin.com/jobs/view/...` URL is not a posting. It is a wrapper around one
of two different things, and they need opposite handling. Determine which
before you fetch, because for one of them the URL you were given is the wrong
URL to prep against.

Read the job id off the end of the URL and fetch LinkedIn's guest view of it:

```bash
curl -s -A "Mozilla/5.0" "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<jobId>" | grep -o 'apply-link[-_a-z]*' | sort -u
```

Read the result:

- **`offsite`** (e.g. `apply-link-offsite`) — the employer's own ATS owns this
  application. LinkedIn only advertises it. Resolve the real URL, below.
- **`onsite`** (e.g. `apply-link-onsite`, `apply-link-simple_onsite`) — Easy
  Apply. LinkedIn owns the form. Change nothing: prep against the LinkedIn URL
  exactly as given, and leave `resolved_url` empty in Step 9.
- **no marker at all** — usually a dead posting. Check the page for "no longer
  accepting applications" and classify it `expired` per rung 4. If the page
  looks alive, treat it as `onsite` and carry on.

#### Resolving an offsite URL

The guest view never contains the employer's URL; it wraps the apply button in
a signup redirect. So get it from Chrome, where the session is logged in:
`tabs_create` → `navigate` to the LinkedIn URL → `read_page`, and read the
Apply button's target. That target is the real posting.

Failing that, find the posting on the employer's own site from the company and
title you now have, and confirm the title and location match before trusting
it. A near-miss on a similarly named role is worse than no match — if you
cannot confirm it is the same job, do not guess. Prep against the LinkedIn URL
and say so in `notes`.

Once you have the real URL, **prep against it**: run it up the ladder below,
slug the folder from it, and put it in `posting.md` under `**URL:**`. Two rules:

- **`url` in `prep-result.json` stays the LinkedIn URL you were handed.** It is
  what matches the spreadsheet row. Changing it fails the whole batch with
  `result contains a URL that was never requested`.
- **The employer URL goes in `resolved_url`.** The sheet writer moves it into
  the row's Posting URL when the row moves to "ready to apply", so the fill
  phase opens the employer's form and the LinkedIn wrapper is not applied to
  twice.

### Then fetch the posting

Fetch whichever URL Step 2's triage settled on: the employer's for an offsite
job, the LinkedIn one for Easy Apply or any non-LinkedIn URL.

Climb this ladder in order. Stop at the first rung that yields a real posting.

1. **`PY TRACKER/main.py fetch-job "<url>"`** — cheap company, title, and ATS
   from the URL pattern plus the HTML title. Read the **`fetched`** field, not
   `success`: `fetched: false` means no page body was retrieved, so keep
   climbing. (`success` can be true off the URL pattern alone.)
2. **WebFetch.** Treat it as a failure — not a success — unless the result is
   over ~800 characters **and** contains job-description signal (requirements,
   responsibilities, qualifications). A 200 response carrying a JavaScript
   shell or a Cloudflare interstitial is a failure.
3. **Claude in Chrome**: `tabs_context` → `tabs_create` → `navigate` →
   `get_page_text`. For Greenhouse and Lever embeds, pull the iframe tokens the
   way `proficiently:apply` Step 3 does.
4. **Classify the failure** if all three rungs fail:
   - HTTP 404/410, or text matching "no longer accepting applications", "this
     job is closed", "position has been filled" → `expired` or `not_found`
   - 403/429 or a bot wall that survives Chrome → `blocked`
   - anything else → `fetch_failed`

Record every rung attempted and the exact error seen. That text becomes the
`notes` field in Step 9, and it is validated for length — a bare "failed" is
rejected.

Write `posting.md` in **exactly** this format. `scanner.parse_posting_md` reads
it, and `prepare-move` and the batch sheet writer both depend on that parse. A
malformed header silently breaks the link between the sheet row and the folder:

```markdown
# <Job Title> - <Company>

**URL:** <the posting URL>
**Company:** <company>
**Location:** <location>

## Requirements
## Technical Tools
## Responsibilities
## Fit Assessment
```

Put the **title first and the company second**. Always include the explicit
`**Company:**` field — it overrides header parsing and removes all ambiguity.

## Step 3: Always write a cover letter

**Resolved coupling.** `proficiently:apply` generates a cover letter only if its
Step 3 browser scout found a cover-letter field on the live form. That signal
does not exist offline, so this skill does not try to guess it.

Always generate one. An unused cover letter costs one file; a missing one
blocks a fill session later.

Use /proficiently:cover-letter as the main skill to guied this process. The
steps below expand on the content in the /proficiently:cover-letter skill,
that skill forms the basis of all further directions.

### The letter argues one thing: hire this person

A cover letter is not a fit report and it is not a self-assessment. It is the
one document in the packet whose entire job is to make the reader want the
interview. It answers three questions and nothing else:

1. **What has he built that this team needs?** Two or three pieces of real
   work, each with a number, each chosen because this posting asks for it.
2. **Why is he the right person for this job?** The judgement behind the work,
   not only the work itself: the call he made, the disagreement he resolved,
   the system he owned end to end.
3. **What does the team get by hiring him?** Concrete value, stated as fact.

### The opening paragraph introduces him before the evidence starts

Set by Jeffrey on 2026-09-14, following Cornell University's cover letter
guidance. The first paragraph lays the foundation for the letter and contains
all four of these, in a few sentences:

1. **The position** he is applying for, by its posted title.
2. **The employer** or department, by name.
3. **How he learned about the opening**: the job board, a recruiter who reached
   out, a referral. Use what is actually known; a batch-prepped posting from a
   board is "I found the opening on <board>" only when the board is known.
   Never invent a referral.
4. **A brief statement about himself**: the kind of engineer he is and his core
   stack, in one sentence. No metrics here; those belong to the evidence
   paragraphs that follow.

Achievements start in paragraph two. Do not open the letter on a project, a
number, or a result.

Close by naming what draws him to this company in particular. Enthusiasm is
part of the argument, not decoration on it. He wants these jobs, and a letter
that reads as neutral loses to one that does not.

### Every Step 6 content rule applies here in full

Step 6's section **"Answer with confidence, no qualifiers"** is written for
open-ended screening answers. **All of it governs the cover letter too.** The
letter is where those rules get broken most often, because a letter has room
to ramble and a screening box does not. Re-read that section before drafting.
The parts that matter most here:

- **Never volunteer a gap.** Not in the opening, not in a "two things to be
  straight about" paragraph, not as a closing flourish about naming things
  before someone else finds them. Nobody asked.
- **No concessive openers, and no concessive paragraphs.** A letter that
  spends a paragraph on what he has not done has spent a quarter of its words
  arguing the other side of the case.
- **Lead with the strongest true thing.** Once the opening paragraph has done
  its four jobs, the first evidence paragraph names work and result. Never
  wind up, and never open on a caveat.
- **Cut every hedge.** Step 6's list is the list.
- **Honesty outranks tone, and there is still no conflict.** Step 0's negative
  capability notes stay absolute. Confidence means choosing the strongest true
  fact and stating it plainly. It never means inflating one. If a paragraph
  only works because a claim grew, the claim is wrong — go find the real work.

**Where the honest gap goes.** Into `application-plan.md` under
`## Known gap to be ready for`, per Step 6. That section is internal prep, it
is never submitted, and it is where a real mismatch gets recorded in full so
he can answer it if a human raises it. Recording it there is the honest move.
Printing it in the letter is not more honest, it is only worse advocacy.

### Banned moves, every one taken from a real generated letter

These came out of this pipeline before the rule existed. None may appear:

| Do not write | Write instead |
|---|---|
| "The gap first, since it is the first thing you will check." | The four-part opening, then the work this posting most wants. |
| "Two things to be straight about." | Nothing. Delete the paragraph. |
| "I have not shipped Vue 3 professionally." | "React is my daily frontend framework," and stop. |
| "That is not senior-level Java." | Name the Java work he did and what it ran. |
| "I have no production experience with gRPC." | Say nothing about gRPC. |
| "Where I do not match: ..." | Delete. |
| "I would rather tell you now than have it surface in week two." | Delete. |
| "I would ramp on Kubernetes quickly." | Name the infrastructure he has actually run. |
| "I have seven years and four months against the eight you ask for." | "Seven years owning production systems," full stop. |
| "a habit of naming the gap before somebody else finds it" | A closing on what the team gets. |

**Adjacent experience is stated positively or left out.** When the posting
names a technology he has not used, the move is to name the closest real work
as its own accomplishment and stop there. "My backends are Go, Node, and
TypeScript" is a strong sentence. "My backends are Go, Node, and TypeScript,
and I have not used Django" is a weak sentence carrying the same information
plus a reason to reject him. If the closest real work is not close enough to
earn a sentence, leave the technology out of the letter and let the resume and
the interview handle it.

### Drafting

Draft it with `/unslop mimic` per the writing standard above, loading
`card/openings.md`, `card/argument.md`, `card/closings.md`, and
`card/addressing-reader.md`.

The fixed parts of the letter are not the card's to restyle. It opens
`Dear Hiring Manager,` and closes `Regards,` regardless of what
`card/openings.md` and `card/closings.md` measure — those sheets shape the
first and last *paragraph*, not the salutation and sign-off. `card/openings.md`
says to open cold on a concrete fact; for a cover letter that governs rhythm
only. The four-part opening above decides the first paragraph's content.

### The gate

`check-bespoke` scans `cover-letter.md` with `bespoke.volunteered_gaps` and
**fails the folder on any hit**, quoting the offending span and naming the
reason. It runs the same scan over the plan's `## Open-ended answers` prose as
a warning. Check it while drafting rather than discovering it at Step 8:

```bash
PY -c "import sys; sys.path.insert(0, r'TRACKER'); import bespoke, pathlib; \
print(bespoke.volunteered_gaps(pathlib.Path(r'<letter path>').read_text(encoding='utf-8')))"
```

An empty list passes. Fix a hit by **deleting the concession**, never by
rewording it into a softer concession. If deleting it leaves the letter short
of 250 words, that letter was carrying a paragraph of the wrong argument.
Replace it with another piece of real work.

## Step 4: Tailor the resume

Follow `proficiently:tailor-resume`, skipping its Step 1 (the posting is
already fetched). Keep all of its substance, in particular:

- The **mandatory Step 3b critique-and-rewrite pass**.
- The strict accuracy rules: only what is on the master resume or in
  `profile.md`; never infer business model, scope, or partnerships.
- The writing rules: short sentences, no em dashes, no filler phrases, no
  stacked adjectives, summary of 2-3 sentences each under 20 words.
- The voice rules in Step 6: no hedges, no qualifiers, no apologetic framing.

These rules are what keep Step 8's check passing. A resume that lightly edits
the master will be rejected there and have to be rewritten anyway.

### The summary synthesizes the whole career; it never restates a bullet

The summary is a two-to-three sentence portrait of what kind of engineer he is
across **all** of his experience, angled at this posting. It is not a highlight
reel. A number, project, or accomplishment that already appears as a bullet
below does not go in the summary — the reader is about to see it, and repeating
it wastes the most-read lines on the page. That covers the
headline results in `FACTS` and every other bullet.

Style, set by Jeffrey on 2026-09-14. This is the model:

> Full-stack engineer specializing in TypeScript with React and Node. Builds
> tools quickly with the customer and developer experiences in mind. Works with
> clients to ship MVPs and architects projects from concept to customer.

- **No "I", implied subject.** Sentence one is a noun phrase naming the role
  and core stack. Later sentences open on a present-tense verb ("Builds",
  "Works", "Owns").
- **Describe patterns, not events.** How he works and what he reliably
  delivers, generalized across roles: speed, customer and developer experience,
  client collaboration, ownership from concept to production, test-first
  delivery, AI-assisted workflows.
- **Tailor the emphasis, not the facts.** Lead with the stack the posting names
  and pick the working patterns the posting cares about. Every pattern must
  still be backed by the record.
- **No metrics.** Numbers belong to bullets.

Do not draft the summary with `/unslop mimic`. The voice card is measured from
first-person essays and would pull the summary back toward "I" sentences. The
Step 6 confidence rules still apply: no hedges, no "working toward".

Bullets follow `tailor-resume`'s rules: one line each, active verb first, a
real number where one exists. Do not voice-score `resume.md` — the file is
mostly fragments and headers, so the number would measure the format rather
than the writing.

Step 7 runs the gate battery over every document once they all exist, so it
covers the set in one pass rather than per file.

### Every role in the experience section must be relevant to this job

The master resume (`~/.proficiently/resume/`, filename from `profile.toml`) is the
list of roles the experience section may draw from. Today that is the
**Software engineering roles** list in `FACTS`. It is not a floor to pad out.

`profile.md` also carries the **Operations roles** listed in `FACTS`. Those are
pre-software operations roles, kept
for the career-pivot case that `profile.md` names explicitly: applications to
non-software roles such as procurement or purchasing. **Never put them in the
experience section of a software engineering resume**, and never let them carry
a cover letter paragraph or a screening answer there.

A posting that talks about operations, finance, billing, supply chain, or
internal business process is still a software job when it asks for years of
professional software engineering experience. Domain overlap is not a licence to
list the operations role. Make the point with software work that already touches
the domain -- the **Headline results** in `FACTS` -- and leave it there.

Two consequences worth stating:

- **Do not let `posting.md`'s own Fit Assessment override this.** That section is
  drafted in Step 2 and has argued for the operations background before. It is
  input, not permission.
- **Skills must be supported by a listed role.** If dropping a role removes the
  only evidence for a skill, drop the skill too. `FACTS` names the skills that
  come only from the operations roles; they do not belong on a software resume.

A gap in the dates is fine and expected. An irrelevant role is not.

Write `resume.md` and `cover-letter.md` into the job folder. The cover letter is
250-350 words, opens `Dear Hiring Manager,`, closes `Regards,`, hyphens only,
and names the company at least once.

## Step 5: Render HTML, then print PDFs

Copy `TRACKER/templates/resume.html.template` and
`TRACKER/templates/cover-letter.html.template` into the folder as `resume.html`
and `cover-letter.html`, substituting **content only**.

**Leave the identity placeholders exactly as they are.** `{{CANDIDATE_NAME}}`,
`{{LOCATION}}`, `{{STREET}}`, `{{CITY_STATE_ZIP}}`, `{{PHONE}}`, `{{EMAIL}}`,
`{{LINKEDIN_URL}}`, `{{LINKEDIN_TEXT}}`, `{{GITHUB_URL}}` and `{{GITHUB_TEXT}}`
are filled by `make-pdf` from `~/.jobtrackeragent/profile.toml`. Do not type the
candidate's name, address, or contact details into the HTML. Every other
placeholder is yours to fill.

**Copy the `<style>` block verbatim.** Do not adjust margins, font size, line
height, or the accent color, and do not write your own CSS. The existing 222
generated resumes contain **20 different `@page` rules** — margins from 0.38in
to 0.6in, `letter` and `Letter`, even one A4 — because this CSS was improvised
from memory each session. The template is now the only source of truth, and
`make-pdf` rejects HTML whose CSS does not match it.

Then:

```bash
PY TRACKER/main.py make-pdf <folder-name> --kind resume
PY TRACKER/main.py make-pdf <folder-name> --kind cover-letter
```

This prints through headless Chrome and verifies the result (`%PDF` magic
bytes, over 5 KB, opens in pypdf, **exactly 1 page**). Output filenames are
`<Candidate Name> Resume - <Company>.pdf` and
`<Candidate Name> Cover Letter - <Company>.pdf`, using the name from
`profile.toml`.

If `make-pdf` reports `chrome_not_found`, continue anyway: report the job as
`prepared`, note the failure, and leave the markdown as the material. A
rendering failure must not drop a good job off the board.

### Both PDFs must be exactly one page

`make-pdf` fails the render if either document exports to more than one page,
and Step 8's `check-bespoke` fails again on the same rule. A resume that runs
onto a second page is the single most common defect here, because the content
looks fine as markdown and only overflows once it is laid out.

Fix it by **cutting content**, never by changing presentation. Do not edit the
font sizes, line heights, margins, or `@page` rules -- those come from
`job_tracker_agent/templates/` and `make-pdf` rejects HTML that has drifted
from them. Cut in this order:

1. The weakest bullet on the least relevant role.
2. Bullets on older roles, down to two apiece.
3. Tighten wordy bullets to one line each -- a bullet that wraps to a second
   line costs as much space as an entire extra bullet.
4. Trim the summary to two lines.

Re-run `make-pdf` after each pass. Keep the skills block: dropping it to fit is
not an acceptable trade.

For the cover letter, stay inside 250-350 words and cut the weakest paragraph
rather than compressing the closing.

### The resume must also *fill* that page

One page is a ceiling and a floor. A resume that stops two thirds down reads as
a thin candidate no matter how good the bullets are, and it is the failure mode
that trimming for relevance creates: cut a role, and the page goes short.

`check-bespoke` measures this as `resume_bottom_gap_in` -- the distance from the
last line of text to the bottom edge -- and **fails the folder above 1.5in**.
Under 1in is where the corpus sits. Read the number, not the markdown; the
markdown gives no hint of how the page lays out.

Fill it by **adding real material**, in this order:

1. Restore a bullet cut for space from the most relevant role.
2. Add a bullet from `profile.md` that this posting actually asks about --
   there is more in the profile than any one resume uses.
3. Give the strongest role a one-line context line under the title (team,
   product, scale) if it does not have one.
4. Split an overloaded bullet that is carrying two accomplishments into two.

What must never be used to fill the page: an irrelevant role (see Step 4), a
skill no listed role supports, restated bullets, or larger fonts and margins --
`make-pdf` rejects HTML whose CSS has drifted from the template.

If the page is genuinely short after all four passes, that is a signal the
tailoring cut too deep. Go back to the posting and find the requirement the
resume is not yet answering.

## Step 6: Write `application-plan.md`

**Resolved coupling.** `proficiently:apply` builds its answer set from live
element refs returned by `read_page`. There are no refs offline. Source the
questions from three offline inputs instead:

1. The **ATS**, inferred from the URL (`fetch-job` returns it; conventions are
   in the proficiently plugin's `shared/references/ats-patterns.md`) — this
   gives that ATS's standing field set.
2. Questions **quoted verbatim in the posting text**.
3. The standing set in `application-data.md`.

Key the field table by **field label**, never by element ref.

Use exactly these section headings. The heading text is checked:

```markdown
# Application Plan — <Company>, <Role>

- **Company**:
- **Role**:
- **URL**:
- **ATS**:
- **Location**:
- **Known risk**:

## Field answers

| Field | Value |
|---|---|
| First name | ... |

> If the form auto-populates EEO fields from a resume parse or browser
> autofill, re-verify Gender/Race/Disability before submit rather than
> trusting the parse.

## Uploads (absolute paths for manual upload)
- Resume: <absolute path to the resume PDF>
- Cover letter: <absolute path to the cover letter PDF>

## Open-ended answers (draft responses)

**<Question as it appears on the form>**
<Full prose answer.>

## Known gap to be ready for
<Explicit "do not claim X" notes, and how to address any mismatch plainly.>
```

The `## Field answers` and `## Open-ended answers` headings are required; a
parenthetical suffix after either is fine.

### Two sentences. This is a hard cap, and Step 8 enforces it

**Every open-ended answer is at most two sentences.** Not two paragraphs, not
"short" — two sentences.

The reason is the form, not taste. These boxes are routinely a single line of
input, and a four-sentence answer in a one-line box is a scrolling wall to the
person reading it. The reader is also skimming a stack of them. One concrete
piece of work stated plainly beats a survey of the whole resume.

Write to the cap from the start rather than drafting long and cutting. A long
answer trimmed down keeps the shape of a long answer: a windup, a list of
everything adjacent, and the actual point buried in the middle.

**What fits in two sentences.** Sentence one names the work and the number.
Sentence two names the hard part, the outcome, or the tie to this company. Both
sentences may carry a colon or a subordinate clause — the cap is on sentences,
not on clauses, and a two-sentence answer routinely runs 60 to 90 words.

**What to cut when it does not fit.** Cut whole facts, not words. Drop the
second example, the "what I would do differently" coda, the list of adjacent
technologies. Keep the number and the mechanism. If dropping a fact makes the
answer untrue or misleading, keep the fact and drop a different one — never
compress by making a claim vaguer than the truth.

Three exceptions, all driven by the form itself:

- The form states a word or character minimum, or asks for a set number of
  bullets. Meet it, and keep each bullet to one sentence.
- The field is a cover-letter substitute. Reuse `cover-letter.md` rather than
  writing a second one.
- Internal prep notes ("ask which band this req is scoped to", "confirm with
  the candidate before committing"). Put these in a **separate paragraph** below the
  answer. They are not submitted text and the cap does not apply to them, but
  they only stay out of the submitted answer if they are their own paragraph.

A multi-part question ("what did you build, and what would you change") is not
an exception. Answer both parts inside the two sentences, or answer the part
that is actually being screened for.

If a question cannot be answered honestly in two sentences, that is a gap
rather than a length problem. Say the short true thing and record the gap under
`## Known gap to be ready for`.

### Answer with confidence, no qualifiers

This applies to every open-ended answer, the cover letter, and the resume
summary. The candidate's record is strong. The writing has to sound like
someone who knows that.

**Step 3 restates this section for the cover letter and adds the letter's own
banned-move table.** The rules are identical in both places by design. There is
no document in this packet where conceding ground is the right call.

**Cut every hedge.** None of these appear in a submitted answer: "I think",
"I believe", "I would say", "I feel", "somewhat", "fairly", "relatively",
"pretty", "a bit", "kind of", "mostly", "hopefully", "I'm still learning",
"limited experience", "not much", "although", "while I haven't", "I don't
have direct experience with X, but". No concessive opener, ever. An answer
that starts by conceding ground has already lost the reader before the good
part arrives.

**Lead with the strongest true thing.** First sentence names the work and
the result: one of the **Headline results** in `FACTS`. Context, if any,
comes after. Never wind up.

**Short and punchy.** Two sentences, per the hard cap above. Active voice,
past tense, concrete nouns, real numbers. One piece of work stated plainly
beats three surveyed.

**Never volunteer a gap.** Answer the question asked, out of what he has
actually done. If a form asks directly about a technology with no matching
experience, give one sentence naming the closest real work and stop. No
apology, no self-rating, no plan to learn it.

**Honesty outranks tone, and there is no conflict here.** Step 0's negative
capability notes are absolute: never described as having production
Kubernetes, Terraform, or gRPC, or anything else that list names. Confidence
means picking the strongest true fact and stating it without softening. It
never means inflating scope, ownership, or depth. If an answer only sounds
strong because a claim grew, the claim is wrong -- go find the real work that
answers the question.

**Gaps live in `## Known gap to be ready for`.** That section is internal
prep for the candidate, not text that gets submitted. Record the honest gap
there in full, and keep it out of the answers themselves.

### Draft the answers in voice

Write the `## Open-ended answers` prose with `/unslop mimic` per the writing
standard above, loading `card/anecdote.md`, `card/explaining-technical.md`, and
`card/numbers-data.md`. Most of these answers tell one small story about one
piece of work, which is exactly what the anecdote sheet is measured from.

Two limits the card does not know about:

- **Length wins over rhythm.** These answers are capped at two sentences. The
  card's paragraph average of 121 words and its 18-word median sentence both
  describe essays, not screening answers. Never add a sentence to reach the
  paragraph average, and expect these sentences to run longer than 18 words
  because two of them have to carry the whole answer.
- **Settled wording is frozen.** An answer quoted from `application-data.md`'s
  Custom Answers goes in verbatim. Do not re-voice it — the cache and the
  submitted answer have to agree, and those entries were already approved.

Everything else in this step still binds. The card supplies cadence; Step 6
supplies the rules; Step 0 supplies what may not be claimed.

## Step 7: Run the `/unslop` gate battery over every document

Steps 3, 4, and 6 already drafted in voice. This step proves the drafts clear
the removal gates and reads the whole set together for consistency, because a
recruiter reads the resume, the cover letter, and the screening answers as one
submission.

Scope is the three generated documents:

- `resume.md`
- `cover-letter.md`
- `application-plan.md` — the `## Open-ended answers` section only

### Check for volunteered gaps first — this runs every time

```bash
PY -c "import sys; sys.path.insert(0, r'TRACKER'); import bespoke, pathlib; \
print(bespoke.volunteered_gaps(pathlib.Path(r'<letter path>').read_text(encoding='utf-8')))"
```

Run it on `cover-letter.md` and on the plan's `## Open-ended answers` prose.
An empty list passes. Any hit on the cover letter is a Step 8 failure, so fix
it here. Repair by **deleting the concession**, per Step 3 — a softer
concession is still a concession, and the scanner is not the reason to remove
it. Re-run until clean.

### Check answer length next — this runs every time

Before the prose gates, count the sentences in every open-ended answer:

```bash
PY -c "import sys; sys.path.insert(0, r'TRACKER'); import bespoke, pathlib; \
print(bespoke.over_length_answers(pathlib.Path(r'<plan path>').read_text(encoding='utf-8')))"
```

An empty list passes. Anything else names the question and its sentence count,
and each one is a Step 8 failure, so fix it here rather than discovering it at
the gate. `over_length_answers` counts only the first paragraph of each block,
skips bullet lists and tables, and applies the `ANSWER_SENTENCE_CAP` of 2 from
Step 6.

Rewrite an over-length answer by **cutting a fact**, not by splicing sentences
together with semicolons or commas. A 40-word sentence and a 90-word sentence
are both one sentence to the checker, but only one of them is readable in a
single-line form field.

### Run the gates

On each document's prose:

```bash
PY UNSLOP/scripts/banned_phrase_scan.py < <file>
PY UNSLOP/scripts/structure_scan.py < <file>
PY UNSLOP/scripts/silhouette_scan.py < <file>
PY UNSLOP/scripts/readability_metrics.py < <file>
```

Give `UNSLOP` as a full path in every command. The scripts resolve their own
imports and phrase lists relative to themselves, so the working directory does
not matter, but a bare `scripts/...` will not resolve from a job folder.

Blocking, per `UNSLOP/references/core-contract.md` **Validation**: hard
banned-phrase hits, `anti_slop_register` hits, unjustified structural damage,
corroborated or hard silhouette damage, and staccato. Preexisting soft cadence
and uncorroborated soft silhouette warnings are advisory. **A scanner match
alone never authorizes an edit** — confirm the defect by reading the sentence in
context first, and prefer a no-op to an uncertain edit.

Then score the voice on the two prose documents:

```bash
PY UNSLOP/scripts/voice_score.py --profile VOICE/profile.json \
  --impostors UNSLOP/evals/fixtures/voice/impostors --seed 7 <file>
```

The composite is a **guide, not a gate** — lower is closer to the voice. Do not
score `resume.md` (see Step 4), and expect `low_confidence: true` on any single
answer under about 150 words; score the concatenated `## Open-ended answers`
section rather than one answer at a time.

### Repair under the core contract

Fix only spans with a confirmed finding, make the smallest repair, and copy
every other sentence byte-for-byte. Re-run the gates on the repaired text.

If a document needs more than a span fix, hand it to `/unslop rewrite`, which
runs the same contract as a two-pass diagnose-then-reconstruct flow. Verify the
result with:

```bash
PY UNSLOP/scripts/validate_preservation.py <original> <rewritten>
```

Preservation loss blocks the edit. Facts, numbers, dates, company names, and
scope survive a repair or the repair is wrong.

**Scope limits.** Never run any of this over:

- `posting.md` — that is the employer's text, quoted. Rewriting it corrupts the
  input that Step 4 and Step 6 were built from.
- The `## Field answers` table — those are names, dates, URLs, and standing
  answers from `application-data.md`. There is no prose to fix.
- `## Known gap to be ready for` — internal prep notes, never submitted.
- Any answer quoted verbatim from `application-data.md`'s Custom Answers. Those
  are settled wording; changing them here makes the cache and the submitted
  answer disagree.

**Never soften the voice.** Reject any repair that adds "I think", "somewhat",
"helped to", or a concessive clause, or that turns an owned accomplishment into
a team one. Step 6's rules outrank both the scanners and the card.

**Accuracy outranks style.** If a repair changes a claim — a number, a scope, a
technology, a role — keep the original wording. Step 0's negative capability
notes still bind: an edit that makes the candidate sound like they ran
Kubernetes in production is wrong no matter how much better it reads.

### Re-render the PDFs if the markdown changed

Step 5 already printed PDFs. If this step changed `resume.md` or
`cover-letter.md`, those PDFs are now stale — update the HTML with the edited
content and print again:

```bash
PY TRACKER/main.py make-pdf <folder-name> --kind resume
PY TRACKER/main.py make-pdf <folder-name> --kind cover-letter
```

Re-check both page rules from Step 5 afterward: still exactly one page, and
`resume_bottom_gap_in` still under 1.5in. A repair that tightens three bullets
can pull the resume short enough to fail the gap check in Step 8.

If nothing changed, skip the re-render. A clean pass is the expected outcome
now that the drafts start in voice.

## Step 8: Verify the work is bespoke and one page — this is the completion gate

```bash
PY TRACKER/main.py check-bespoke <folder-name>
```

Exit 0 means done. Exit 2 prints exactly what failed, with scores.

Three checks fail here most often. The first is **a volunteered gap in the
cover letter** — `check-bespoke` reports `cover_letter_volunteered_gaps` and
one failure line per span, quoting it. Step 7 already ran this scan, so a
failure here means the letter was edited afterward. Delete the concession; do
not soften it.

The second is **open-ended answers over two sentences** — `check-bespoke` reports `open_ended_over_two_sentences` in its
scores and one failure line per offending question. Step 7 already ran this, so
a failure here means an answer was rewritten after that check.

The third is 5-gram shingle containment of `resume.md` against the master
resume, which must stay at or below 0.55. For calibration: across the existing
253 folders the median is 0.131 and the 90th percentile 0.266, but seven early
folders score 0.585 to 0.710 — those are real near-copies of the master, and
they are what this gate exists to prevent.

On failure, **rewrite the offending document** — genuinely rework it against
this specific posting, do not paraphrase around the threshold — and re-run.
At most two attempts. If it still fails, report the job as `prepared` with the
scores in `notes` so a human can look; do not loop.

## Step 9: Write `prep-result.json`

Write a single object into the job folder as `prep-result.json`:

```json
{
  "url": "https://boards.greenhouse.io/acme/jobs/123",
  "resolved_url": "",
  "status": "prepared",
  "folder_name": "acme-2026-09-01",
  "company": "Acme",
  "title": "Senior Software Engineer",
  "date_prepared": "2026-09-01",
  "notes": "",
  "ats": "greenhouse",
  "resume_pdf": "Jordan Reyes Resume - Acme.pdf",
  "cover_letter_pdf": "Jordan Reyes Cover Letter - Acme.pdf",
  "application_plan": "application-plan.md",
  "fetch_method": "webfetch",
  "invalid_reason": null
}
```

`folder_name` is a bare folder name, never a path. `resume_pdf` and
`cover_letter_pdf` are filenames within the folder.

`resolved_url` is empty for almost every job, including LinkedIn Easy Apply.
Fill it only for a LinkedIn offsite listing whose employer URL you resolved in
Step 2, and set it to that employer URL — never to another LinkedIn link, which
the validator rejects. `url` stays the requested URL either way:

```json
{
  "url": "https://www.linkedin.com/jobs/view/senior-engineer-at-acme-4466437607",
  "resolved_url": "https://boards.greenhouse.io/acme/jobs/123",
  "status": "prepared",
  "folder_name": "acme-2026-09-12",
  "fetch_method": "chrome (linkedin offsite) + webfetch (greenhouse)"
}
```

For a job that could not be prepared:

```json
{
  "url": "https://jobs.lever.co/acme/456",
  "status": "invalid",
  "invalid_reason": "expired",
  "company": "Acme",
  "notes": "fetch-job returned fetched:false with HTTP 410. WebFetch returned 120 characters with no job-description content. Chrome loaded the page and it read 'This job is no longer accepting applications.'",
  "fetch_method": "chrome"
}
```

`invalid_reason` is required, and `notes` must actually explain what happened
and what was tried — under 40 characters is rejected by the validator.

Reasons route differently downstream: `expired`, `not_found`, `duplicate`, and
`poor_fit` move the row to the invalid tab; `blocked` and `fetch_failed` leave
it in "to apply" to be retried, which is why the diagnostic matters.

## Never do these

- Never open, fill, or interact with an application form.
- Never use `form_input`, `file_upload`, or `computer`. Chrome is for **reading**
  a posting only.
- Never submit anything.
- Never write `applied.md` — that belongs to the fill phase.
- Never touch the Google Sheet. The Python side owns every sheet write.

---

## Appendix: conventions this skill owns

These were previously carried only in model memory, which is why hand runs and
batch runs produced different output.

- **`posting.md` format** — Step 2. Parsed by `scanner.parse_posting_md`; the
  header order and the `**Company:**` field are load-bearing.
- **Print CSS** — `TRACKER/templates/`. The template is authoritative and
  `make-pdf` enforces it.
- **PDF filenames** — `<Candidate Name> Resume - <Company>.pdf`. The long form
  is what a recruiter sees on upload.
- **`application-plan.md` schema** — Step 6. It existed in 184 folders and no
  specification, and had drifted into at least six different heading sets.
- **No volunteered gaps in submitted text** — Step 3 for the cover letter,
  Step 6 for the screening answers, enforced by `bespoke.volunteered_gaps` and
  reported by `check-bespoke` as `cover_letter_volunteered_gaps`. Every pattern
  in that list was lifted from a real letter this pipeline produced, where the
  model opened by conceding a missing technology or a short tenure. The honest
  note belongs in the plan's `## Known gap to be ready for`, which is internal
  and is never scanned.
- **Two-sentence cap on open-ended answers** — Step 6, enforced by
  `bespoke.over_length_answers` and reported by `check-bespoke` as
  `open_ended_over_two_sentences`. The cap exists because these boxes are
  routinely one line of input. Internal prep notes go in a separate paragraph
  below the answer, where the cap does not reach them.
- **Voice** — the `jeff` card at `VOICE`, applied at drafting time via
  `/unslop mimic`. Every generated document uses it, which is what keeps the
  resume, the cover letter, and the screening answers reading as one person.
  The card is a rhythm model only; Step 0's negative capability notes and
  Step 6's voice rules outrank it.
- **`applied.md`** — written at *fill* time, not here, but worth recording
  because the spec is wrong elsewhere. On disk it is YAML frontmatter, and
  `job-tracker sync`/`applied` parse `status:` and `ats:` out of it:

  ```markdown
  ---
  status: Applied
  date: 2026-08-26
  ats: Greenhouse
  url: https://...
  ---
  Free-text notes.
  ```

  The `proficiently:apply` Step 9 spec shows markdown headings instead. The
  YAML form is the one that works.
