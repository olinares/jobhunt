# jobhunt

A job-discovery pipeline for Solutions Engineer and Forward Deployed Engineer roles. It finds
company job boards on Greenhouse, Lever, Ashby, Workday and Gem, pulls every posting, keeps
the ones whose title and location fit (`config/roles.yaml`), scores each new one against two
resume versions with Claude Haiku, and emails a numbered digest every morning.

```bash
pip install -e ".[dev]"
jobhunt run --no-discover      # poll the seed boards and print new relevant jobs
pytest -q
```

## Daily digest

`jobhunt daily` is the whole morning run:

1. **Poll.** Register the seed boards (`config/seeds.yaml`), discover more through search
   when `SEARCH_API_KEY` is set, then fetch every known board and keep the relevant jobs.
   Descriptions are stored with them: free for Greenhouse, Lever and Ashby (they come with
   the listing), and fetched only for new relevant jobs on Workday and Gem.
2. **Score.** Up to `--max-score` (default 300) unscored jobs go to Claude Haiku, which
   returns a 0–100 fit, the resume version to send (SE or FDE) and a one-line reason.
   Jobs over the cap, or whose scoring failed, wait for the next run.
3. **Email.** Every scored job not yet in a digest is ranked and numbered, and sent over
   Gmail SMTP. A day with nothing new still sends a short "no new jobs" email, so silence
   always means something broke. The footer reports boards polled, failed boards and
   scoring failures.
4. **Record.** Only after the email is sent are the numbers saved, so a failed send leaves
   those jobs for the next digest and "approve 3 and 7" always refers to what you received.

It exits non-zero if every board failed, the email failed, or scoring failed for every job,
which turns the GitHub Actions run red. A partial failure only shows in the digest footer.

### Running it on GitHub Actions

`.github/workflows/daily.yml` runs `jobhunt daily` at 13:30 UTC (06:30 PDT / 05:30 PST),
never two at once, with a 60-minute limit. Set these repository secrets:

```bash
gh secret set DATABASE_URL          # Postgres URL (e.g. Neon); required
gh secret set ANTHROPIC_API_KEY
gh secret set RESUME_SE < private/resumes/se.md
gh secret set RESUME_FDE < private/resumes/fde.md
gh secret set GMAIL_ADDRESS         # the sending Gmail account
gh secret set GMAIL_APP_PASSWORD    # an app password; needs 2-step verification
gh secret set SEARCH_API_KEY        # optional: Serper key for discovery
gh secret set DIGEST_TO             # optional: recipient, defaults to GMAIL_ADDRESS
```

Then run it once by hand and check the email arrives before relying on the schedule:

```bash
gh workflow run daily.yml
gh run watch
```

GitHub pauses scheduled workflows in a public repository after 60 days with no repository
activity. If the digest stops arriving, re-enable the workflow in the Actions tab (or push
any commit).

### Dry run locally

```bash
cp .env.example .env               # fill in ANTHROPIC_API_KEY at least
set -a; source .env; set +a
jobhunt daily --dry-run --no-discover
```

The text digest is printed instead of emailed, and no digest is recorded, so the next real
run sends the same jobs. Scoring still calls the API and the scores are saved, so they are
not paid for twice. The database is `$DATABASE_URL` if set, else `jobhunt.db`; pass
`--db other.db` to keep a dry run away from your real data. Resumes come from `RESUME_SE` /
`RESUME_FDE`, else `private/resumes/se.md` and `fde.md` (gitignored).

## Phase 4: Claude Code MCP server

`jobhunt-mcp` is a local [MCP](https://modelcontextprotocol.io) server (stdio) that puts the
job store, the pipeline and the application packet in front of Claude Code, so the morning
digest turns into a conversation: "approve 3 and 7", "prep 3".

```bash
pip install -e ".[dev]"             # installs the jobhunt-mcp script
export DATABASE_URL=postgresql://…  # the database the daily run writes to
claude mcp add jobhunt -- jobhunt-mcp --root /path/to/jobhunt
```

The server runs with the environment `claude` was started in, so start it from a shell with
`DATABASE_URL` set (plus `RESUME_SE` / `RESUME_FDE` if the resumes aren't in
`private/resumes/`, and `SEARCH_API_KEY` for discovery), or pass them to `claude mcp add`
with `-e NAME=value`. If the venv isn't on your `PATH`, use the full path to
`.venv/bin/jobhunt-mcp`. To share the setup per project instead, copy `.mcp.json.example` to
`.mcp.json`: it reads the same variables from the environment and holds no secrets.

The server refuses to start without `--db` or `DATABASE_URL`, because the CLI's `jobhunt.db`
fallback would quietly show an empty database. `config/roles.yaml` and `private/` are read
from `--root`, and so is a relative SQLite `--db` path. Only the protocol goes to stdout;
logs go to stderr.

A job is named by a **ref**: `3` is item 3 of the latest digest, `12#3` is item 3 of digest
12, and a uid such as `greenhouse:acme:123` always works.

| Tool | What it does |
| --- | --- |
| `search_jobs(query?, status?, min_score?, variant?, remote?, limit=20)` | Stored jobs, best score first |
| `get_job(ref)` | Score, reason, status, pay, URL and a description preview |
| `list_pipeline(statuses?)` | Approved, applied, interviewing and offer jobs, closed ones included |
| `update_status(refs, status)` | Batch status change; `new` and `closed` are left to the pipeline |
| `build_packet(ref)` | The application packet: rules, ticked verified facts, resume, description |
| `discover_companies(max_queries=5)` | Search for new boards and register them; the next run polls them |
| `refresh_boards(board_keys, max_boards=5)` | Poll up to 10 boards now; new jobs are scored by the daily run |

Resources: `jobhunt://config/roles`, `jobhunt://facts/verified` (ticked facts only) and
`jobhunt://boards`. Prompts:

- **`morning_triage`** lists the latest digest and the pipeline, asks which numbers to
  approve or skip, and applies the answer with `update_status`.
- **`prep_application(ref)`** builds the packet, drafts answers from its facts only, and has
  Claude in Chrome fill in the form. It never clicks submit: you review and submit, then it
  marks the job applied.

Applications may only use facts ticked (`[x]`) in gitignored `private/verified.md` (format
in `facts/verified.example.md`). Without that file, `build_packet` says so instead of
building a packet.
