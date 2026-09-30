# Deploying the remote MCP server to Cloud Run

`jobhunt-remote` (`src/jobhunt/remote.py`) serves the MCP server over Streamable HTTP at
`/mcp`, behind its own OAuth with GitHub login, plus the digest's Approve/Skip links at
`/a/{token}` and a `/healthz` probe. `.github/workflows/deploy.yml` builds the image and
deploys it on every push to `main`, but only once the repo variable `DEPLOY_ENABLED` is
`true`. Until then it skips.

Everything below is done once, by hand. Nothing here uses a service-account JSON key: GitHub
Actions signs in to Google Cloud with Workload Identity Federation.

Set these in your shell first; the commands below use them.

```sh
export PROJECT_ID=jobhunt-oz            # pick a globally unique id
export REGION=us-west1
export REPO=olinares/jobhunt            # GitHub owner/name of this repository
export BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX   # gcloud billing accounts list
```

## 1. Project, billing and a $5 budget alert

```sh
gcloud projects create "$PROJECT_ID"
gcloud config set project "$PROJECT_ID"
gcloud billing projects link "$PROJECT_ID" --billing-account "$BILLING_ACCOUNT"
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

gcloud services enable billingbudgets.googleapis.com
gcloud billing budgets create --billing-account "$BILLING_ACCOUNT" \
  --display-name "jobhunt" --budget-amount 5USD \
  --filter-projects "projects/$PROJECT_ID" \
  --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0
```

A budget alert emails the billing admins; it does not stop spending. With
`--min-instances 0` the service costs nothing while idle.

## 2. APIs, Artifact Registry and service accounts

```sh
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com

gcloud artifacts repositories create jobhunt --repository-format docker --location "$REGION"

# Runs the service. It only reads secrets.
gcloud iam service-accounts create jobhunt-run --display-name "jobhunt Cloud Run runtime"
# Used by GitHub Actions to push the image and deploy.
gcloud iam service-accounts create jobhunt-deploy --display-name "jobhunt GitHub deploy"

export RUN_SA=jobhunt-run@$PROJECT_ID.iam.gserviceaccount.com
export DEPLOY_SA=jobhunt-deploy@$PROJECT_ID.iam.gserviceaccount.com

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$DEPLOY_SA" --role roles/run.admin
gcloud artifacts repositories add-iam-policy-binding jobhunt --location "$REGION" \
  --member "serviceAccount:$DEPLOY_SA" --role roles/artifactregistry.writer
# The deploy account may deploy a service that runs as jobhunt-run, and nothing else.
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" \
  --member "serviceAccount:$DEPLOY_SA" --role roles/iam.serviceAccountUser
```

## 3. Workload Identity Federation, bound to this repository

```sh
gcloud iam workload-identity-pools create github --location global \
  --display-name "GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc jobhunt-repo \
  --location global --workload-identity-pool github \
  --issuer-uri "https://token.actions.githubusercontent.com" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition "assertion.repository == '$REPO'"

# Only workflows in this repository can act as the deploy account.
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"

export WIF_PROVIDER=projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/jobhunt-repo
```

## 4. The service URL

Cloud Run's URL is predictable, so you know it before the first deploy:

```sh
export PUBLIC_URL=https://jobhunt-$PROJECT_NUMBER.$REGION.run.app
```

It must be exactly `https://` plus the host: no path, no trailing slash. The server refuses to
start otherwise, because the OAuth issuer, the token audience (`$PUBLIC_URL/mcp`) and the
email links are all built from it. After the first deploy, check it matches one of the URLs
`gcloud run services describe jobhunt --region "$REGION" --format='value(status.url)'`
prints (a service has two; either works, but use the same one everywhere).

## 5. The GitHub OAuth app

GitHub → Settings → Developer settings → OAuth Apps → New OAuth App:

- Homepage URL: `$PUBLIC_URL`
- Authorization callback URL: `$PUBLIC_URL/oauth/github/callback`

Generate a client secret. Then find your numeric GitHub id (the allowlist is by id, not by
login, because logins can be renamed and re-registered):

```sh
gh api user --jq .id
```

## 6. Secrets

Each secret goes in Secret Manager, readable only by the runtime account. `printf '%s'`
avoids storing a trailing newline.

