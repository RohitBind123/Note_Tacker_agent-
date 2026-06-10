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

## P3 — Calendar auto-trigger  (DONE — real e2e verified, pushed)
- [x] Async Google token helper (refresh via oauth2 endpoint) + CalendarClient (list bot events w/ Meet links)
- [x] Calendar poller — idempotent upsert (ON CONFLICT google_event_id, preserves status)
- [x] Scheduler — claim due meetings (FOR UPDATE SKIP LOCKED, no I/O in lock), dispatch, advance to transcript on end
- [x] Background runner wired into FastAPI lifespan (poller 60s + scheduler 30s loops)
- [x] Calendar PUSH (events.watch) receiver + watch mgmt, gated behind CALENDAR_PUSH_ENABLED (prod/verified-domain only); poller = dev primary + prod backstop
- [x] REAL e2e: invited centralagentai to a Calendar event (no URL) → poller auto-detected as meeting id=2 → scheduler claimed due meeting → dispatched bot 14840 → bot joined (active). Decision: bot calendar must be set "add invitations: from everyone" (read-only scope can't auto-RSVP).
- Note: Calendar timezone gotcha — user's GCal is UTC; our parsing of dateTime+offset is correct (verified against raw API).

## P4 — Gemini insights  (DONE — real Gemini verified, pushed)
- [x] GeminiAnalyzer: httpx → generateContent with responseSchema (structured JSON), model gemini-2.5-flash from config
- [x] Data-quality guards: short/empty transcript → insufficient report (no model call); prompt forbids inventing owners/dates
- [x] orchestrator.run_analysis → store MeetingReport; wired into scheduler advance (PROCESSING → analyze, FAILED_ANALYSIS on error)
- [x] endpoints: POST /meetings/{id}/analyze, GET /meetings/{id}/report; 2 unit tests (respx)
- [x] REAL e2e: analyzed meeting 1's actual transcript → structured summary + action_items(owner), empty decisions/risks (no hallucination). Stored in Neon.

## P5 — Gmail delivery  (DONE — real email sent, pushed)
- [x] GmailSender: users.messages.send (base64url MIME) as centralagentai (gmail.send scope)
- [x] email_template: HTML insights email; empty sections -> "None noted" (no fake data), HTML-escaped
- [x] orchestrator.send_report_email -> COMPLETED / EMAIL_FAILED; wired into scheduler advance (analyze -> email -> COMPLETED)
- [x] endpoint POST /meetings/{id}/send-email; 3 email-template unit tests (24 total pass)
- [x] REAL e2e: sent insights email for meeting 1 to organizer (message_id 19eb35470014281b), meeting -> COMPLETED

## FULL PIPELINE COMPLETE (P1-P5): invite bot -> detect -> dispatch -> join -> transcribe -> Gemini -> email -> COMPLETED

## P6 — Hardening + Deploy  (DONE — LIVE on Railway)
- [x] Shared HTTP helper with timeouts + bounded retries (services/http.py); applied to Vexa/Gemini/Gmail/Google/Calendar calls
- [x] Stale-session recovery in scheduler (reclaim crashed JOINING; force-process ancient ACTIVE)
- [x] Config fail-fast (missing_required) — raise in prod, warn in dev
- [x] Request-id middleware (bound to every log line + X-Request-ID header)
- [x] Retry helper unit tests (27 total pass)
- [x] Dockerfile + .dockerignore + railway.json (migrations on start, /health check)
- [x] docs/ARCHITECTURE.md (decision log) + docs/DEPLOY.md
- [x] Railway deploy LIVE: https://centralagent-production-457c.up.railway.app — project/service "centralagent", 24 env vars pushed (APP_ENV=production, LOG_JSON=true), migrations run on start. Verified: /health 200, /healthz/db 200, poller(60s)+scheduler(30s) loops running, poller_upserted in prod.
- [ ] (follow-up) DB-integration tests for poller/scheduler; idempotency-key on manual dispatch

## Auto-accept (RSVP)  (DONE — verified)
- [x] Widened OAuth scope to calendar.events (read-write); re-consented; new refresh token
- [x] CalendarClient.accept_invite (PATCH self attendee -> accepted); poller auto-RSVPs needsAction invites
- [x] REAL e2e: P3 Auto Test invite needsAction -> accepted automatically on poll
- Note: auto-RSVP needs the event already on the bot calendar ("from everyone" setting). Fully setting-independent detection = Gmail-based (follow-up).

## Zero-click auto-admit  (NEXT — the hard 10%)
- [ ] Self-hosted Vexa on amd64 (Railway/VM) with authenticated-meetings (signed-in bot via Google cookies)
- [ ] SelfHostVexaProvider behind the BotProvider abstraction (config-switched)
- [ ] Risk: Google bot-detection on automated login; Vexa's own cookie-fallback test failed

## Decision log
- Dev uses Vexa CLOUD provider (self-host image is amd64-only, breaks under qemu on arm64 Mac).
- Calendar detection = polling (push needs domain-verified webhook; ngrok can't verify).
- Gemini model `gemini-2.5-flash` via config/env (never hardcoded).
- Internal read-only status API for debugging; Gmail is the only user-facing delivery.
