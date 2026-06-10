# CentralAgent — Meeting Intelligence (note_taker_agent)

Invite **`centralagentai@gmail.com`** to a Google Meet → the system auto-detects the
invite (Google Calendar) → auto-RSVPs → a bot joins (Vexa) → records & transcribes →
**Gemini** generates a summary, decisions, action items, and risks → the insights are
**emailed** to the organizer (Gmail). **No meeting URL needed.**

> **Live:** https://centralagent-production-457c.up.railway.app — `GET /health`

## Status — full pipeline built, deployed, and verified live
- [x] **P1** — skeleton (config, async DB, Alembic, health) — real Neon
- [x] **P2** — Vexa provider + manual orchestration — real Meet join + transcript
- [x] **P3** — Calendar auto-trigger (poll → detect → auto-RSVP → dispatch) — live
- [x] **P4** — Gemini insights (structured, no hallucination) — real Gemini
- [x] **P5** — Gmail delivery (insight email to organizer) — real send
- [x] **P6** — hardening (retries/timeouts, stale-session recovery, request-id) + **Railway deploy**
- [x] Reliability fixes — reliable meeting-end (`/stop` + `end_time` auto-stop), Vexa `completed` routes through the pipeline
- [ ] **Next** — zero-click auto-admit (signed-in self-hosted bot on amd64)

The full chain has been verified end-to-end on the deployed instance: a calendar
invite was auto-detected, auto-RSVP'd, the bot was auto-dispatched, joined,
transcribed, and the Gemini insights were emailed — the only manual step being one
"Admit" click for the anonymous cloud bot.

## Documentation
| Doc | What it covers |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Credentials & onboarding — Google OAuth, Vexa, Gemini, Neon; how to obtain every secret |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Full system design — diagrams, pull-vs-push detection, scheduler timing, state machine, end-to-end trace, components, config knobs, decisions, gotchas |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | All endpoints — health, meetings, admin, webhooks (with curl examples + schemas) |
| [docs/CHALLENGES.md](docs/CHALLENGES.md) | Every dev/deploy blocker hit, with root cause + fix + code |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Railway + Neon deploy runbook + day-2 operations (stop/redeploy/restart) |
| [docs/CALENDAR_PUSH.md](docs/CALENDAR_PUSH.md) | Enabling real-time push (events.watch) once you own a verified domain |
| [docs/ZERO_CLICK_AUTO_ADMIT.md](docs/ZERO_CLICK_AUTO_ADMIT.md) | Plan + risks for the signed-in self-hosted bot (removes the last manual click) |
| [tasks/todo.md](tasks/todo.md) | Build plan + per-phase decision log |

## Stack
- **Backend:** Python / FastAPI, async SQLAlchemy 2.0 + Alembic
- **DB:** Neon Postgres
- **Meeting bot + transcription:** Vexa (cloud provider for dev; self-host planned for zero-click auto-admit)
- **LLM:** Gemini (`gemini-2.5-flash`, configurable)
- **Google:** Calendar (read-write events, for auto-RSVP) + Gmail (send) via OAuth
- **Host:** Railway (amd64) — one FastAPI process, two in-process async loops

## How it works (one paragraph)
Two `asyncio` loops run inside the FastAPI process. The **calendar poller** (60s)
reads the bot's own calendar, auto-RSVPs new invites, and upserts each Meet-bearing
event into Postgres. The **scheduler** (30s) claims a meeting ~60s before its start,
dispatches the Vexa bot, tracks its status, auto-stops it past the scheduled end, and
finalizes ended meetings through transcript → Gemini → email. The DB is the work
queue; the `BotProvider` abstraction keeps the meeting bot swappable (cloud now,
self-host later). See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full picture.

## Principles
- Nothing hardcoded — all config via `.env` + `app/config.py`; prod fails fast on missing secrets
- Structured logging (structlog) with a request-id on every line
- Tested against real services (no mocks for integration paths)
- Every external call has timeouts + bounded retries
- Every decision/blocker documented

## Layout
```
backend/app/
  config.py             # pydantic-settings (loads root .env), fail-fast missing_required()
  logging_config.py     # structlog setup
  main.py               # FastAPI app + lifespan (starts the loops) + request-id middleware
  db/                   # base, models (meetings/transcripts/meeting_reports), async session (Neon)
  api/routes/           # health, meetings, admin, webhooks
  services/
    runner.py           # starts/stops the two background loops
    calendar_poller.py  # detect invites + auto-RSVP + upsert         (LOOP 1, 60s)
    scheduler.py        # dispatch + lifecycle + auto-stop + finalize  (LOOP 2, 30s)
    orchestrator.py     # meeting state machine (join/stop/finalize)
    http.py             # shared async HTTP: timeouts + bounded retries
    google/             # OAuth token, Calendar client, push (events.watch) mgmt
    vexa/               # BotProvider ABC + CloudVexaProvider + factory
    gemini/analyzer.py  # transcript → structured insights
    gmail/sender.py     # send insight email as the bot  (+ email_template.py)
backend/alembic/        # async migrations (Neon direct URL)
backend/tests/          # unit tests (respx for HTTP, pure-logic for scheduler)
docs/                   # ARCHITECTURE.md, CHALLENGES.md, DEPLOY.md
tools/                  # one-off helpers (OAuth refresh-token capture)
tasks/todo.md           # build plan + decision log
```

## Local setup
```bash
python3 -m venv .venv
./.venv/bin/pip install -r backend/requirements.txt
cp .env.example .env            # then fill in secrets (never commit .env)
cd backend && ../.venv/bin/alembic upgrade head
PYTHONPATH=. ../.venv/bin/python -m uvicorn app.main:app --reload --port 8765
```

Run the tests:
```bash
cd backend && PYTHONPATH=. ../.venv/bin/python -m pytest -q
```

## Deploy
Railway (amd64) + Neon. Env vars pushed from `.env` (`APP_ENV=production`,
`LOG_JSON=true`); migrations run on container start. See [docs/DEPLOY.md](docs/DEPLOY.md).

## One-time bot-account setup
- Google Calendar → **Event settings → "Add invitations to my calendar" → "From everyone"** (so invites from any sender land on the bot's calendar for the poller to see).
- Publish the OAuth app to **Production** for a non-expiring refresh token (Testing-mode tokens expire ~7 days).
