# CentralAgent — Meeting Intelligence (note_taker_agent)

Invite **`centralagentai@gmail.com`** to a Google Meet → the system auto-detects the
invite (Google Calendar) → auto-RSVPs → a bot joins (Vexa) → records & transcribes →
**Gemini** generates a summary, decisions, action items, and risks → the insights are
**emailed** to the organizer (Gmail). **No meeting URL needed.**

**Phase 2 — in-meeting copilot:** during the call, type **`@centralagent <question>`**
in the Meet chat and get an answer back in a few seconds, grounded only in what's
actually been said (live transcript + a rolling meeting memory, retrieved with
pgvector). A Vexa webhook also finalizes the summary the instant recording ends.

> **Live:** https://centralagent-production-457c.up.railway.app — `GET /health`

## Status — full pipeline built, deployed, and verified live
- [x] **P1** — skeleton (config, async DB, Alembic, health) — real Neon
- [x] **P2** — Vexa provider + manual orchestration — real Meet join + transcript
- [x] **P3** — Calendar auto-trigger (poll → detect → auto-RSVP → dispatch) — live
- [x] **P4** — Gemini insights (structured, no hallucination) — real Gemini
- [x] **P5** — Gmail delivery (insight email to organizer) — real send
- [x] **P6** — hardening (retries/timeouts, stale-session recovery, request-id) + **Railway deploy**
- [x] Reliability fixes — reliable meeting-end (`/stop` + `end_time` auto-stop), Vexa `completed` routes through the pipeline
- [x] **Phase 2** — in-meeting copilot (`@centralagent` chat Q&A, pgvector retrieval, rolling memory) + Vexa webhook instant-finalize — verified live
- [ ] **Next** — zero-click auto-admit (signed-in self-hosted bot on amd64)

The full chain has been verified end-to-end on the deployed instance: a calendar
invite was auto-detected, auto-RSVP'd, the bot was auto-dispatched, joined,
transcribed, **answered `@centralagent` questions in the Meet chat**, and the Gemini
insights were emailed — the only manual step being one "Admit" click for the
anonymous cloud bot. In the Phase 2 live test the Vexa webhook fired four times on
meeting-end and a per-meeting lock collapsed them to exactly one email (~8s later).

## Documentation
| Doc | What it covers |
|---|---|
| [docs/HOW_IT_WORKS_WALKTHROUGH.md](docs/HOW_IT_WORKS_WALKTHROUGH.md) | **Start here** — plain-English, no-jargon end-to-end story of one real meeting, explaining every component as it's used |
| [docs/SETUP.md](docs/SETUP.md) | Credentials & onboarding — Google OAuth, Vexa, Gemini, Neon; how to obtain every secret |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Full system design — diagrams, pull-vs-push detection, scheduler timing, **Phase 2 in-meeting copilot (§4b: retrieval, rolling memory, idempotency)**, state machine, end-to-end trace, components, config knobs, decisions, gotchas |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | All endpoints — health, meetings, admin, webhooks (with curl examples + schemas) |
| [docs/CHALLENGES.md](docs/CHALLENGES.md) | Every dev/deploy blocker hit, with root cause + fix + code |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Railway + Neon deploy runbook + day-2 operations (stop/redeploy/restart) |
| [docs/DOCKER_EXPLAINED.md](docs/DOCKER_EXPLAINED.md) | Plain-English, end-to-end explanation of how Docker packages & runs the app |
| [docs/CALENDAR_PUSH.md](docs/CALENDAR_PUSH.md) | Enabling real-time push (events.watch) once you own a verified domain |
| [docs/ZERO_CLICK_AUTO_ADMIT.md](docs/ZERO_CLICK_AUTO_ADMIT.md) | Plan + risks for the signed-in self-hosted bot (removes the last manual click) |
| [tasks/todo.md](tasks/todo.md) | Build plan + per-phase decision log |

## Stack
- **Backend:** Python / FastAPI, async SQLAlchemy 2.0 + Alembic
- **DB:** Neon Postgres + **pgvector** (HNSW cosine) for copilot retrieval
- **Meeting bot + transcription:** Vexa (cloud provider for dev; self-host planned for zero-click auto-admit) — chat I/O + `meeting.completed` webhook
- **LLM:** Gemini (`gemini-2.5-flash` for answers/memory/insights, `gemini-embedding-001` @ 768-dim for retrieval)
- **Google:** Calendar (read-write events, for auto-RSVP) + Gmail (send) via OAuth
- **Host:** Railway (amd64) — one FastAPI process, up to **five** in-process async loops + a Vexa webhook

