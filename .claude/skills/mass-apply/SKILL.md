---
name: mass-apply
description: "Two modes: 'prep' prepares application materials for a batch of URLs and returns one validated result object (no Chrome forms). 'fill' fills out applications in Chrome batches."
argument-hint: "prep --urls-file <path> --schema <path> --out <path> | fill [all | <url>]"
---
# Mass Apply

Parse `$ARGUMENTS` to determine the phase.

Paths below use `TRACKER` for `D:\Code\HelperAgents\job_tracker_agent` and
`PY` for `D:\Code\HelperAgents\venv\Scripts\python.exe`.

---

## Phase 1: `prep --urls-file <path> --schema <path> --out <path>`

Prepare materials for a batch of jobs. No form filling. Normally invoked by
`PY TRACKER/main.py prep`, which creates the run directory and passes these
three paths.

**You do not touch the Google Sheet in this phase.** The Python side reads your
result file and makes every sheet change itself, in one batch. Your only
deliverable is a valid result file at the exact `--out` path.

### 1. Read the inputs

`--urls-file` holds a JSON list of `{url, company, title}`. The company and
title are the sheet's current values — hints, not truth; a subagent may correct
them from the posting.

`--schema` holds the JSON Schema for the result file. It is generated from the
Python model, so it cannot drift from what the validator enforces.

### 2. Fan out: 4 URLs per subagent, at most 6 subagents at a time

Split the URL list into consecutive chunks of **4**. Each subagent handles one
chunk, working its URLs one at a time. Launch at most 6 subagents concurrently;
if there are more chunks than that, run them as a second wave.

So 24 URLs is 6 subagents of 4, not 24 subagents of 1.

**Why chunks of 4 rather than one URL each.** Every subagent starts with a cold
context and must load `apply-prep` plus `profile.md`, `preferences.md`,
`application-data.md`, and the voice card at
`D:\Code\HelperAgents\.unslop\voice\jeff\card.md` before it can do anything —
roughly 32K tokens of cache creation, billed at the write rate rather than the
far cheaper read rate. That boot cost is per *subagent*, not per URL, so one
agent doing 4 jobs pays it once instead of four times. Measured on a 6-subagent
wave: 193K cache-creation tokens, 57% of the run's total consumption, spent
before any job work began.

The voice card is layered for exactly this reason. A subagent reads `card.md`
and its situation sheets once for the whole chunk, not once per job.

Do not raise the chunk size much past 4. The context a subagent accumulates
grows with each job it prepares — posting text, resume drafts, cover letter —
and an agent that runs out of room mid-chunk loses more work than one that
never started.

Give each subagent its chunk:

> Prepare these URLs, one at a time, in order:
> `<url 1>`, `<url 2>`, `<url 3>`, `<url 4>`
>
> For each one, invoke the `apply-prep` skill for that single URL and follow
> that skill's steps in order. Draft every document in the `jeff` voice as its
> **Writing standard** section specifies — that is `/unslop mimic` at drafting
> time, not a cleanup pass afterward. Read the voice card once for this whole
> chunk, not once per URL. Every open-ended answer is capped at two sentences —
> run its Step 7 gate battery, including the `over_length_answers` check, and
> its Step 8 `check-bespoke` gate, then write `prep-result.json` into that job's
> folder as its Step 9 specifies, and move on to the next URL.
>
> Finish every URL in the list even if an earlier one fails — a failure on one
> posting says nothing about the next. If you are running low on context, stop
> cleanly and report which URLs you did not reach; do not skip the
> `prep-result.json` for work you already finished.
>
> Return only a JSON array of the objects you wrote, nothing else.

Because each job's `prep-result.json` is written as soon as that job is done, a
subagent that dies partway still leaves finished work on disk for step 3 to
collect.

### 3. Assemble from the sidecar files, not from what the subagents said

After every subagent returns, read the `prep-result.json` files from
disk. Do **not** retype
what a subagent reported into the envelope — transcription is where errors
enter, and the files are already exact.

Every item's `url` must be the requested URL verbatim, even when the subagent
prepped against a different one. A LinkedIn offsite listing is the case that
comes up: the employer's URL belongs in `resolved_url`, and `url` still holds
the LinkedIn link the sheet row is keyed on.

For any requested URL with no `prep-result.json`, synthesize an item:

```json
{"url": "<url>", "status": "invalid", "invalid_reason": "other",
 "notes": "The subagent for this URL produced no prep-result.json, so it is unknown whether preparation was attempted or how far it got."}
```

That reason is non-terminal, so the row stays in "to apply" and is retried next
run rather than being written off.

### 4. Write the envelope

