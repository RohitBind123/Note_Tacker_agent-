# CentralAgent — Meeting Intelligence (note_taker_agent)

Invite **`centralagentai@gmail.com`** to a Google Meet → the system auto-detects the
invite (Google Calendar) → a bot joins (Vexa) → records & transcribes → **Gemini**
generates a summary, decisions, action items, and risks → the insights are **emailed**
to the organizer (Gmail). No meeting URL needed.

## Stack
- **Backend:** Python / FastAPI, async SQLAlchemy 2.0 + Alembic
- **DB:** Neon Postgres
- **Meeting bot + transcription:** Vexa (cloud provider for dev; self-host planned for zero-click auto-admit)
- **LLM:** Gemini (`gemini-2.5-flash`, configurable)
- **Google:** Calendar (read) + Gmail (send) via OAuth

## Principles
- Nothing hardcoded — all config via `.env` + `app/config.py`
- Structured logging at every step
- Tested against real services (no mocks for integration paths)
- Every decision/blocker documented (`tasks/todo.md` + decision log)

## Layout
```
backend/app/
  config.py            # pydantic-settings (loads root .env)
  logging_config.py    # structlog setup
  db/                  # base, models, async session (Neon)
  api/routes/          # health (more in later phases)
  main.py              # FastAPI app + lifespan
backend/alembic/       # async migrations (Neon direct URL)
tools/                 # one-off helpers (OAuth refresh-token capture)
tasks/todo.md          # build plan + decision log
```

## Local setup
```bash
python3 -m venv .venv
./.venv/bin/pip install -r backend/requirements.txt
cp .env.example .env           # then fill in secrets (never commit .env)
cd backend && ../.venv/bin/alembic upgrade head
PYTHONPATH=. ../.venv/bin/python -m uvicorn app.main:app --reload
```

## Status
- [x] P1 — skeleton (config, DB, migrations, health) verified on real Neon
- [ ] P2 — Vexa provider + manual orchestration
- [ ] P3 — Calendar auto-trigger
- [ ] P4 — Gemini insights
- [ ] P5 — Gmail delivery

See `tasks/todo.md` for the full plan.
