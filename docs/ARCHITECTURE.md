# CentralAgent — Architecture & Decision Log

End-to-end goal: **invite `centralagentai@gmail.com` to a Google Meet → bot joins
→ transcribes → Gemini insights → email the organizer.** No meeting URL needed,
no human action except (today) one "Admit" click for the anonymous cloud bot.

- **Runtime:** one FastAPI process on Railway (amd64) + Neon Postgres.
- **Concurrency model:** two in-process `asyncio` loops (poller + scheduler). The
  DB *is* the work queue — no Celery/Redis broker.
- **Detection:** **polling** the bot's own calendar every 60s (push is built but
  off — see §3).

---

## 1. The big picture

```
                          ┌─────────────────────────────────────────────────┐
   Someone invites        │              GOOGLE CLOUD                        │
   centralagentai@        │                                                  │
   gmail.com to a    ────▶│   Google Calendar (centralagentai's calendar)    │
   Meet event             │   Google Meet  │  Gmail  │  Gemini API           │
                          └────────▲───────┴────┬────┴───────▲──────────────┘
                                   │            │            │
                       (A) PULL: GET /events    │ join/      │ generateContent
                           every 60s            │ transcript │
                                   │            │            │
        ┌──────────────────────────┼────────────┼────────────┼───────────────┐
        │  RAILWAY  (1 FastAPI process, always-on)            │               │
        │                          │            │            │               │
        │   ┌──────────────────────┴────┐   ┌───┴──────────┐ │               │
        │   │  LOOP 1: calendar_poller  │   │  Vexa cloud  │ │   ┌─────────┐ │
        │   │  every 60s                │   │  provider    │◀┼───│ Gemini  │ │
        │   │  - list Meet events       │   └───┬──────────┘ │   │ analyzer│ │
        │   │  - auto-RSVP "yes"        │       │            │   └─────────┘ │
        │   │  - UPSERT into meetings   │       │            │   ┌─────────┐ │
        │   └───────────┬───────────────┘       │            └───│ Gmail   │ │
        │               │                       │                │ sender  │ │
        │               ▼                       │                └─────────┘ │
        │        ┌────────────┐                 │                            │
        │        │   NEON     │◀────────────────┴───── reads/writes ─────────┤
        │        │ PostgreSQL │      meetings · transcripts · meeting_reports │
        │        └─────▲──────┘                                              │
        │              │                                                     │
        │   ┌──────────┴──────────────────────────────────────────────┐     │
        │   │  LOOP 2: scheduler.tick()   every 30s                    │     │
        │   │   1. recover_stale()    reclaim crashed/ancient          │     │
        │   │   2. dispatch_due()     claim due → join bot (T-60s)      │     │
        │   │   3. advance_active()   poll Vexa status, auto-stop       │     │
        │   │   4. process_pending()  transcript→Gemini→email→DONE      │     │
        │   └──────────────────────────────────────────────────────────┘     │
        └─────────────────────────────────────────────────────────────────────┘
```

Both loops are started by the FastAPI **lifespan** via `runner.start()`
(`services/runner.py`). A tick error is logged and the loop continues — a
transient Google/Vexa/DB hiccup must never kill the worker.

---

## 2. Pull vs Push (how detection actually works)

**Today it is PULL (polling), not event-driven push.** Every 60s we actively
call the Google Calendar API and read `centralagentai@gmail.com`'s calendar. We
do **not** get auto-notified when an event changes.

```
   PULL (active now)                    PUSH (built, but OFF)
   ─────────────────                    ─────────────────────
   every 60s:                           Google → POST /webhooks/calendar
     GET /calendars/primary/events        on ANY event change (instant)
   → we read the list ourselves         → needs a DOMAIN-VERIFIED https URL
   → works anywhere                     → ngrok/*.up.railway.app can't be verified
                                        → CALENDAR_PUSH_ENABLED=false
```

The push receiver (`events.watch` → `/webhooks/calendar`) and registration
(`runner._maybe_register_calendar_push`) exist but are gated off, because Google
requires a **verified domain** we don't have yet. When a real domain is
available, set `CALENDAR_PUSH_ENABLED=true`: detection becomes push-primary with
a slow reconcile-poll as backstop.

> So: we "scrape" the **bot's own** calendar, not the inviter's. An invite only
> lands there if the bot account's *"Add invitations to my calendar = From
> everyone"* setting is on (see Decision #3).

---

## 3. LOOP 1 — calendar poller (every 60s)

Files: `services/calendar_poller.py`, `services/google/calendar.py`,
`services/google/token.py`.

