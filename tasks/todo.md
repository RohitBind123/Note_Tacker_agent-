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
- [x] FIX (deployed): reliable meeting-end. /stop -> PROCESSING instantly (no waiting on Vexa's stale 'active'); scheduler auto-stops bot past end_time+grace (no lingering); new process_pending pass finalizes any PROCESSING meeting (transcript->Gemini->email) idempotently. Pure end_reason helper + 5 unit tests (32 total). Caveat: Vexa cloud participants_count unreliable (0 with human present) -> auto-stop keys off end_time, not participants.
- [ ] (follow-up) DB-integration tests for poller/scheduler; idempotency-key on manual dispatch; verify /stop->auto-email path on next real meeting

## Auto-accept (RSVP)  (DONE — verified)
- [x] Widened OAuth scope to calendar.events (read-write); re-consented; new refresh token
- [x] CalendarClient.accept_invite (PATCH self attendee -> accepted); poller auto-RSVPs needsAction invites
- [x] REAL e2e: P3 Auto Test invite needsAction -> accepted automatically on poll
- Note: auto-RSVP needs the event already on the bot calendar ("from everyone" setting). Fully setting-independent detection = Gmail-based (follow-up).

## Docs  (DONE — full set pushed)
- [x] README index + ARCHITECTURE, CHALLENGES, DEPLOY (+ops), SETUP, API_REFERENCE, CALENDAR_PUSH, ZERO_CLICK_AUTO_ADMIT; .env.example MEETING_END_GRACE_SECONDS
- [x] FIX (deployed): Vexa "completed" status now -> PROCESSING (was terminal COMPLETED, which skipped transcript/Gemini/email). COMPLETED owned only by send_report_email. Guard test (35 total). Recovered meeting #244 email.

## Configurable email recipients  (DONE — deployed)
- [x] EMAIL_RECIPIENTS env config: "organizer" (default) | "all_attendees". meetings.attendees JSONB col (migration a1b2c3d4e5f6, applied to Neon); poller stores attendee list; orchestrator.resolve_recipients (organizer-first, excludes bot, case-insensitive dedupe, fallback to organizer); send_report_email emails all via To header. 5 tests (40 total). Railway var set to organizer (safe default). To enable all: `railway variables --set "EMAIL_RECIPIENTS=all_attendees"`.

## REMAINING OPEN ACTIONS (not code-blocking — manual setup + future build)
- [ ] **Bot-account "From everyone"** — Calendar → Settings → Event settings → "Add invitations to my calendar" → "From everyone". Makes invites from ANY sender land on the bot calendar (poller detection). One-time, manual on the bot account. See docs/SETUP.md §2.5.
- [ ] **Publish OAuth app to Production** — GCP → OAuth consent screen → Publish app (may need Google verification for sensitive scopes). Stops the ~7-day Testing-mode refresh-token expiry. Until then, re-run tools/get_refresh_token.py. See docs/SETUP.md §2.4.
- [ ] **Zero-click auto-admit (the hard 10%)** — self-hosted Vexa on amd64 with authenticated-meetings (signed-in bot via bot Google session); add SelfHostVexaProvider behind the BotProvider abstraction (config-switched VEXA_PROVIDER). Risk: Google bot-detection on automated login; Vexa's own cookie-fallback test failed. Full plan + risks in docs/ZERO_CLICK_AUTO_ADMIT.md.
- [ ] (follow-up) DB-integration tests for poller/scheduler; idempotency-key on manual dispatch; paid Vexa key for sustained deploy (free key expires ~1h)

## Decision log
- Dev uses Vexa CLOUD provider (self-host image is amd64-only, breaks under qemu on arm64 Mac).
- Calendar detection = polling (push needs domain-verified webhook; ngrok can't verify).
- Gemini model `gemini-2.5-flash` via config/env (never hardcoded).
- Internal read-only status API for debugging; Gmail is the only user-facing delivery.

---

# PHASE 2 — Interactive Meeting Copilot

Branch: `feat/phase2-meeting-copilot` (off `feat/route-insight-to-real-inviter`).
Base the PR on `main` once PR #5 (inviter routing) merges, so the diff is clean.

## Decisions (locked with user 2026-06-11)
- Scope: FULL Phase 2 in one build (chat loop + evolving memory + decisions/action-items + vector retrieval).
- Engine: standard Gemini `generateContent` (gemini-2.5-flash) + a retrieval (RAG) layer. No Live API (Phase 3 "speak").
- Webhooks: YES — register `PUT /user/webhook`; finalize on `meeting.completed` (also fixes the Phase-1 ~20s end lag).
- Trigger: `@centralagent` exact, case-insensitive (env-overridable `COPILOT_TRIGGERS`).

## Validated foundation (real evidence: live Vexa OpenAPI + Vexa source + Gemini docs)
- READ chat:  `GET  /bots/{platform}/{native_id}/chat` -> `{messages:[{sender,text,timestamp,is_from_bot}]}`
- SEND chat:  `POST /bots/{platform}/{native_id}/chat` body `{text}`
- WebSocket (PRIMARY live channel): `wss://api.cloud.vexa.ai/ws` header `X-API-Key`;
  subscribe `{"action":"subscribe","meetings":[{"platform":"google_meet","native_id":"..."}]}`;
  pushes `chat.received {sender,text,timestamp}` + `transcript.mutable` segments. Poll = fallback.
- Webhook: enveloped POST `{event_id,event_type,api_version,created_at,data:{meeting}}`;
  `meeting.completed` default-on; verify `X-Webhook-Signature: sha256=HMAC(secret, f"{ts}."+body)`,
  `X-Webhook-Timestamp` replay guard; dedup on `event_id`.
- Embeddings: `gemini-embedding-001`, `outputDimensionality=768`, taskType RETRIEVAL_DOCUMENT/QUERY,
  L2-normalize ourselves (768 not pre-normalized). pgvector `vector(768)` + HNSW cosine.
- Caveat: free Vexa key expires ~1h -> sustained live testing needs a fresh/paid key.

## Architecture (push-first, poll-fallback — matches SSE-before-polling rule)
```
Vexa WS --chat.received--> mention router --@centralagent?--> copilot engine --> POST /chat (reply)
        \--transcript.mutable--> rolling memory builder (Gemini extract) + chunker -> embed -> pgvector
Vexa webhook --meeting.completed--> /webhooks/vexa -> instant finalize (Phase-1 latency fix)
copilot engine context = retrieval(pgvector) + recent chat + meeting memory + metadata
```

## Batches (TDD; pytest green + commit between each)

### Batch 0 — Config + flags + bot identity
- [ ] config.py: COPILOT_ENABLED, COPILOT_TRIGGERS(csv, "@centralagent"), VEXA_WS_URL,
      COPILOT_CHAT_POLL_INTERVAL_SECONDS, GEMINI_EMBED_MODEL(gemini-embedding-001), EMBED_DIMENSIONS(768),
      VEXA_WEBHOOK_SECRET, COPILOT_MEMORY_REFRESH_SECONDS, COPILOT_CONTEXT_TOP_K.
- [ ] Bot joins as visible name "CentralAgent" (so @centralagent is discoverable).
- [ ] Tests: settings parse / trigger csv split.

### Batch 1 — DB models + pgvector migration
- [ ] Models: MeetingChatMessage, MeetingMemory, TranscriptChunk(embedding vector(768)), CopilotInteraction.
- [ ] Migration: CREATE EXTENSION IF NOT EXISTS vector; tables; HNSW cosine index; idempotency unique indexes; IF NOT EXISTS; direct URL.
- [ ] Validate additively vs real Neon; row-count audit before any constraint.

### Batch 2 — Vexa client extensions
- [ ] get_chat / send_chat / set_webhook on provider + cloud_provider.
- [ ] WS client: connect (X-API-Key), subscribe, async-iterate, parse chat.received/transcript.mutable, reconnect.
- [ ] Tests: httpx-mocked get/send/webhook; WS frame parse from sample JSON.

### Batch 3 — Embeddings + retrieval
- [ ] Embed client: gemini-embedding-001, 768, taskType, L2-normalize, batch endpoint.
- [ ] Chunker (pure): segments -> overlapping chunks w/ speaker/time. Store+embed on delta; cosine top-k.
- [ ] Tests: chunker pure; embed mocked (shape+normalize); retrieval SQL.

### Batch 4 — Meeting memory builder
- [ ] Gemini extract over new transcript -> decisions/action_items/risks/open_questions/rolling_summary; idempotent upsert; delta guard.
- [ ] Tests: prompt build + JSON parse (Gemini mocked).

### Batch 5 — Copilot Q&A engine + mention router
- [ ] Trigger parser (pure): COPILOT_TRIGGERS case-insensitive; strip handle -> question.
- [ ] Engine: context (retrieval + recent chat + memory + metadata) -> Gemini -> reply. Answer each mention once (CopilotInteraction dedup).
- [ ] Intents: summarize so far / decisions / action items (mine to sender) / what did X say / did we discuss Y.
- [ ] Tests: trigger parse table; engine mocked.

### Batch 6 — Live wiring (WS manager + webhook endpoint + runner)
- [ ] WS manager loop (gated COPILOT_ENABLED): one conn, subscribe all ACTIVE meetings, route chat->router, transcript->memory/chunker; reconnect; poll fallback.
- [ ] POST /webhooks/vexa: verify HMAC+timestamp, dedup event_id, on meeting.completed -> finalize now. Register webhook at startup (prod https).
- [ ] Tests: signature verify; endpoint (valid/invalid sig, replay, dup event).

### Batch 7 — Real E2E
- [ ] Live: bot joins as CentralAgent; "@centralagent summarize so far" -> reply in chat; memory populated; webhook instant finalize; retrieval "what did X say". Deploy to Railway (with confirmation).

### Batch 8 — Docs + PR
- [ ] Update README/docs; open PR (base main). No direct commits to main.

## Review (filled in as batches land)
- (pending)