## How it works (one paragraph)
Up to five `asyncio` loops run inside the FastAPI process. The **calendar poller**
(60s) reads the bot's own calendar, auto-RSVPs new invites, and upserts each
Meet-bearing event into Postgres. The **scheduler** (30s) claims a meeting ~60s
before its start, dispatches the Vexa bot, tracks its status, auto-stops it past the
scheduled end, and finalizes ended meetings through transcript → Gemini → email.
While a meeting is live, two **copilot** loops run: one (4s) reads the Meet chat,
answers `@centralagent` questions grounded in retrieved transcript chunks + a rolling
memory, and incrementally indexes new transcript into pgvector; the other (60s)
refreshes that rolling memory. A **Vexa webhook** finalizes the summary the instant
recording ends (the scheduler is the fallback; a per-meeting lock makes the two
exactly-once). The DB is the work queue; the `BotProvider` abstraction keeps the
meeting bot swappable (cloud now, self-host later). See
[ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full picture.

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
  db/                   # base, models, copilot_models (chat/interactions/chunks/memory), async session (Neon)
  api/routes/           # health, meetings, admin, webhooks (calendar + vexa)
  services/
    runner.py           # starts/stops the loops + registers the Vexa webhook
    calendar_poller.py  # detect invites + auto-RSVP + upsert            (LOOP 1, 60s)
    scheduler.py        # dispatch + lifecycle + auto-stop + finalize     (LOOP 2, 30s)
    orchestrator.py     # meeting state machine (join/stop/finalize)
    http.py             # shared async HTTP: timeouts + bounded retries
    google/             # OAuth token, Calendar client, push (events.watch) mgmt
    vexa/               # BotProvider ABC + CloudVexaProvider + factory (chat I/O + webhook)
    gemini/analyzer.py  # transcript → structured insights
    gmail/sender.py     # send insight email as the bot  (+ email_template.py)
    copilot/            # Phase 2 in-meeting copilot:
      live.py           #   capture chat + answer mentions + index         (LOOP 4, 4s)
      memory.py         #   refresh rolling meeting memory                  (LOOP 5, 60s)
      capture.py        #   persist + dedup chat, route actionable mentions
      triggers.py       #   parse @centralagent mentions
      router.py         #   handle a mention: claim → retrieve → answer → send
      retrieval.py      #   embed + index transcript chunks, top-K cosine (pgvector)
      chunker.py        #   deterministic transcript chunking
      engine.py         #   grounded Gemini answer ("use ONLY this context")
      webhook.py        #   Vexa meeting.completed → instant finalize
backend/alembic/        # async migrations (Neon direct URL; pgvector + Phase 2 tables)
backend/tests/          # unit tests (respx for HTTP, pure-logic for scheduler + copilot)
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

## Configuration switches
**Who gets the insight email** — `EMAIL_RECIPIENTS` (default `organizer`):
```bash
cd backend
railway variables --set "EMAIL_RECIPIENTS=all_attendees"   # email organizer + all guests (excludes the bot)
railway variables --set "EMAIL_RECIPIENTS=organizer"       # back to organizer only
```
(Locally, set `EMAIL_RECIPIENTS` in `.env`.)

**Phase 2 copilot** — `COPILOT_ENABLED` (default on) gates the two live loops and the
Vexa webhook; `COPILOT_CHAT_POLL_INTERVAL_SECONDS` (4) tunes chat responsiveness;
`COPILOT_TRIGGERS` (`@centralagent`) sets the mention handle; `VEXA_WEBHOOK_SECRET`
secures the instant-finalize webhook. The in-chat "thinking…" placeholder is **off by
default** (`COPILOT_THINKING_ACK_ENABLED=false`) because Meet chat is append-only.

Other knobs (intervals, dispatch lead, end grace, calendar push, copilot top-K and
memory refresh) live in `.env` / Railway variables — see
[ARCHITECTURE.md §8](docs/ARCHITECTURE.md#8-configuration-knobs-env-via-configpy).

## One-time bot-account setup
- Google Calendar → **Event settings → "Add invitations to my calendar" → "From everyone"** (so invites from any sender land on the bot's calendar for the poller to see).
- Publish the OAuth app to **Production** for a non-expiring refresh token (Testing-mode tokens expire ~7 days).
