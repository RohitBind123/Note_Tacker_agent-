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

## Notes / follow-ups
- **Free-tier Vexa key expires hourly** — for a long-lived deploy use a paid Vexa
  plan (non-expiring key) or rotate.
- **Testing-mode Google refresh tokens expire in 7 days** — publish the OAuth app
  to Production (after Google verification) for non-expiring tokens.
- **Calendar push** (`CALENDAR_PUSH_ENABLED=true`) only works once `PUBLIC_BASE_URL`
  is a **verified domain** (Search Console + GCP). Until then the poller drives it.
- **Zero-click auto-admit** needs the self-hosted signed-in bot (separate workstream).
