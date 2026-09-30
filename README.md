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
2. **Score.** Up to `--max-score` (default 100) unscored jobs go to Claude Haiku, which
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
never two at once, with a 45-minute limit. Set these repository secrets:

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
