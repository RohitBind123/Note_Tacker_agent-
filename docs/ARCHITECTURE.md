# CentralAgent — Architecture & Decision Log

End-to-end goal: **invite `centralagentai@gmail.com` to a Google Meet → bot joins
→ transcribes → Gemini insights → email the organizer.** No meeting URL needed,
no human action except (today) one "Admit" click for the anonymous cloud bot.

**Phase 2 (live) adds an in-meeting copilot:** during the call, anyone can type
`@centralagent <question>` in the Meet chat and get a grounded answer back in a
few seconds, plus an instant (~10s) summary email the moment the meeting ends.
Phase 2 lives entirely in §4b; Phase 1 (detection → join → transcribe → email)
is unchanged below it.

- **Runtime:** one FastAPI process on Railway (amd64) + Neon Postgres (with the
  `pgvector` extension for Phase 2 retrieval).
- **Concurrency model:** up to **five** in-process `asyncio` loops — calendar
  poller + Gmail scanner + scheduler (Phase 1), plus copilot-chat + copilot-memory
  (Phase 2, only when `COPILOT_ENABLED=true`). The DB *is* the work queue — no
  Celery/Redis broker.
- **Detection — two independent paths:** **poll** the bot's own calendar every
  60s (§3) **and** **scan** the bot's Gmail inbox every 90s (§3b) for Meet invites
  that create no Calendar event. Calendar push is built but off (§2).
- **Finalize — two paths, fastest wins:** a Vexa **webhook** (`meeting.completed`
  → `/webhooks/vexa`) finalizes within ~10s of the meeting ending; the scheduler's
  `process_pending` pass is the always-on fallback (§4b). At-least-once delivery is
  deduped to exactly one email by a per-meeting lock.

---

## 0. Mental model — two clocks, no webhook

Before the detailed diagrams: the core is **two timer-driven loops** and a
database between them. **Nothing is event-driven** — Google never calls us. Each
loop wakes on its own clock, looks, acts, and goes back to sleep.

> A **third** loop — the Gmail invite scanner (§3b) — is a *second detector* that
> writes `SCHEDULED` rows into the same `meetings` table when a meeting has no
> Calendar event. It is left out of this mental model to keep it clean: downstream,
> the scheduler can't tell which detector produced a row.

