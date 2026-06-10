# Deploy — Railway (amd64) + Neon

The backend is a single FastAPI service. It runs the API plus two in-process
background loops (calendar poller + scheduler). Neon is already cloud-hosted.

## Why Railway
amd64 host (no qemu issues), simple Docker deploys, env-var management, free
starter tier. Single instance is fine — the scheduler's `FOR UPDATE SKIP LOCKED`
claim is already multi-instance-safe if we scale later.

## Steps
1. **Create a Railway project** → "Deploy from GitHub repo" → select
   `RohitBind123/Note_Tacker_agent-`.
2. In the service **Settings**:
   - **Root Directory:** `backend`
   - Railway auto-detects `backend/Dockerfile` + `backend/railway.json`
     (healthcheck `/health`, migrations run on start).
3. **Variables** — add these (copy values from your local `.env`; do NOT commit them):
   ```
   APP_ENV=production
   LOG_LEVEL=INFO
   LOG_JSON=true
   DATABASE_URL=<neon pooled url>
   DATABASE_URL_DIRECT=<neon direct url>
   VEXA_API_BASE=https://api.cloud.vexa.ai
   VEXA_API_KEY=<vxa_bot_...>
   GEMINI_API_KEY=<...>
   GEMINI_MODEL=gemini-2.5-flash
   GEMINI_API_BASE=https://generativelanguage.googleapis.com/v1beta
   GCP_PROJECT_ID=centralagent-499019
   BOT_GOOGLE_EMAIL=centralagentai@gmail.com
   GOOGLE_OAUTH_CLIENT_ID=<...>
   GOOGLE_OAUTH_CLIENT_SECRET=<...>
   GOOGLE_OAUTH_REFRESH_TOKEN=<...>
   CALENDAR_POLL_INTERVAL_SECONDS=60
   SCHEDULER_INTERVAL_SECONDS=30
   DISPATCH_LEAD_SECONDS=60
   CALENDAR_PUSH_ENABLED=false
   CALENDAR_WEBHOOK_TOKEN=<random secret>
   ```
4. **Deploy.** The container runs `alembic upgrade head` then `uvicorn`.
5. After the first deploy, copy the public URL and set **`PUBLIC_BASE_URL`** to it,
   then redeploy.
6. **Verify:** open `https://<app>.up.railway.app/health` and `/healthz/db`.

## How it was actually deployed (CLI)
We deployed via the Railway **CLI** (Docker build uploaded from `backend/`), not a
connected GitHub repo. All `railway` commands run from `backend/` (the linked dir).
```bash
brew install railway
cd backend
railway login
railway init --name centralagent          # creates project
railway add --service centralagent         # creates the service
# push every var from ../.env (skip empty values; override APP_ENV/LOG_JSON):
#   railway variables --set "KEY=VALUE" ... --skip-deploys
railway up                                  # build (amd64) + migrate + serve
railway domain                              # generate the public URL
railway variables --set "PUBLIC_BASE_URL=https://<app>.up.railway.app"  # then it redeploys
```
> Gotchas (see CHALLENGES §6): the link is **dir-scoped to `backend/`**; the CLI
> **rejects empty-value vars** (`PUBLIC_BASE_URL=` — set it after first deploy);
> `railway logs` is a **snapshot**, not a live stream.

## Operations (day-2: stop / redeploy / restart)
All from `backend/`. Dashboard equivalents are in the service → **Deployments** tab,
via the **⋮** menu on the ACTIVE deployment.

| Goal | CLI | Dashboard |
|---|---|---|
| **Stop** (save credit; bot goes offline) | `railway down` | ⋮ → **Remove** (red) |
| **Start / deploy current local code** | `railway up` | — (CLI only; UI can't upload code) |
| **Redeploy same build** (re-release, re-run migrations) | `railway redeploy` | ⋮ → **Redeploy** |
| **Restart** (no rebuild; pick up env-var changes) | `railway restart` | ⋮ → **Restart** |
| **Status** | `railway status` | top of service panel |
| **Logs** | `railway logs` | **View logs** |

- **Deploy NEW code** = `railway up` (commit first). The dashboard "Redeploy"
  re-runs the **old** build — it does not pull your latest local code (no GitHub
  connection).
- **Stopping stops the loops:** while removed, the poller + scheduler don't run, so
  the bot won't auto-join. Bring it up (`railway up`) a few minutes before any
  meeting you want captured.
- **Optional:** connect the GitHub repo (Settings → Source) to get auto-deploy on
  `git push` and make UI Redeploy use latest code.

## Notes / follow-ups
- **Free-tier Vexa key expires hourly** — for a long-lived deploy use a paid Vexa
  plan (non-expiring key) or rotate.
- **Testing-mode Google refresh tokens expire in 7 days** — publish the OAuth app
  to Production (after Google verification) for non-expiring tokens.
- **Calendar push** (`CALENDAR_PUSH_ENABLED=true`) only works once `PUBLIC_BASE_URL`
  is a **verified domain** (Search Console + GCP). Until then the poller drives it.
- **Zero-click auto-admit** needs the self-hosted signed-in bot (separate workstream).