```
poll_once():
  1. token = refresh Google OAuth access token (from the stored refresh_token)
  2. GET https://www.googleapis.com/calendar/v3/calendars/primary/events
       ?timeMin = now - 15min          ← small look-back (catch just-started)
       &timeMax = now + 24h            ← look-ahead window
       &singleEvents=true              ← expand recurring into instances
       &orderBy=startTime
       &showDeleted=false
  3. FILTER each event:
       - skip if status == "cancelled"
       - skip if NO Meet link (must have hangoutLink OR conferenceData video)
  4. AUTO-RSVP: if my responseStatus == "needsAction"
       → PATCH .../events/{id}  set self attendee responseStatus="accepted"
  5. EXTRACT from the event JSON payload  ──┐
  6. UPSERT into `meetings` (idempotent)    │
```

### Fields extracted from the calendar payload (step 5)
```
  event["id"]                              → google_event_id   (idempotency key)
  event["summary"]                         → title
  event["start"]["dateTime"]  → parse → UTC→ start_time     ◀── drives scheduling
  event["end"]["dateTime"]    → parse → UTC→ end_time       ◀── drives auto-stop
  event["organizer"]["email"]              → organizer_email (who gets the email)
  event["attendees"][*]                    → attendees + self responseStatus
  hangoutLink / conferenceData.entryPoints → meet_url → native_meeting_id
```
`_parse_dt` converts Google's RFC3339 (`...Z` or `+05:30` offset) to **UTC**.
All-day events (only a `date`, no `dateTime`) are ignored — they have no Meet time.

### The UPSERT (step 6) — safe to run forever
```sql
INSERT INTO meetings (...) VALUES (...)
ON CONFLICT (google_event_id) DO UPDATE
  SET title, start_time, end_time, meet_url, organizer_email, updated_at
  -- NEVER touches status / vexa_bot_id
```
Polling the same event 100× = **1 row**. Re-polling a meeting already `ACTIVE`
refreshes its metadata but **won't re-dispatch** (status/vexa fields preserved).
New events land as `SCHEDULED`.

---

## 4. LOOP 2 — scheduler (every 30s) and the "1 minute before"

File: `services/scheduler.py`. `tick()` runs four passes, in order:

```
tick():
 ┌─ 1. recover_stale() ─────────────────────────────────────────┐
 │   JOINING but no bot_id for >5min   → back to SCHEDULED (retry)│
 │   ACTIVE for >3h (hard cap)         → force PROCESSING         │
 ├─ 2. dispatch_due() ──────────────────────────────────────────┤
 │   claim rows WHERE status=SCHEDULED                           │
 │     AND start_time <= now + 60s   ◀── THE "1 MINUTE BEFORE"   │
 │     AND start_time >= now - 30min  (don't dispatch ancient)   │
 │   ... using FOR UPDATE SKIP LOCKED (multi-worker safe)        │
 │   set JOINING, then OUTSIDE the lock → Vexa POST /bots        │
 ├─ 3. advance_active() ────────────────────────────────────────┤
 │   for each JOINING/ACTIVE: GET Vexa status, reconcile         │
 │   if now >= end_time + 120s grace → STOP bot (no lingering)   │
 ├─ 4. process_pending() ───────────────────────────────────────┤
 │   for each PROCESSING: transcript → Gemini → email → COMPLETED│
 └──────────────────────────────────────────────────────────────┘
```

### Why "1 minute before" (the exact math)
`dispatch_lead_seconds = 60`. A meeting becomes "due" when
`start_time <= now + 60s`. The loop ticks every 30s, so:

```
 start_time = 10:00:00
 ─────────────────────────────────────────────────────────────▶ time
        09:58:30   09:59:00   09:59:30   10:00:00
 tick:     ✗          ✗          ✓ claim    (bot already joining)
                                 │
              now+60s = 09:59:30+60 = 10:00:30 ≥ 10:00:00 → DUE → JOIN
```
The bot is dispatched in the **0–60s window before start** so it is in the lobby
*when the meeting opens*, not scrambling after it has started.

### Single convergence point for ending (today's reliability fix)
`process_pending` is the **only** place that runs the insight pipeline. Every way
a meeting can end now routes to `PROCESSING`, which `process_pending` finalizes:

