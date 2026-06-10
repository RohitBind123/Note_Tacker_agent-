# CentralAgent — Architecture & Decision Log

End-to-end: **invite `centralagentai@gmail.com` to a Google Meet → bot joins →
transcribes → Gemini insights → email the organizer.** No meeting URL required.

## Flow
```
Google Calendar (bot invited)
   │  (poll now; push events.watch in prod)
   ▼
Calendar Poller ──upsert──► Neon: meetings (SCHEDULED)
   ▼
Scheduler (every 30s)
   ├─ recover_stale  (reclaim crashed mid-flight meetings)
   ├─ dispatch_due   (claim FOR UPDATE SKIP LOCKED → Vexa join)  → JOINING/ACTIVE
   └─ advance_active (poll status; on end → PROCESSING)
        ▼
   Vexa transcript ──► Neon: transcripts
        ▼
   Gemini analyzer ──► Neon: meeting_reports (summary/decisions/actions/risks/next)
        ▼
   Gmail send (as bot) → organizer  → COMPLETED
```

State machine: `PENDING → SCHEDULED → JOINING → ACTIVE → PROCESSING → COMPLETED`
with failure branches `CANCELLED / FAILED_JOIN / FAILED_RECORDING / FAILED_ANALYSIS / EMAIL_FAILED`.

## Components (`backend/app/`)
- `config.py` — all settings from `.env` (nothing hardcoded); Neon async/direct URL handling; fail-fast `missing_required()`.
- `db/` — async SQLAlchemy 2.0 on Neon (pooled+SSL, PgBouncer-safe); models `meetings/transcripts/meeting_reports`.
- `services/vexa/` — `BotProvider` ABC + `CloudVexaProvider` (swap for self-host later).
- `services/google/` — async OAuth token, Calendar client, push `events.watch` mgmt.
- `services/gemini/analyzer.py` — structured-output insights.
- `services/gmail/sender.py` + `email_template.py` — HTML insights email as the bot.
- `services/{calendar_poller, scheduler, orchestrator, runner, http}.py` — the engine.
- `api/routes/` — health, meetings (dispatch/status/transcript/analyze/report/email/stop), admin debug, webhooks.

## Key decisions (with reasoning)
1. **Vexa cloud for dev, self-host for prod.** The published `vexa-lite` image is amd64-only and Redis/Chrome segfault under qemu on the arm64 Mac. Cloud API verified working (issue #407 did not block it). A `BotProvider` abstraction keeps the engine swappable.
2. **Calendar polling now, push (events.watch) on deploy.** Calendar push requires a *verified-domain* HTTPS webhook; ngrok can't be verified. Pattern = **push primary + reconcile-poll backstop**; intervals are env-driven. Push is gated behind `CALENDAR_PUSH_ENABLED` (off in dev — no domain yet).
3. **Bot calendar must auto-add invitations.** With read-only Calendar scope the bot can't auto-RSVP, so an un-responded invite is invisible. Fix: set the bot account's "Add invitations to my calendar = From everyone". Production-robust alternative (logged): widen to `calendar.events` and auto-RSVP, or detect via Gmail.
4. **Zero-click auto-admit deferred.** A cloud guest bot lands in the Meet lobby (manual admit). True zero-click needs a *signed-in* bot (Vexa `authenticated-meetings`, self-host on amd64). Tracked as a parallel workstream.
5. **Data quality.** Missing report sections render "None noted" — never fabricated. Short/empty transcripts skip the model entirely.
6. **Reliability.** Every external call has timeouts + bounded retries (`services/http.py`); scheduler claims with `FOR UPDATE SKIP LOCKED` and never holds a lock across network I/O; `recover_stale()` reclaims crashed meetings; request-id on every log line.

## Notable blockers hit & resolved
- Vexa Meet-join reportedly broken (#407) → verified cloud join works live.
- arm64 Mac can't run amd64 vexa-lite under qemu → use cloud provider.
- Mac disk near-full broke Docker pulls → cleared regenerable caches.
- Calendar TZ confusion (user's GCal in UTC) → confirmed our offset parsing correct vs raw API.

## Deploy
See [DEPLOY.md](./DEPLOY.md). Railway (amd64) + Neon; env vars from `.env`. Migrations run on container start (`alembic upgrade head`).