| | **Clock A — `calendar_poller`** | **Clock B — `scheduler.tick`** |
|---|---|---|
| Wakes every | `calendar_poll_interval_seconds` = **60s** | `scheduler_interval_seconds` = **30s default / 20s in prod** |
| Reads | Google Calendar (the bot's own) | the `meetings` table |
| Writes | upserts meetings as `SCHEDULED` | flips lifecycle status, calls Vexa/Gemini/Gmail |
| "Starts soon?" decision | — | claims any `SCHEDULED` whose `start_time ≤ now + dispatch_lead_seconds (60)` |

```
        GOOGLE CALENDAR  (source of truth for "what meetings exist")
                 │
                 │   PULL on a timer — NOT pushed (no verified domain; see §2)
                 ▼
   ┌──────────────────────────────┐
   │  CLOCK A: calendar_poller     │   every 60s
   │   • list Meet events          │
   │   • auto-RSVP "yes"           │
   │   • UPSERT → meetings         │
   └───────────────┬──────────────┘
                   │  rows: status=SCHEDULED, start_time, end_time, meet_url, organizer
                   ▼
        ┌────────────────────────┐
        │   NEON POSTGRES        │   the work queue (survives restarts)
        │   meetings table       │   every meeting + its start_time + status
        └───────────┬────────────┘
                   │  SELECT ... on every tick
                   ▼
   ┌──────────────────────────────┐
   │  CLOCK B: scheduler.tick()    │   every ~20s (prod)
   │   asks each lap:              │
   │   "SCHEDULED rows with        │
   │    start_time ≤ now + 60s     │   ◀── dispatch_lead_seconds = 60
   │    AND ≥ now − 30m ?"         │
   │   → claim (SKIP LOCKED)       │
   │   → Vexa POST /bots = SEND BOT │
   └───────────────┬──────────────┘
                   │
                   ▼
            VEXA CLOUD → joins the Google Meet
```

**How it "knows a meeting starts in a minute":** it doesn't get told — Clock B
*polls the database* every ~20s and claims any meeting whose `start_time` is ≤ 60s
away. The `start_time` was copied into the DB by Clock A up to 24h earlier, so at
dispatch time the calendar is no longer in the loop; the scheduler is purely a
countdown over rows it already has. Worst-case lag from "invite sent" to "we know
about it" is **one poller lap (≤60s)**; from "due" to "bot dispatched" is **one
scheduler lap (≤20s)**.

### One invite, end to end (at a glance)

```
[1] someone invites centralagentai@gmail.com to a Meet event ──▶ Send
[2] Google drops the invite on the bot's calendar (needs "add invites from everyone")
[3] ≤60s  CLOCK A: sees it, auto-RSVPs "yes", UPSERTs row  → status=SCHEDULED
[4]       row WAITS in the DB until its start time nears (minutes…hours)
[5] T-60s CLOCK B: claims it → JOINING → Vexa POST /bots → bot joins (human Admits once)
[6]       bot records + transcribes                       → status=ACTIVE
[7]       meeting ends (leave / end-call / end_time+grace); bot leaves ~45s after alone
                                                           → status=PROCESSING
[8]       CLOCK B finalize: transcript → Gemini insights → Gmail send
                                                           → status=COMPLETED ✅
```

The sections below expand each piece: §1 the full picture, §2 pull-vs-push, §3/§4
the two core loops in detail, §3b the Gmail scanner, §5 the state machine, §6 a
concrete timeline.

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

> **Not shown above:** a third loop — the **Gmail invite scanner** (§3b) — runs
> alongside LOOP 1 and also UPSERTs `SCHEDULED` rows into `meetings`. It is a
> *second detection path* for meetings that never create a Calendar event, so the
> scheduler downstream is identical. Omitted from the diagram only to keep it legible.

All three loops are started by the FastAPI **lifespan** via `runner.start()`
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

> **Second pull path (§3b):** even with that setting on, a meeting created *from
> meet.google.com* (via "Add people") produces **no Calendar event at all** — the
> poller is structurally blind to it. The Gmail invite scanner covers that gap by
> reading the invite email Google sends instead. It too is PULL (Gmail search on a
> 90s timer), not push.

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

## 3b. LOOP 3 — Gmail invite scanner (every 90s, flag-gated)

Files: `services/gmail_scanner.py`, `services/gmail/reader.py`,
`services/gmail/invite_parser.py`.

**Why it exists.** LOOP 1 only sees meetings that exist *as Calendar events*. A
meeting started directly on **meet.google.com** and shared via **"Add people"**
creates **no Calendar event** — Google emails the invitee instead. With
calendar-only detection the bot is structurally blind to these (confirmed in prod:
the poller logged `with_meet=0` while a real invite to `dwz-mvzb-esz` sat unread in
the inbox; the bot never joined). This loop is the **second, independent detector**.
Downstream, the scheduler treats its rows like any other `SCHEDULED` meeting.

Default **OFF** (`GMAIL_SCAN_ENABLED=false`): it needs the `gmail.readonly` OAuth
scope, a Google *restricted* scope that can only be added by re-consenting the
bot's refresh token (not in code). Ships inert; enabled as a rollout step.

```
scan_once():
  1. LIST message ids:  Gmail q = GMAIL_SCAN_QUERY
       'from:meetings-noreply@google.com "meet.google.com" newer_than:1d'
       └─ meetings-noreply@google.com is Meet's instant-invite sender; scoping to
          it EXCLUDES organizer-sent calendar invitations LOOP 1 already owns.
  2. FAST DEDUP:  drop ids already in meetings.gmail_message_id
       └─ skips body download for seen mail → saves Gmail API quota
  3. FETCH + PARSE each new email → {native_meeting_id, meet_url, title, organizer, start?}
       └─ reader is multipart-safe: walks all leaf parts, text/plain wins
  3b. CROSS-SOURCE DEDUP:  skip any Meet code already in a NON-TERMINAL row
       (poller / manual / prior scan)  +  collapse duplicate emails in-batch
  4. UPSERT meetings (idempotent), status=SCHEDULED, start_time = parsed OR now
```

### No double-dispatch — five layers
A live Meet room can't host two concurrent sessions, so two rows for one code = two
bots + a duplicate insight email. Guards, outermost first:

1. **Narrow sender query** — calendar invitations (from the organizer) never match
   `meetings-noreply@google.com`.
2. **`gmail_message_id` fast dedup** — a re-seen email is skipped before its body is fetched.
3. **Cross-source in-flight guard** — skip a Meet code already in a non-terminal
   row (`ACTIVE_STATUSES`), i.e. one LOOP 1 or a manual dispatch already owns.
4. **In-batch dedup** — two invite emails for the same code in one scan collapse to one row.
5. **Idempotent upsert** — `ON CONFLICT (gmail_message_id) WHERE gmail_message_id
   IS NOT NULL DO UPDATE`, inferred against a **partial unique index** (Calendar
   rows leave `gmail_message_id` NULL and never collide). Re-scanning never makes a
   second row. The upsert NEVER touches `status`/`vexa_bot_id` — same invariant as LOOP 1.

### Why `start_time = now` for instant meets
"Add people" invites carry no scheduled time. Setting `start_time = now` lands the
row inside the scheduler's dispatch window (`start_time ≤ now + dispatch_lead_seconds`),
so LOOP 2's next ~20s tick claims and joins immediately — no special-casing in the
scheduler. Detection→bot-in-lobby verified live in prod (meeting #772: scan →
`gmail_scan_upserted` → `scheduler_claimed` → `vexa_join_ok` → `awaiting_admission`).

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

## 4b. Phase 2 — the in-meeting copilot (loops 4 & 5 + a webhook)

Files: `services/copilot/{live,capture,router,triggers,chunker,retrieval,memory,engine}.py`,
`api/routes/webhooks.py`, `db/copilot_models.py`. Active only when
`COPILOT_ENABLED=true`; Phase 1 runs unchanged without it.

Two more timer loops run **for the duration of each live meeting** (any meeting
in `JOINING`/`ACTIVE` with a dispatched bot), iterating each meeting in its own
session with the same one-bad-meeting-can't-stall-the-rest discipline as the
scheduler.

| | **LOOP 4 — `copilot_chat`** (fast) | **LOOP 5 — `copilot_memory`** (slow) |
|---|---|---|
| Wakes every | `COPILOT_CHAT_POLL_INTERVAL_SECONDS` = **4s** | `COPILOT_MEMORY_REFRESH_SECONDS` = **60s** |
| Does | capture chat → answer `@centralagent` mentions; index new transcript chunks | rebuild the rolling meeting memory (summary/decisions/actions/risks/open-Qs) |
| Cost guard | only NEW chunks are embedded (incremental) | skips the paid Gemini call unless the transcript grew ≥ `MIN_GROWTH_CHARS` (400) |

```
              GOOGLE MEET CHAT  (held by the Vexa bot)
   ┌──────────────────────────────────────────────────────┐
   │  user: @centralagent what did we decide on pricing?   │
   └──────────────────────────┬───────────────────────────┘
                              │ get_chat (REST poll) every 4s
                              ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  LOOP 4 copilot_chat_tick  (per live meeting)                  │
   │   capture.capture_chat:                                        │
   │     • persist each message ONCE                                │
   │       (unique on meeting_id + sha256(sender|ts|text))          │
   │     • is it a human "@centralagent ..."? → handle_mention ↓    │
   │   index_transcript:                                            │
   │     • chunk the transcript, embed + store only NEW chunks      │
   │       (idempotent on meeting_id + chunk_index)                 │
   └──────────────────────────┬────────────────────────────────────┘
                              │ handle_mention (router.py)
                              ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  1. CLAIM the mention exactly once                             │
   │       INSERT copilot_interactions ... ON CONFLICT (chat_msg_id)│
   │       DO NOTHING RETURNING id   — lose the race → no-op        │
   │  2. ASSEMBLE grounding context:                                │
   │       • retrieve_context: top-K transcript chunks most similar │
   │         to the question (pgvector cosine, HNSW index)          │
   │       • the rolling meeting memory row                         │
   │       • the last few human chat lines                          │
   │       • the meeting title                                      │
   │  3. CopilotEngine.answer → Gemini, grounded ("use ONLY this;   │
   │       if absent, say you haven't caught it yet")               │
   │  4. send_chat the answer back into the Meet chat               │
   │  5. record answer + grounding chunk ids on the interaction     │
   └───────────────────────────────────────────────────────────────┘
```

### Retrieval, in one paragraph
`chunker.chunk_segments` splits the transcript deterministically from the start,
so chunk *i*'s text is stable as the meeting grows — that's what makes
"embed only new chunk indices" correct and cheap. Each chunk is embedded to a
**768-dim vector** (Gemini `gemini-embedding-001`) and stored in
`transcript_chunks.embedding` (`pgvector`), indexed with **HNSW cosine**. A
question is embedded the same way; `retrieve_context` returns the top-K nearest
chunks by cosine distance. The chunk text — not the raw vectors — is what the
answer is grounded on.

### Rolling memory (LOOP 5)
`memory.refresh_memory` extracts a structured snapshot (summary + decisions +
action items + risks + open questions) via Gemini structured output and upserts
the single `meeting_memory` row per meeting. The **delta guard**
(`should_rebuild`) skips the model call unless the transcript grew ≥ 400 chars
since `transcript_chars` (the last covered point); a too-short transcript writes
an empty memory rather than risk invented owners/decisions. Verified in prod:
idle ticks log `copilot_memory_skip_no_growth`.

### Instant finalize via the Vexa webhook
Registered at boot by `runner._register_vexa_webhook` (needs `COPILOT_ENABLED`,
an HTTPS `PUBLIC_BASE_URL`, and `VEXA_WEBHOOK_SECRET`). `POST /webhooks/vexa`:

```
verify HMAC signature (fail-closed) → reject stale timestamps →
ring-buffer dedup on event_id → find the meeting by native id →
mark JOINING/ACTIVE → PROCESSING (+ end_time) → fire-and-forget finalize
```

Vexa delivers `meeting.completed` **at least once** (observed 4× in prod). A
per-meeting `asyncio.Lock` + a `finalize_already_completed` no-op collapse the
duplicates to **exactly one** insight email. If the webhook never arrives (no
secret, plain-http dev), nothing breaks — `process_pending` (§4) still finalizes
on its next lap; the webhook only buys latency (~10s vs one scheduler lap).

### Two idempotency anchors (no dupes, ever)
| Risk | Guard |
|---|---|
| Same chat message seen twice (poll re-read / provider retry) | `meeting_chat_messages` unique on `(meeting_id, dedup_key)`; insert is `ON CONFLICT DO NOTHING RETURNING id` — only the first delivery is routed |
| Same `@mention` answered twice | `copilot_interactions.chat_message_id` UNIQUE; the router only proceeds if it WON the insert |
| Same `meeting.completed` webhook delivered N times | per-meeting lock + COMPLETED no-op → one email |

### Phase 2 tables (`db/copilot_models.py`, migration `d4e5f6a7b8c9`)
| Table | Holds |
|---|---|
| `meeting_chat_messages` | every captured chat line (deduped); `is_mention` flags the actionable ones |
| `copilot_interactions` | one row per answered `@mention` — question, answer, model, grounding chunk ids, status |
| `transcript_chunks` | chunked transcript + 768-dim `embedding` (pgvector, HNSW cosine) |
| `meeting_memory` | one rolling structured memory row per meeting |

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

The machine is **source-agnostic**: meetings detected by the Gmail scanner (§3b)
enter at the same `SCHEDULED` state (with `start_time = now` for instant invites)
and follow the identical path.

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
| **Lifespan / runner** | `services/runner.py` | starts/stops the loops (3 Phase-1 + 2 Phase-2) + registers the Vexa webhook | once at boot |
| **Calendar poller** | `services/calendar_poller.py` | detect + auto-RSVP + upsert | **60s** |
| **Calendar client** | `services/google/calendar.py` | GET events / PATCH RSVP | per poll |
| **Gmail scanner** | `services/gmail_scanner.py` | detect Meet invites with no Calendar event + upsert | **90s** (flag-gated) |
| **Gmail reader** | `services/gmail/reader.py` | read-only Gmail REST (list/get, multipart-safe) | per scan |
| **Invite parser** | `services/gmail/invite_parser.py` | invite email → native_meeting_id / meet_url / time | per email |
| **Google token** | `services/google/token.py` | refresh → access token | cached, on demand |
| **Scheduler** | `services/scheduler.py` | dispatch + lifecycle | **30s** |
| **Orchestrator** | `services/orchestrator.py` | join/stop/finalize logic | per meeting |
| **Vexa provider** | `services/vexa/{provider,cloud_provider,factory}.py` | bot join/status/transcript/stop | per call |
| **Gemini analyzer** | `services/gemini/analyzer.py` | transcript → structured insights | per meeting |
| **Gmail sender** | `services/gmail/sender.py` + `email_template.py` | insight email as the bot | per meeting |
| **Copilot — chat loop** | `services/copilot/live.py` (`copilot_chat_tick`) | capture chat, answer mentions, index chunks (Phase 2) | **4s** |
| **Copilot — memory loop** | `services/copilot/live.py` (`copilot_memory_tick`) | rebuild rolling meeting memory (Phase 2) | **60s** |
| **Copilot — capture/router** | `services/copilot/{capture,router,triggers}.py` | dedup chat, parse `@mention`, answer once | per new message |
| **Copilot — retrieval** | `services/copilot/{chunker,retrieval}.py` | chunk + embed transcript, pgvector top-K | per tick / per question |
| **Copilot — memory/engine** | `services/copilot/{memory,engine}.py` | structured memory + grounded Gemini answer | per refresh / per question |
| **Embeddings** | `services/gemini/embeddings.py` | 768-dim Gemini embeddings (docs + query) | per chunk / per question |
| **Vexa webhook** | `api/routes/webhooks.py` | `meeting.completed` → instant finalize (deduped) | per event |
| **HTTP helper** | `services/http.py` | timeouts + bounded retries on all external calls | every call |
| **Config** | `config.py` | all settings from `.env`; fail-fast `missing_required()` | at import |
| **DB** | `db/` (Neon Postgres + pgvector) | `meetings`,`transcripts`,`meeting_reports`; Phase 2: `meeting_chat_messages`,`copilot_interactions`,`transcript_chunks`,`meeting_memory` | — |
| **API** | `api/routes/` | health, meetings (dispatch/status/transcript/analyze/report/email/stop), admin, webhooks (calendar + vexa) | per request |

---

## 8. Configuration knobs (`.env`, via `config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `calendar_poll_interval_seconds` | 60 | LOOP 1 cadence |
| `scheduler_interval_seconds` | 30 | LOOP 2 cadence |
| `gmail_scan_enabled` | false | LOOP 3 on/off (needs `gmail.readonly` scope) |
| `gmail_scan_interval_seconds` | 90 | LOOP 3 cadence |
| `gmail_scan_query` | `from:meetings-noreply@google.com "meet.google.com" newer_than:1d` | Gmail search for instant-invite emails (widen to `7d` on first enable) |
| `gmail_scan_max_results` | 25 | max emails inspected per scan |
| `dispatch_lead_seconds` | 60 | how early the bot joins before start |
| `meeting_end_grace_seconds` | 120 | wait past `end_time` before auto-stopping a lingering bot |
| `email_recipients` | organizer | who gets the insight email: `organizer` or `all_attendees` |
| `calendar_push_enabled` | false | push vs poll (needs a verified domain) |
| `gemini_model` | gemini-2.5-flash | insight model |
| **— Phase 2 copilot —** | | |
| `copilot_enabled` | false | master switch for LOOP 4/5 + the Vexa webhook (§4b) |
| `copilot_chat_poll_interval_seconds` | 4 | LOOP 4 cadence (chat capture + answer + index) |
| `copilot_memory_refresh_seconds` | 60 | LOOP 5 cadence (rolling memory rebuild) |
| `copilot_context_top_k` | 6 | how many transcript chunks ground each answer |
| `copilot_bot_name` | CentralAgent | name the copilot uses when answering |
| `copilot_triggers` | `@centralagent` | mention handle(s) that route a chat line to the copilot |
| `copilot_thinking_ack_enabled` | false | post a "thinking…" placeholder before answering — OFF: Meet chat is append-only so it can't be replaced by the answer; answers land in ~3s anyway |
| `vexa_webhook_secret` | "" | HMAC secret for `/webhooks/vexa`; empty → webhook fails closed (scheduler still finalizes) |
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
8. **Two detection paths, not one (§3b).** Calendar-only detection silently dropped
   every meeting born on meet.google.com (no Calendar event → poller blind). The
   Gmail invite scanner is an independent second path; explicit cross-path dedup
   (narrow sender query + in-flight `native_meeting_id` guard) keeps the two from
   double-dispatching. Ships OFF until `gmail.readonly` is consented. The upsert
   targets a **partial unique index** via index inference (`ON CONFLICT (col)
   WHERE ...`) — `ON CONSTRAINT <name>` does **not** resolve a partial index and
   crashed every scan tick in prod until fixed; a compile-level test now guards the
   SQL shape. Full write-up: CHALLENGES §5.10.
9. **Copilot answers are retrieval-grounded, never free-form (§4b).** A meeting
   copilot that invents decisions is worse than one that admits it missed
   something. Every answer is built from top-K transcript chunks + the rolling
   memory + recent chat, and the prompt forbids going beyond them. The transcript
   is chunked deterministically from the start so growth only ever embeds the new
   tail (cheap, incremental), and pgvector HNSW makes "closest chunks" fast.
10. **Exactly-once everywhere on the live path.** Chat is polled (at-least-once)
    and the Vexa webhook is delivered at-least-once (4× in prod), so every live
    write is idempotent: chat messages dedup on a content hash, mentions dedup on
    `chat_message_id`, and `meeting.completed` collapses to one email via a
    per-meeting lock. Correctness never depends on a delivery arriving exactly once.
11. **In-chat "thinking…" placeholder defaulted OFF.** Google Meet chat is
    **append-only** — no edit/delete API — so a placeholder can never be *replaced*
    by the answer the way ChatGPT/Perplexity do; it would linger as a permanent
    stale line above every reply. With answers landing in ~3s, that's clutter, not
    feedback. The helper + `COPILOT_THINKING_ACK_ENABLED` gate remain so it can be
    re-enabled if a future platform supports message replacement; the faster 4s
    chat poll (the part that actually helped responsiveness) stayed.

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
- **Gmail scanner needs a restricted scope + ships off.** `gmail.readonly` is a
  Google *restricted* scope; it can't be added in code, only by re-consenting the
  bot's refresh token. The scanner runs with `GMAIL_SCAN_ENABLED=false` until that
  re-consent and the flag flip (a deliberate rollout step, §3b).
- **Instant-invite `start_time = now`.** Gmail-detected meetings dispatch on the
  next scheduler tick. If the invite email is stale (room already closed) the bot
  attempts to join and fails fast (`failed_join`) — harmless, but expected when
  first enabling with a wide `newer_than` window.

---

## 11. Deploy

See [DEPLOY.md](./DEPLOY.md). Railway (amd64) + Neon. Env vars pushed from
`.env` (`APP_ENV=production`, `LOG_JSON=true`). Migrations run on container start
(`alembic upgrade head`). Health: `GET /health` and `GET /healthz/db`.
Live: `https://centralagent-production-457c.up.railway.app`.