Write to the exact `--out` path:

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-01T14:30:00",
  "items": [ ... one object per requested URL ... ]
}
```

Exactly one item per requested URL. No extras, no omissions.

### 5. Validate before you finish

```bash
PY TRACKER/main.py prep-validate <out-path>
```

Exit 0 means done. Exit 2 prints precisely which item failed and why — fix
those items and re-run. At most two repair attempts.

Common rejections and their fixes:

| Rejection | Fix |
|---|---|
| `status=invalid requires notes of at least 40 characters` | Write out what was actually attempted at each rung of the fetch ladder |
| `status=invalid requires invalid_reason` | Add one of `expired`, `not_found`, `fetch_failed`, `blocked`, `duplicate`, `poor_fit`, `other` |
| `folder does not exist on disk` | `folder_name` must be the real folder basename, not a path or a guess |
| `title is the '(fetch failed)' sentinel` | The posting was never read; report the job as `invalid`, not `prepared` |
| `check-bespoke` reports `open_ended_over_two_sentences` | An answer runs past the two-sentence cap; cut a fact rather than splicing sentences together |
| `Extra inputs are not permitted` | Remove the invented field; the schema is closed |
| `result contains a URL that was never requested` | An item's `url` was rewritten. It must be the URL from the urls file verbatim; a LinkedIn offsite job's employer URL belongs in `resolved_url` |
| `resolved_url ... is another LinkedIn job link` | `resolved_url` takes the employer's own posting only. Leave it empty for Easy Apply |

### 6. Report

One short summary line: how many prepared, how many invalid, and the run
directory path. Nothing downstream reads your prose — the result file is the
interface.

### Prohibitions

Never fill or submit a form. Never use `form_input`, `file_upload`, or
`computer`. Never write to the Google Sheet.

---

## Phase 2: `fill [all | <url>]`

Fill out applications in Chrome using previously prepared materials.

1. If `all`, get the ready jobs:
   ```bash
   PY TRACKER/main.py list-rows "ready to apply"
   ```
   If given a specific URL, use just that one.

   **Work the URLs in the order they are given.** `list-rows` returns them
   oldest-added first, and that order is the point: postings expire, so the
   oldest row is the one most likely to go dead before it is reached. Never
   reorder by ATS, by how easy a site looks to fill, or by anything else. Batch
   boundaries follow the same order — the first 6 rows are batch one, the next 6
   are batch two.

2. For each job, verify `~/.proficiently/jobs/<folder>/` has `resume.md`,
   `posting.md`, and `application-plan.md`. Skip any without materials and
   report them as not prepared.

3. Using Claude in Chrome, fill applications in batches of 6, driven by each
   job's `application-plan.md`. Don't submit the applications and don't upload
   the PDFs — hand the user the absolute paths from the plan's Uploads section.
   If you encounter any issues with filling an application, like the site requires a login
   or has a bot wall, leave the posting URL open and inform the user of the actions they
   should take to proceed. The goal is to work with them to get through all the postings
   in a way that's easy for them to track, not what's easisiest to fill quicky.

   **A form you cannot fill is a normal outcome, not a question.** LinkedIn Easy
   Apply, a login wall, a bot check, a CAPTCHA, an ATS that rejects automation —
   when you hit one, get the posting as far along as you can, leave that tab
   open, and hand it to the user with what is left to do. Do not ask permission
   to give up on it and do not stall the batch waiting for an answer. Partial
   progress handed over beats a blocked queue.

   **Do ask** when the judgment is the user's rather than mechanical:

   - the posting turns out to be a poor fit (wrong seniority, wrong stack,
     a role materially different from what the tracker row said)
   - the job details contradict `preferences.md` or `profile.md` — comp below
     the floor, onsite when remote was required, a location that does not work
   - the form wants a fact that is not in the profile or application data, and
     guessing it would put something untrue on the application

   Raise these when you hit them and keep filling the rest of the batch; collect
   the questions and put them in the batch report rather than freezing on each one.

   When you've finished a batch, show a table of the jobs the user needs to take current action on,
   the Company name, and the folder name as the user will need to reference it to complete the application.

4. Wait for the user to say the batch is done, then get the status for each
   application (APPLIED, INVALID, POOR FIT, etc.).

5. For each completed application, write `applied.md` into the job folder as
   YAML frontmatter — this exact shape, because `job-tracker sync` parses
   `status:` and `ats:` out of it:
   ```markdown
   ---
   status: Applied
   date: YYYY-MM-DD
   ats: Greenhouse
   url: <posting url>
   ---
   Free-text notes about what was filled.
   ```

6. Move completed rows:
   ```bash
   PY TRACKER/main.py move-row "<url>" "ready to apply" "awaiting response" --date-applied "<date>" --ats "<ats_type>"
   ```

7. Next batch. Repeat until done.

---

## `prepare` (deprecated)

`prepare` is the old name for Phase 1, from when this skill spawned one agent
per URL with a prose instruction and no checked return shape. If invoked, say
so and run `prep` instead. If no `--urls-file` was supplied, get the URL list
with `PY TRACKER/main.py list-rows "to apply"` — but prefer
`PY TRACKER/main.py prep`, which handles dedup, the run directory, and the
sheet updates as one operation.
