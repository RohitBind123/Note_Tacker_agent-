# CentralAgent / note_taker_agent — Build Plan

> Meeting-intelligence platform: invite `centralagentai@gmail.com` to a Google Meet →
> auto-detect (Calendar) → bot joins (Vexa) → transcribe → Gemini insights → email organizer.
> Stack: Python / FastAPI + async SQLAlchemy + Alembic on Neon. Vexa CLOUD provider for dev.
> Principles: nothing hardcoded (env + config), logging at every step, document every decision.

## De-risk (DONE)
- [x] Vexa cloud bot VERIFIED joining a real Google Meet (#407 retired) — `vxa_bot_` key
- [x] Gemini key validated (model: `gemini-2.5-flash`, from config/env)
- [x] Google OAuth for `centralagentai@gmail.com` — calendar.events.readonly + gmail.send VERIFIED
- [x] Neon Postgres (pooled + direct URLs), ngrok, GCP project — all in gitignored `.env`

## P1 — Skeleton  (DONE — verified on real Neon, pushed to main)
- [x] backend/ project structure, requirements
- [x] config.py — pydantic-settings, loads root .env, Neon async/alembic URL handling
- [x] logging_config.py — structured logging, used everywhere
- [x] db: base, models (meetings, transcripts, meeting_reports + status enum), async session
- [x] Alembic (async env) + first migration applied to Neon (rev baf0f4995237)
- [x] FastAPI main + /health, /healthz/db (verified: real Neon reachable)
- [x] git init + push to RohitBind123/Note_Tacker_agent- (main)

## P2 — Vexa provider + manual orchestration  (DONE — real e2e verified, pushed)
- [x] BotProvider interface + CloudVexaProvider (POST /bots, status, transcript, DELETE)
- [x] Meet URL parser (native_meeting_id) + orchestrator (dispatch/refresh/transcript)
- [x] Manual dispatch endpoint: Meet URL → join → poll → fetch transcript → store
- [x] Unit tests (19 pass): meet_url, transcript helpers, cloud provider (respx-mocked)
- [x] REAL e2e: our API dispatched bot 14839 → joining→active → transcript (8 segs, speaker-attributed) persisted to Neon → bot stopped. Confirmed manual admit still required (auto-admit = parallel workstream).

## P3 — Calendar auto-trigger
- [ ] Calendar poller (read bot's events, detect invited + Meet link), idempotent upsert
- [ ] Scheduler (claim job, dispatch bot at start time) — the graded "advanced" flow

## P4 — Gemini insights
- [ ] Analyzer: transcript → structured report (summary/decisions/actions/risks/next steps)

## P5 — Gmail delivery
- [ ] HTML insights email → organizer (send as centralagentai)

## P6 — Hardening
- [ ] Idempotency, retries, timeouts, tests, decision-log docs

## Parallel workstream — zero-click auto-admit
- [ ] Self-hosted signed-in bot on amd64 infra (authenticated-meetings)

## Decision log
- Dev uses Vexa CLOUD provider (self-host image is amd64-only, breaks under qemu on arm64 Mac).
- Calendar detection = polling (push needs domain-verified webhook; ngrok can't verify).
- Gemini model `gemini-2.5-flash` via config/env (never hardcoded).
- Internal read-only status API for debugging; Gmail is the only user-facing delivery.
