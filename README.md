# job-search-agent

An agent that runs a job search end to end: finds postings, tracks them on a
board, writes tailored materials for each one, and fills the application forms.
It runs as a set of Claude Code skills over a Python CLI, on a nightly schedule.

## Status

Working, and in daily use by one person. Not yet usable by a second one.

The tool is being extracted from a personal setup. Personal data is already out
of the tree, but a number of things are still fused to one machine: absolute
Windows paths in the skill files, a Google Sheets board as the only backend, and
no onboarding beyond hand-editing config. Those are tracked as issues.

Read the open issues before trying to run this. There is no setup command yet.

## How it fits together

Three Claude Code skills in `.claude/skills/`, each calling the Python CLI in
`job_tracker_agent/`:

- **job-tracker** — the board. Finds postings from job-alert email, a LinkedIn
  search and a web search; tracks each one through six states from *to apply* to
  *rejected* or *received interview*; scans Gmail for the replies that move them.
- **apply-prep** — one application, offline. Fetches the posting, tailors a
  resume, writes a cover letter, plans the form answers, renders the PDFs.
- **mass-apply** — fans a batch of URLs across prep subagents, then drives Chrome
  to fill the forms.

The Python side owns every write to the board. A skill never edits it directly,
which is what keeps a half-finished agent run from corrupting the board.

`tests/` holds an eval harness for the form-filling phase, with a mock ATS server
and recorded scenarios for Greenhouse, Lever and Workday.

## Configuration

Nothing personal lives in this repository. Two TOML files under
`~/.jobtrackeragent/` hold it instead:

| File | Holds |
|---|---|
| `config.toml` | which board to write to, where job data lives, the search filters |
| `profile.toml` | candidate name and contact block, master resume filename, voice card |

Google credentials go in the same directory: `credentials.json` from the Cloud
Console, plus the two token files the OAuth flows write.

`job_tracker_agent/config.py` documents every key it reads. Set
`JOB_TRACKER_HOME` to point elsewhere.

## Dependencies

Beyond `requirements.txt`, the skills expect several things to already be
installed. There is no check for them yet.

- Claude Code, with the Claude in Chrome extension connected to a browser
- the `proficiently` plugin, for job search and resume tailoring
- the `unslop` skill at `~/.claude/skills/unslop`, with a trained voice card
- a Google Cloud project with the Sheets and Gmail APIs enabled

## License

MIT.
