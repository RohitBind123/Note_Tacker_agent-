# CentralAgent — Setup & Credentials Guide

How to obtain **every** credential the app needs and wire it into `.env`. This is
the most involved part of the project (most of [CHALLENGES.md](./CHALLENGES.md)
§3–4 lives here), so follow it top to bottom.

All secrets live **only** in the gitignored `.env` at the repo root. Start from
the template:

```bash
cp .env.example .env   # then fill in each value below
```

The app refuses to boot in production if any **required** secret is missing
(`config.missing_required()`): `DATABASE_URL`, `VEXA_API_KEY`, `GEMINI_API_KEY`,
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
`GOOGLE_OAUTH_REFRESH_TOKEN`, `BOT_GOOGLE_EMAIL`.

---

## 1. The bot Google account

Everything is anchored to a dedicated Google account: **`centralagentai@gmail.com`**.
This account *is* the bot — it receives invites, joins via its identity (later),
and sends the insight email. Create or use a dedicated account; don't use a
personal one.

Set `BOT_GOOGLE_EMAIL=centralagentai@gmail.com` in `.env`.

---

## 2. Google Cloud project + OAuth (Calendar + Gmail)

The bot reads its calendar (to detect invites + auto-RSVP) and sends Gmail. Both
go through one OAuth client.

### 2.1 Create the GCP project
1. https://console.cloud.google.com → sign in **as the bot account**.
2. Create a project (ours: `centralagent-499019`). Note the project ID/number →
   `GCP_PROJECT_ID`, `GCP_PROJECT_NUMBER`.
3. **APIs & Services → Library** → enable **Google Calendar API** and **Gmail API**.

### 2.2 OAuth consent screen
1. **APIs & Services → OAuth consent screen** → **External**.
2. Fill app name/support email. Save.
3. **Test users → Add** `centralagentai@gmail.com`.
   > ⚠️ If you skip this you get `403 access_denied` during consent
   > (CHALLENGES §3.1).
4. **Scopes** — the app uses:
   - `https://www.googleapis.com/auth/calendar.events` (read **+ write**, so the bot can auto-RSVP "yes")
   - `https://www.googleapis.com/auth/gmail.send`
   - `openid`, `https://www.googleapis.com/auth/userinfo.email`