| End trigger | How it reaches PROCESSING |
|---|---|
| User hits `/stop` | `orchestrator.stop_meeting` sets PROCESSING immediately |
| Vexa reports bot gone (`None`) | `refresh_status` → PROCESSING |
| Vexa reports `completed`/`stopped` | `_VEXA_TO_STATUS` → PROCESSING (see Decision #7) |
| Past scheduled `end_time` + grace | `advance_active` auto-stops the bot → PROCESSING |
| Hard 3h cap | `recover_stale` → PROCESSING |

This decouples *detecting the end* from *running the pipeline*, so a stale
provider flag can no longer stall the email, and the bot can't linger in an
empty Meet for hours.

---

## 5. Meeting state machine

```
   poller upsert
        │
        ▼
   ┌──────────┐  dispatch_due (T-60s)   ┌──────────┐  Vexa accepts   ┌─────────┐
   │SCHEDULED │ ──────────────────────▶ │ JOINING  │ ──────────────▶ │ ACTIVE  │
   └──────────┘   set JOINING + POST    └────┬─────┘  + human ADMITS └────┬────┘
        ▲              /bots                 │                            │
        │ recover_stale                      │ Vexa "failed"              │ /stop  OR
        │ (crashed claim)                    ▼                            │ end_time+grace OR
        │                              ┌──────────────┐                   │ Vexa gone/completed
        └──────────────                │ FAILED_JOIN  │                   ▼
                                       └──────────────┘            ┌────────────┐
   process_pending():                                              │ PROCESSING │
   transcript → Gemini → email                                     └─────┬──────┘
        ┌────────────────────────────────────────────────────────────────┘
        ▼
   ┌───────────┐        (analysis err → FAILED_ANALYSIS)
   │ COMPLETED │        (email err    → EMAIL_FAILED)
   └───────────┘
```
`COMPLETED` is owned **solely** by `send_report_email` (it means "insights
emailed"). No provider status ever sets it directly — a guard test enforces this.

---

## 6. Full trace of ONE invite (concrete timeline)

```
T0      colleague@x.com creates a Meet event 10:00–10:30, invites
        centralagentai@gmail.com
        (REQUIRES bot setting "Add invitations → From everyone" so it lands
         on the bot's calendar)

T0+≤60s LOOP1 poll: GET /events → sees the event (has a Meet link)
        → responseStatus needsAction → PATCH accept (auto-RSVP "yes")
        → UPSERT meetings row #N: status=SCHEDULED,
          start=10:00Z, end=10:30Z, organizer=colleague@x.com,
          meet_url=meet.google.com/abc-defg-hij
        log: calendar_rsvp_accepted, poller_upserted

09:59:xx LOOP2 dispatch_due: start <= now+60s → claim row #N (SKIP LOCKED)
        → status=JOINING → Vexa POST /bots {native_meeting_id: abc-defg-hij}
        log: scheduler_claimed, dispatch_existing_ok, vexa_join_ok bot=NNNN

10:00    bot knocks → a human clicks ADMIT (anonymous cloud bot)
        LOOP2 advance_active: Vexa "active" → status=ACTIVE
        log: refresh_status_change joining→active

10:00→   people talk → Vexa streams a Whisper transcript (speaker-attributed)
10:30    meeting ends. Whichever fires first:
           • host ends call → Vexa "completed"/gone
           • user hits stop  → /stop sets PROCESSING
           • 10:30 + 2min    → scheduler auto-stops the bot
        → status=PROCESSING

10:30+≤30s LOOP2 process_pending:
        → GET Vexa /transcripts → store in `transcripts`
        → Gemini generateContent(responseSchema) → store `meeting_reports`
          {summary, decisions, action_items, risks, next_steps}
        → Gmail users.messages.send → organizer (colleague@x.com)
        → status=COMPLETED
        log: transcript_stored, analysis_stored, report_emailed, finalize_completed
```

---

## 7. Component cheat-sheet (`backend/app/`)

| Component | File | Role | Cadence |
|---|---|---|---|
| **Lifespan / runner** | `services/runner.py` | starts/stops the 2 loops | once at boot |
| **Calendar poller** | `services/calendar_poller.py` | detect + auto-RSVP + upsert | **60s** |
| **Calendar client** | `services/google/calendar.py` | GET events / PATCH RSVP | per poll |
| **Google token** | `services/google/token.py` | refresh → access token | cached, on demand |
| **Scheduler** | `services/scheduler.py` | dispatch + lifecycle | **30s** |
| **Orchestrator** | `services/orchestrator.py` | join/stop/finalize logic | per meeting |
| **Vexa provider** | `services/vexa/{provider,cloud_provider,factory}.py` | bot join/status/transcript/stop | per call |
| **Gemini analyzer** | `services/gemini/analyzer.py` | transcript → structured insights | per meeting |
| **Gmail sender** | `services/gmail/sender.py` + `email_template.py` | insight email as the bot | per meeting |
| **HTTP helper** | `services/http.py` | timeouts + bounded retries on all external calls | every call |
| **Config** | `config.py` | all settings from `.env`; fail-fast `missing_required()` | at import |
| **DB** | `db/` (Neon Postgres) | `meetings`,`transcripts`,`meeting_reports` | — |
| **API** | `api/routes/` | health, meetings (dispatch/status/transcript/analyze/report/email/stop), admin, webhooks | per request |

---

## 8. Configuration knobs (`.env`, via `config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `calendar_poll_interval_seconds` | 60 | LOOP 1 cadence |
| `scheduler_interval_seconds` | 30 | LOOP 2 cadence |
| `dispatch_lead_seconds` | 60 | how early the bot joins before start |
| `meeting_end_grace_seconds` | 120 | wait past `end_time` before auto-stopping a lingering bot |
| `email_recipients` | organizer | who gets the insight email: `organizer` or `all_attendees` |
| `calendar_push_enabled` | false | push vs poll (needs a verified domain) |
| `gemini_model` | gemini-2.5-flash | insight model |
| Required (`missing_required`) | — | `DATABASE_URL`, `VEXA_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_OAUTH_*`, `BOT_GOOGLE_EMAIL` — app refuses to boot in prod if any is unset |

---

## 9. Key decisions (with reasoning)

1. **Vexa cloud for dev, self-host for prod.** The published `vexa-lite` image is
   amd64-only; Redis/Chrome segfault under qemu on the arm64 Mac. Cloud API
   verified working (issue #407 did not block it). A `BotProvider` abstraction
   keeps the engine swappable.
2. **Calendar polling now, push (events.watch) on deploy.** Push requires a
   *verified-domain* HTTPS webhook; ngrok and `*.up.railway.app` can't be
   verified. Pattern = **push primary + reconcile-poll backstop**, gated behind
   `CALENDAR_PUSH_ENABLED` (off until a real domain exists).
3. **Bot calendar must auto-add invitations.** An un-responded invite is only
   visible to the poller if it lands on the bot's calendar. Set the bot account's
   *"Add invitations to my calendar = From everyone."* We widened OAuth to
   `calendar.events` so the bot can auto-RSVP "yes"; fully setting-independent
   detection (Gmail-based) is a logged follow-up.
4. **Zero-click auto-admit deferred.** A cloud *guest* bot lands in the Meet
   lobby (one manual admit). True zero-click needs a *signed-in* bot (Vexa
   `authenticated-meetings`, self-host on amd64). Parallel workstream; risk:
   Google bot-detection on automated login.
5. **Data quality.** Missing report sections render "None noted" — never
   fabricated. Short/empty transcripts skip the model entirely.
6. **Reliability.** Every external call has timeouts + bounded retries
   (`services/http.py`); the scheduler claims with `FOR UPDATE SKIP LOCKED` and
   never holds a lock across network I/O; `recover_stale()` reclaims crashed
   meetings; request-id on every log line; `process_pending` is the single,
   idempotent finalize path.
7. **`COMPLETED` is ours, not Vexa's.** Vexa `"completed"` means *recording
   finished* — mapping it straight to our terminal `COMPLETED` skipped
   transcript→Gemini→email. All "meeting ended" Vexa statuses
   (`completed`/`stopped`/`processing`) now map to `PROCESSING`; our `COMPLETED`
   is set **only** by `send_report_email`. A guard test enforces the invariant.

---

## 10. Known limitations / gotchas

- **Vexa cloud `participants_count` is unreliable.** Observed `0` for the entire
  duration a human was present and talking (9 transcript segments captured),
  flipping to `1` only at the instant the meeting closed. It is **not** used for
  any logic — auto-stop keys off `end_time`. Transcription itself is unaffected.
- **Google Meet "red button" = Leave call, not End for everyone.** A plain leave
  keeps the room open and the bot in it; only the host's *"End call for everyone"*
  evicts the bot. The reliable bot-exit is our `end_time`+grace auto-stop, not
  relying on how the user leaves.
- **Cloud bot needs an active room.** If the bot arrives at an empty/closed Meet
  it fails to join (`failed_join`); a human must be in the room.
- **Dev credential expiry.** Vexa free-tier bot keys expire ~1h; Google OAuth
  *Testing-mode* refresh tokens expire ~7 days. Publish the OAuth app to
  Production for a non-expiring token; use a paid Vexa key for sustained runs.
- **Calendar timezone.** Times are stored/compared in UTC. A bot/organizer
  calendar set to GMT+00 makes "AM" entries land hours off in local time — set
  the calendar timezone to the user's actual zone.

---

## 11. Deploy

See [DEPLOY.md](./DEPLOY.md). Railway (amd64) + Neon. Env vars pushed from
`.env` (`APP_ENV=production`, `LOG_JSON=true`). Migrations run on container start
(`alembic upgrade head`). Health: `GET /health` and `GET /healthz/db`.
Live: `https://centralagent-production-457c.up.railway.app`.
