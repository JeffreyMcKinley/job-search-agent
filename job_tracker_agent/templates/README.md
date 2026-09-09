# Resume and cover letter print templates

These two files are the **only** source of truth for what a generated resume or
cover letter PDF looks like. Before they existed, the print CSS lived nowhere
but inside 222 already-generated `resume.html` files and in whatever the model
happened to remember from a previous session. That is what made the output
non-deterministic: running the skill by hand produced a styled ~79 KB PDF via
Chrome, while the headless batch run fell back to ReportLab and produced a
plain ~4 KB one.

## The contract

`apply-prep` writes `resume.html` and `cover-letter.html` into the job folder by
copying the matching template and substituting **only content**. The CSS block
is copied verbatim. `main.py make-pdf` then refuses to convert an HTML file
whose CSS signature does not match, so drift fails loudly instead of silently
producing a differently-styled PDF.

## Resume structure

- `header` — name, then a `.contact` line. Never changes between jobs.
- `Summary` — one `<p class="summary">`, 2-3 short sentences, tailored to the role.
- `Work experience` — one `.job` block per role, most recent first. Keep every
  role from the master resume; gaps look suspicious. Each block is a `.job-head`
  (bold `.job-title` on the left, italic date `.job-meta` right-aligned by the
  flex rule) then an optional team/location `.job-meta` line, then a `<ul>`.
- `Technologies and languages` — a `.skills` section, one `<p>` per category,
  category name in `<strong>`. Reorder categories to lead with what the job asks for.

Target: two pages maximum. `make-pdf` rejects a resume PDF over 2 pages, since a
blown layout almost always shows up as extra pages.

## Cover letter structure

Sender block, date, recipient, salutation, `.body-text` paragraphs, closing,
signature. 250-350 words. `Dear Hiring Manager,` opens and `Regards,` closes.
Hyphens only, never em dashes.

## Accent color

The resume accent is fixed at `#1f6f54` (with `#b7d4c8` for the `h2` underline).
Earlier folders varied the accent per company, which was undocumented and
unreviewable. `make-pdf --accent "#RRGGBB"` still allows it, but it is now an
explicit choice rather than something that happens on its own.

## Placeholders

`{{LIKE_THIS}}`. Every placeholder must be replaced; `make-pdf` rejects a file
that still contains any.

## One page, always

Both documents must export to exactly one page. `make-pdf` fails the render
otherwise, and `check-bespoke` fails again on the same rule.

The margin comes from the `@page` rule alone. Do not add `padding` to `body`:
Chrome applies both, so a 1in `@page` margin plus 1in of body padding yields a
2in effective margin, cutting the printable area from 6.5x9in to 4.5x7in. That
bug was inherited from the folder these templates were seeded from and made
every rendered document two pages.

When content overflows, cut content. Font sizes, line heights, and margins are
fixed here and validated on render.