### 2.3 OAuth client (Desktop)
1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Type **Desktop app**. Download the `client_secret_*.json` (keep it in the repo
   root — it's gitignored — for future re-auth).
3. Copy into `.env`: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`.

### 2.4 Capture the refresh token
Run the helper (opens a browser; sign in **as the bot account**):
```bash
./.venv/bin/python tools/get_refresh_token.py
```
It uses `login_hint=centralagentai@gmail.com` so you don't accidentally consent
with a personal account. Approve the Calendar + Gmail scopes. The script prints /
captures the refresh token → set `GOOGLE_OAUTH_REFRESH_TOKEN`.

> **Token expiry:** while the OAuth app is in **Testing** mode, refresh tokens
> expire in ~7 days — re-run the helper to refresh. To stop the expiry,
> **publish the app to Production** (OAuth consent screen → *Publish app*; Google
> may require verification for sensitive scopes).

### 2.5 One-time calendar setting — "From everyone"
So invites from **any** sender land on the bot's calendar for the poller to see:
1. Sign in to https://calendar.google.com **as the bot**.
2. ⚙ **Settings → Event settings → "Add invitations to my calendar" → "From everyone."**
3. Set the calendar **time zone** to your real zone (e.g. Asia/Kolkata) so event
   times aren't stored hours off (CHALLENGES §4.2).

---

## 3. Vexa (meeting bot + transcription)

1. Get a cloud API key at https://vexa.ai → it looks like `vxa_bot_…`.
2. `.env`: `VEXA_API_KEY=vxa_bot_…`, `VEXA_API_BASE=https://api.cloud.vexa.ai`.
3. `VEXA_TRANSCRIPTION_TOKEN` (`vxa_tx_…`) is **self-host only** — not needed for
   cloud; leave the placeholder.

Verify the key:
```bash
curl -s https://api.cloud.vexa.ai/bots -H "X-API-Key: $VEXA_API_KEY" -w "\n[%{http_code}]\n"
# expect HTTP 200
```

> **Key expiry:** free-tier `vxa_bot_` keys expire ~1h. Use a paid plan
> (non-expiring) for a long-lived deploy. Cost ≈ $0.50/hr with transcription.

---

## 4. Gemini (insights)

1. https://aistudio.google.com → **Get API key**.
2. `.env`: `GEMINI_API_KEY=…`, `GEMINI_MODEL=gemini-2.5-flash`,
   `GEMINI_API_BASE=https://generativelanguage.googleapis.com/v1beta`.

Verify:
```bash
curl -s "$GEMINI_API_BASE/models?key=$GEMINI_API_KEY" -w "\n[%{http_code}]\n" | head -c 200
```

---

## 5. Neon Postgres (TWO URLs)

You need **both** a pooled URL (app runtime) and a **direct** URL (Alembic
migrations) — Alembic DDL is incompatible with Neon's PgBouncer pooler
(CHALLENGES §2.3).

1. https://neon.tech → create a project/database.
2. In the **Connect** modal:
   - **Connection pooling ON** → the `...-pooler...` string → `DATABASE_URL`.
   - **Connection pooling OFF** → the direct `...neon.tech` string →
     `DATABASE_URL_DIRECT`.
3. Run migrations:
   ```bash
   cd backend && ../.venv/bin/alembic upgrade head
   ```

The app strips libpq params and configures SSL itself, so paste the URLs as-is.

---

## 6. (Dev only) ngrok + webhooks

Calendar **push** needs a verified domain (CHALLENGES §4.3), which ngrok can't
satisfy — so in dev the **poller** is the trigger and push stays off:
```
CALENDAR_PUSH_ENABLED=false
CALENDAR_WEBHOOK_TOKEN=<any random secret>   # used to validate push calls in prod
NGROK_AUTHTOKEN=<your token>                 # only if you experiment with webhooks
PUBLIC_BASE_URL=                             # set to the deployed URL in prod
```
See [CALENDAR_PUSH.md](./CALENDAR_PUSH.md) for enabling push once you own a domain.

---

## 7. Verify the whole setup

```bash
cd backend
PYTHONPATH=. ../.venv/bin/python -m pytest -q            # tests pass
PYTHONPATH=. ../.venv/bin/python -m uvicorn app.main:app --port 8765 &
curl -s localhost:8765/health ; echo
curl -s localhost:8765/healthz/db ; echo                # database: reachable
curl -s -X POST localhost:8765/admin/poll-calendar ; echo   # upserted: N
```

If `/healthz/db` is reachable and `poll-calendar` returns without error, every
credential is wired correctly.

---

## Credentials checklist

- [ ] `BOT_GOOGLE_EMAIL` set to the dedicated bot account
- [ ] GCP project created; Calendar API + Gmail API enabled
- [ ] OAuth consent = External; bot added as **test user**
- [ ] Scopes include `calendar.events` (read-write) + `gmail.send`
- [ ] Desktop OAuth client → `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET`
- [ ] Refresh token captured → `GOOGLE_OAUTH_REFRESH_TOKEN`
- [ ] Calendar **"Add invitations → From everyone"** set
- [ ] Calendar **time zone** = your real zone
- [ ] `VEXA_API_KEY` (`vxa_bot_`) verified (HTTP 200)
- [ ] `GEMINI_API_KEY` verified
- [ ] Neon **pooled** → `DATABASE_URL`; **direct** → `DATABASE_URL_DIRECT`
- [ ] `alembic upgrade head` succeeds
- [ ] `/healthz/db` reachable, `poll-calendar` runs clean

See also: [DEPLOY.md](./DEPLOY.md) for pushing these to Railway.
