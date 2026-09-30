# Agent H — Daily digest email

## Branch / PR
`phase3/h-digest` → one PR into `main`.

## Agent model
`sonnet`: well-specified rendering and stdlib email; little ambiguity.

## Owns
src/jobhunt/digest.py, tests/test_digest.py

## Build
- `DigestStats` dataclass: boards polled, jobs fetched, relevant, new, closed, scored,
  score failures, boards failed (so the email says whether the pipeline was healthy).
- `render_digest(items: list[ScoredJob], *, day: date, stats: DigestStats)
  -> tuple[str, str, str]` returning `(subject, text, html)`.
  - Items are already ranked; number them 1..N in that order (these numbers become the
    Phase 4 "approve 3 and 7" handles, so the text and HTML must agree).
  - Each item: `#n · score · SE/FDE · title · company · location(s) · pay · reason · link`.
    Pay gets a ⚠ and "(looks like a placeholder)" when `score.pay_suspect`.
  - Subject like `jobhunt · Tue Sep 30 · 7 new (top 88)`.
  - Zero items still renders a short "no new jobs today" email with the stats: it's the
    heartbeat that proves the cron ran.
  - Short footer: failed boards and score failures, if any.
  - HTML: simple, inline styles only, readable in Gmail on a phone. Escape everything.
- `send_email(subject, text, html, *, smtp_factory=smtplib.SMTP_SSL) -> None`
  - Multipart/alternative via `email.message.EmailMessage`.
  - Env: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `DIGEST_TO` (defaults to `GMAIL_ADDRESS`).
    Clear error naming the missing variable.
  - smtp.gmail.com:465. The factory parameter exists so tests pass a fake.
- Standard library only; no new dependency. `ScoredJob`/`Score`/`Job` come from `jobhunt.models`.
  Reuse the pay/location formatting style from `cli.py` (`format_job`, `_format_pay`), but don't
  edit cli.py; copy what you need or propose a shared helper in the PR's flags.

## Tests
Numbering, ordering, escaping (a title with `<script>`), suspect-pay marker, the empty
heartbeat email, the subject line, and `send_email` against a fake SMTP (login + one message
with both parts). No network.

## Done when
PR open with the template filled in, CI green. Paste a rendered text digest (from a test
fixture) into the PR description.
