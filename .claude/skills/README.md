# Project skills

These skills live here rather than in `~/.claude/skills/` so they travel
with the Python code they drive. Every one of them shells out to
`job_tracker_agent/main.py`, and the two halves have to change together — the
result schema, the `posting.md` format, the HTML templates, and the
`application-plan.md` headings are a single contract split across a skill file
and a Python module.

| Skill | Role |
|---|---|
| `job-tracker` | Top level. Owns the Google Sheets board and its subcommands. |
| `mass-apply` | `prep` fans out across a batch of URLs and returns one validated result object. `fill` drives Chrome. |
| `apply-prep` | Prepares exactly one job, offline. Invoked per URL by `mass-apply prep`. |
| `setup` | One-time onboarding. Runs `/proficiently:setup` for anything missing, then writes the gitignored `.personal/candidate-facts.md` that `apply-prep` reads. |

## Consequence of being project-scoped

Project skills load only when the working directory is inside this repo.
`/job-tracker`, `/mass-apply`, and `/apply-prep` will not be available from an
unrelated directory. That is fine for the automated path — `main.py prep`
launches its agent with this repo as the working directory — but it does mean
interactive use has to happen from here too.

To get them back everywhere, copy a directory into `~/.claude/skills/`. Prefer
copying over splitting: two divergent copies of a skill that encodes a schema
is exactly the failure this layout exists to prevent.

## Paths

The skills use absolute paths (`D:\Code\HelperAgents\...`) rather than paths
relative to the repo root. That is deliberate: a relative path would break when
invoked from a subdirectory such as `job_tracker_agent/`, and the entries in
`.claude/settings.local.json` that pre-approve these commands match on the
absolute form. If the repo ever moves, update both together.
