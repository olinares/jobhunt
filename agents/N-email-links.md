# Agent N — One-tap Approve/Skip links

## Branch / PR
`phase5/n-email-links` → one PR into `main`. Runs in parallel with M, O1 and Q. Merge after M
(rebase for `.env.example`).

## Agent model
`opus`: signed links that change state, and they must be safe against mail scanners that
prefetch URLs.

## Owns
src/jobhunt/links.py (new), src/jobhunt/web.py (new), src/jobhunt/digest.py,
src/jobhunt/store.py (one new method only), .github/workflows/daily.yml (env lines only),
.env.example (append this brief's variables only), tests/test_links.py (new),
tests/test_web.py (new), tests/test_digest.py, tests/test_store.py

## Build
### links.py
- `sign(uid, action, *, secret, now=None, ttl=timedelta(days=14)) -> str` and
  `verify(token, *, secret, now=None) -> LinkClaim(uid, action, exp)`; raises `LinkError` with
  a reason.
- Token: base64url(`uid|action|exp`) + `.` + base64url(HMAC-SHA256(secret,
  `b"jobhunt-link-v1|" + payload`)). Compare with `hmac.compare_digest`. `action` is
  `approve` or `skip`.
- Don't include a digest id: the digest is recorded only after the email is sent
  (`cli.py` `_daily`), so there isn't one when links are rendered.
- The secret comes from `JOBHUNT_LINK_SECRET`. Refuse secrets shorter than 32 bytes.

### store.py
- `set_status_if(uid, status, *, expected) -> bool`: a single atomic
  `UPDATE jobs SET status = ? WHERE uid = ? AND status = ?`, in both SQLite and Postgres.
  Returns whether a row changed. Add it to the `Store` protocol. Change nothing else in
  store.py.

### web.py
Plain starlette route functions built by `link_routes(store_factory, *, secret)`. Brief P
registers them with FastMCP's `custom_route`, so don't create an app here.
- `GET /a/{token}`: verify, then show **only** title, company, current status and the action,
  plus a form that POSTs to the same URL. **It never writes.** Headers: `Cache-Control:
  no-store`, `X-Robots-Tag: noindex`, `Referrer-Policy: no-referrer`. Leave out the score
  reason: a model wrote it after reading the resume, and this page is public.
- `POST /a/{token}`: verify, then `set_status_if(uid, approved|skipped, expected="new")`.
  On a change → "Approved ✓". If already at the target status → "Already approved" (200). If
  some other status (for example applied) → "Status is applied; not changed" (200, no write).
  A bad or expired token → 400 with a plain page; an unknown uid → 404.
- Open the store per request.

### digest.py
- When both `JOBHUNT_PUBLIC_URL` and `JOBHUNT_LINK_SECRET` are set, each item gets
  "Approve · Skip" links (HTML) and two URL lines (text). Otherwise the output is byte-for-byte
  what it is today.

### daily.yml
- Add `JOBHUNT_PUBLIC_URL: ${{ vars.JOBHUNT_PUBLIC_URL }}` and
  `JOBHUNT_LINK_SECRET: ${{ secrets.JOBHUNT_LINK_SECRET }}` to the `jobhunt daily` step env.
  Both unset means no links, so nothing breaks before the deploy.

## Tests
No network. Cover:
- Round trip; tampered payload; tampered MAC; wrong secret; expired; unknown action; short
  secret; malformed base64.
- GET has no side effect (status unchanged after GET).
- POST new → approved; a repeated POST → "already"; POST on an applied job doesn't change it;
  a bad token → 400.
- `set_status_if` on SQLite (and on Postgres when `TEST_DATABASE_URL` is set, like the existing
  store tests).
- Digest with links; digest without the env vars is unchanged.

## Done when
PR open with the template filled in, CI green. If a change is needed outside "Owns", stop and
report it.