```sh
secret() {  # secret NAME  (value on stdin)
  gcloud secrets create "$1" --replication-policy automatic --data-file=- &&
  gcloud secrets add-iam-policy-binding "$1" \
    --member "serviceAccount:$RUN_SA" --role roles/secretmanager.secretAccessor
}

printf '%s' 'postgresql://...neon...'       | secret DATABASE_URL
printf '%s' 'Iv1.xxxxxxxx'                  | secret GITHUB_CLIENT_ID
printf '%s' 'the-oauth-app-client-secret'   | secret GITHUB_CLIENT_SECRET
printf '%s' "$(gh api user --jq .id)"       | secret JOBHUNT_ALLOWED_GITHUB_ID
printf '%s' 'your-serper-key'               | secret SEARCH_API_KEY
secret RESUME_SE  < private/resumes/se.md
secret RESUME_FDE < private/resumes/fde.md
gcloud secrets create VERIFIED_FACTS --data-file=private/verified.md
gcloud secrets add-iam-policy-binding VERIFIED_FACTS \
  --member "serviceAccount:$RUN_SA" --role roles/secretmanager.secretAccessor
```

`JOBHUNT_LINK_SECRET` signs the Approve/Skip links. The daily digest (GitHub Actions) signs
them and the server checks them, so it must be **the same value** in both places. Generate
it once and set both from the same shell variable:

```sh
LINK_SECRET=$(openssl rand -base64 48)
printf '%s' "$LINK_SECRET" | secret JOBHUNT_LINK_SECRET
printf '%s' "$LINK_SECRET" | gh secret set JOBHUNT_LINK_SECRET --repo "$REPO"
unset LINK_SECRET
```

To change a secret later, add a version (`gcloud secrets versions add NAME --data-file=-`)
and redeploy: the service reads `:latest` when an instance starts.

## 7. Repository variables, then turn deploys on

These are not secret: they name resources, not credentials.

```sh
gh variable set GCP_PROJECT_ID     --repo "$REPO" --body "$PROJECT_ID"
gh variable set GCP_REGION         --repo "$REPO" --body "$REGION"
gh variable set GCP_WIF_PROVIDER   --repo "$REPO" --body "$WIF_PROVIDER"
gh variable set GCP_DEPLOY_SA      --repo "$REPO" --body "$DEPLOY_SA"
gh variable set JOBHUNT_PUBLIC_URL --repo "$REPO" --body "$PUBLIC_URL"
gh variable set DEPLOY_ENABLED     --repo "$REPO" --body true
```

`JOBHUNT_PUBLIC_URL` is also what turns on the links in the daily digest (`daily.yml`
already reads it), so from the next morning the email carries Approve/Skip links.

Run the first deploy by hand and watch it:

```sh
gh workflow run deploy.yml --repo "$REPO"
gh run watch --repo "$REPO"
```

The last step is a smoke test: `GET /healthz` must return 200 and `POST /mcp` without a token
must return 401. If the service won't start, its reason is in the logs:
`gcloud run services logs read jobhunt --region "$REGION"`. A missing or malformed variable
is named there ("Refusing to start").

## 8. Add the claude.ai connector

claude.ai → Settings → Connectors → Add custom connector:

- Name: jobhunt
- URL: `$PUBLIC_URL/mcp`
- Advanced settings: leave the OAuth client id and secret empty. Claude registers itself
  through dynamic client registration (`/register`), which only accepts claude.ai's and
  claude.com's callback and loopback redirects.

Connect, approve on the consent page (it names the client and where the code goes), then sign
in to GitHub. Any GitHub account other than the allowed id gets a 403.

The first request after the service has scaled to zero is a cold start of a few seconds.
claude.ai gives the OAuth endpoints 10 seconds; `--cpu-boost` is set to keep starts short. If
connecting times out, try once more (the instance is warm by then).

## 9. Rotating and revoking

- **Sign out every client now**: delete the token rows. Access tokens are refused at once,
  and refresh tokens can't be used.

  ```sql
  DELETE FROM oauth_tokens;
  -- or one client: DELETE FROM oauth_tokens WHERE client_id = '...';
  -- forget registered clients too (they re-register on next connect):
  DELETE FROM oauth_clients;
  ```

- **GitHub OAuth app secret**: generate a new secret in the OAuth app, add it as a new
  version of `GITHUB_CLIENT_SECRET`, redeploy (`gh workflow run deploy.yml`), then delete the
  old secret in GitHub.
- **Link secret**: rotating `JOBHUNT_LINK_SECRET` (both copies, same value) makes every link
  in past emails invalid. That's the way to kill a leaked link.
- **Turn deploys off**: `gh variable set DEPLOY_ENABLED --repo "$REPO" --body false`.
  **Take the server down**: `gcloud run services delete jobhunt --region "$REGION"`.
