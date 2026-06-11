# CentralAgent — Development Challenges & Resolutions

A complete, honest record of every non-trivial problem hit while building
CentralAgent end-to-end — from de-risking Vexa, through the FastAPI build, to the
Railway deployment and live testing — and exactly how each was solved, **with
reasoning and code**.

Format per item: **Symptom → Root cause (the *why*) → Solution (with snippet).**

Categories:
1. [Infrastructure & local environment](#1-infrastructure--local-environment)
2. [Database (Neon / asyncpg / Alembic)](#2-database-neon--asyncpg--alembic)
3. [Auth & Google APIs](#3-auth--google-apis)
4. [Calendar invite detection](#4-calendar-invite-detection)
5. [Meeting lifecycle & reliability](#5-meeting-lifecycle--reliability)
6. [Deployment (Railway)](#6-deployment-railway)
7. [Tooling & repo hygiene](#7-tooling--repo-hygiene)

---

## 1. Infrastructure & local environment

### 1.1 Vexa Meet-join reportedly broken (GitHub #407)
- **Symptom:** Vexa's public issue #407 reported that the Google Meet bot join
  was broken — the single biggest risk, since the whole product depends on it.
- **Root cause:** Unknown reliability of the join path; we couldn't assume it
  worked.
- **Solution:** **De-risk first.** Before writing any product code, we ran a live
  `POST /bots` at a real Meet against the **cloud** API and watched the bot join.
  It worked → #407 did not block the cloud path. This single test reordered the
  whole project (cloud-first), saving days of building on a broken assumption.

### 1.2 Self-hosting `vexa-lite` impossible on the arm64 Mac
- **Symptom:** Running the self-host image, every process ran under
  `qemu-x86_64`, `redis-cli` segfaulted, and `meeting-api`/`runtime-api` couldn't
  reach Redis.
- **Root cause:** `vexaai/vexa-lite:latest` is **amd64-only** (confirmed via
  `docker manifest inspect` — single-arch, no arm64 variant). On the arm64 Mac it
  runs under qemu emulation; Redis segfaults under qemu, and the bot is
  Playwright + Chrome inside that emulated container — Chrome under x86-on-ARM is
  notoriously broken/unusably slow.
- **Solution:** Pivot to the **Vexa cloud provider** for dev, and design a
  `BotProvider` abstraction so production can self-host on real amd64 infra
  without touching the engine. This is why the codebase has `provider.py` (ABC) +
  `cloud_provider.py` + `factory.py`.

```python
# services/vexa/provider.py — the seam that made the pivot a config change
class BotProvider(ABC):
    @abstractmethod
    async def join(self, native_meeting_id: str, *, bot_name: str) -> JoinResult: ...
    @abstractmethod
    async def get_status(self, native_meeting_id: str) -> BotStatusResult | None: ...
    @abstractmethod
    async def get_transcript(self, native_meeting_id: str) -> TranscriptResult: ...
    @abstractmethod
    async def stop(self, native_meeting_id: str) -> bool: ...
```

> **Lesson:** an abstraction earns its keep the moment the concrete impl becomes
> impossible on your hardware. The amd64/arm64 wall *validated* the design.

### 1.3 Mac disk near-full broke Docker pulls
- **Symptom:** Docker image pulls failed partway through.
- **Root cause:** The Mac was running near-full; Docker needs headroom to unpack
  layers (failed at <3GB free).
- **Solution:** Cleared regenerable caches to free ~15GB; rule of thumb recorded:
  keep >10GB headroom for Docker work.

### 1.4 Port conflicts on the dev machine (3000 / 3001 / 8056)
- **Symptom:** Self-host launch failed — ports 3000, 3001, then 8056 all "in use."
- **Root cause:** The Mac already had several Node dev servers listening (verified
  with `lsof -nP -iTCP:<port> -sTCP:LISTEN`).
- **Solution:** Moot once we abandoned self-host for cloud (1.2). The takeaway:
  probe ports with `lsof` before binding, and treat the dashboard as optional.

### 1.5 Cloud vs self-host token confusion
- **Symptom:** Two Vexa keys existed (`vxa_bot_…`, `vxa_tx_…`); unclear which the
  cloud path needs.
- **Root cause:** The transcription token (`vxa_tx_`) is only for **self-host**,
  where your own container calls `transcription.vexa.ai`. On **cloud**, the bot
  transcribes internally and bills per hour.
- **Solution:** Cloud needs **only** `vxa_bot_…` (`X-API-Key` header). Verified
  by listing running bots (`GET /bots` → HTTP 200). Documented the full cloud
  surface used: `POST /bots`, `GET /bots`, `GET /transcripts/...`, `DELETE
  /meetings/...`.

---

## 2. Database (Neon / asyncpg / Alembic)

### 2.1 asyncpg rejects Neon's libpq URL params
- **Symptom:** Connecting with Neon's connection string failed — asyncpg doesn't
  understand `sslmode` / `channel_binding` query params.
- **Root cause:** Neon hands out a **libpq**-style URL; asyncpg configures SSL via
  `connect_args`, not query params, so those params must be stripped and the
  scheme normalized to `postgresql+asyncpg`.
- **Solution:** A URL normalizer that drops the query string entirely; SSL is
  re-added via an explicit `SSLContext`.

```python
# config.py
def _to_asyncpg_url(raw: str) -> str:
    parts = urlsplit(raw)
    # Drop the query string (sslmode / channel_binding live there).
    return urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, "", ""))
```

### 2.2 PgBouncer breaks server-side prepared statements
- **Symptom:** Intermittent prepared-statement errors against the pooled Neon URL.
- **Root cause:** Neon's pooler (PgBouncer, transaction mode) is incompatible with
  asyncpg's server-side prepared-statement cache **and** SQLAlchemy's.
- **Solution:** Disable both caches and pass an explicit SSL context.

```python
# db/session.py
engine = create_async_engine(
    _disable_prepared_cache(settings.async_database_url),  # adds prepared_statement_cache_size=0
    pool_pre_ping=True, pool_size=5, pool_recycle=300, pool_timeout=30,
    connect_args={
        "ssl": _ssl_context(),
        "statement_cache_size": 0,           # asyncpg-level, required for PgBouncer
        "server_settings": {"application_name": "centralagent"},
    },
)
```

### 2.3 Alembic migrations must use the **direct** (non-pooled) URL
- **Symptom:** Migrations against the pooled URL would hang/fail.
- **Root cause:** Alembic DDL is incompatible with PgBouncer transaction mode.
- **Solution:** Keep a separate `DATABASE_URL_DIRECT` (Neon "Connection pooling"
  OFF) and point Alembic at it explicitly.

```python
# alembic/env.py
config.set_main_option("sqlalchemy.url", settings.async_database_url_direct)
```

### 2.4 Enum status stored **UPPERCASE** — a diagnostic query silently returned nothing
- **Symptom:** A raw-SQL health check for stuck meetings
  (`... WHERE status IN ('scheduled','pending',...)`) returned **0 rows**, even
  though a meeting was visibly stuck in `SCHEDULED`. The query looked correct but
  was a false negative — nearly hid a real bug (see 5.9).
- **Root cause:** The `Meeting.status` column uses
  `SAEnum(MeetingStatus, native_enum=False)`. SQLAlchemy stores the enum's
  **member name** (e.g. `'SCHEDULED'`, `'COMPLETED'` — uppercase), while the API
  serializes the **value** (`'scheduled'` — lowercase) via `.value`. So the
  Python/API layer sees lowercase, but the actual bytes in Postgres are
  uppercase. A lowercase `IN (...)` filter matches nothing.
- **Solution:** Any **raw SQL** against `meeting.status` must use the uppercase
  member names. The ORM (`Meeting.status == MeetingStatus.SCHEDULED`) handles the
  mapping automatically — only hand-written SQL is exposed to the gotcha.

```sql
-- WRONG (matches nothing — column holds member names, not values)
SELECT id FROM meeting WHERE status IN ('scheduled', 'pending');
-- CORRECT
SELECT id FROM meeting WHERE status IN ('SCHEDULED', 'PENDING');
```

> **Lesson:** `native_enum=False` decouples the *stored* representation (member
> name) from the *serialized* one (value). When the ORM and raw SQL disagree,
> check which one the column actually persists before trusting a query's "0 rows."

---

## 3. Auth & Google APIs

### 3.1 OAuth consent kept failing with `403 access_denied`
- **Symptom:** The browser OAuth flow returned `Error 403: access_denied`.
- **Root cause:** The browser was signed into a **different** Google account
  (`bangadu5346@…`), which is not a test user on the bot's OAuth app (consent
  screen = External/Testing).
- **Solution:** Force the bot account in the auth URL and add it as a test user.

```python
# tools/get_refresh_token.py
flow.authorization_url(login_hint="centralagentai@gmail.com", prompt="consent")
```

### 3.2 `calendar.events.readonly` can't list calendars
- **Symptom:** `GET /users/me/calendarList` → 403 even with a valid token.
- **Root cause:** The readonly events scope does not grant calendar-list access.
- **Solution:** Read events directly from the known calendar:
  `GET /calendars/primary/events`. (`CalendarClient(calendar_id="primary")`.)

### 3.3 Dev credentials expire
- **Symptom:** Tokens/keys stop working after a while.
- **Root cause:** OAuth apps in **Testing** mode issue refresh tokens that expire
  in ~7 days; Vexa **free-tier** bot keys expire ~1h.
- **Solution (documented, not yet applied):** Publish the OAuth app to
  **Production** for a non-expiring refresh token; use a paid Vexa key for
  sustained runs. Until then, re-run `tools/get_refresh_token.py` to refresh.

---

## 4. Calendar invite detection

### 4.1 Invited event never appears on the bot's calendar
- **Symptom:** `POST /admin/poll-calendar` → `upserted: 0`; querying the bot's
  calendar directly showed **0 events** in the window.
- **Root cause:** Classic Google behavior — Google only adds an invitation to your
  calendar **after you respond**. With read-only scope the bot couldn't auto-RSVP,
  so un-responded invites stayed invisible to the API.
- **Solution (two layers):**
  1. One-time bot-account setting: **Calendar → Event settings → "Add invitations
     to my calendar" → "From everyone."**
  2. Code: widen OAuth to `calendar.events` (read-write) and **auto-RSVP "yes"**
     in the poller, so the event is confirmed and reliably present.

```python
# calendar_poller.py — auto-RSVP un-responded invites each poll
for ev in events:
    if ev.self_response_status == "needsAction" and ev.raw.get("attendees"):
        await client.accept_invite(ev.event_id, ev.raw["attendees"])  # PATCH self -> accepted
```

> Production-robust alternative (logged): detect the invite from the bot's
> **Gmail** so it's fully independent of any calendar setting.

### 4.2 Timezone confusion — meetings looked hours off
- **Symptom:** An event created at "2:15 AM" was scheduled by us for ~5.5h later;
  the scheduler "refused" to dispatch. Looked like a bug.
- **Root cause:** The user's Google Calendar timezone was **GMT+00 (UTC)**. "2:15
  AM" was stored as `02:15Z` = 7:45 AM IST. Our parser was **correct** — it stored
  true UTC; the mismatch was the calendar's display zone vs the user's wall clock
  (IST, UTC+5:30).
- **Solution:** Always parse to UTC (already done); the user fix is to set the
  calendar timezone to their actual zone (Asia/Kolkata). Once changed, scheduling
  became intuitive.

```python
# services/google/calendar.py
def _parse_dt(node):
    value = node.get("dateTime")                       # timed events only
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
```

### 4.3 Calendar push needs a verified domain — can't use ngrok
- **Symptom:** Wanted event-driven push instead of polling, but couldn't register
  `events.watch`.
- **Root cause:** Google Calendar push requires the webhook URL to be on a
  **domain you've verified ownership of** (Search Console + GCP). ngrok and
  `*.up.railway.app` can't be verified.
- **Solution:** Build the **push + reconcile-poll** pattern: push is the
  primary real-time trigger (activates on a verified domain in prod), the poller
  is the always-on reliability backstop. Gated by config so dev just runs the
  poller.

```python
# runner.py
async def _maybe_register_calendar_push():
    if not settings.calendar_push_enabled:
        log.info("calendar_push_disabled", reason="poller is primary"); return
    if not settings.public_base_url.startswith("https://"):
        log.warning("calendar_push_skipped", reason="PUBLIC_BASE_URL not https"); return
    await calendar_watch.register_watch()
```

---

## 5. Meeting lifecycle & reliability

### 5.1 Cloud bot is anonymous → Meet lobbies it (manual admit)
- **Symptom:** The bot didn't auto-join; it sat in the "wants to join" lobby.
- **Root cause:** The cloud bot has no Google identity → Google Meet lobbies
  unknown participants.
- **Solution:** Accept one manual **Admit** click for now; defer true zero-click
  to a **signed-in self-hosted bot** (Vexa `authenticated-meetings`, amd64). Logged
  as a parallel workstream with the honest risk that Google bot-detects automated
  logins.

### 5.2 Bot "failed_join" within 30s — empty room
- **Symptom:** Dispatched bot went `joining → failed_join` in ~30s with
  `participants=0`.
- **Root cause:** Creating a calendar event doesn't open the Meet room. The bot
  arrived at an inactive/empty room → nothing to join.
- **Solution:** A human must actually be **in** the Meet when the bot joins.
  Operational fix, documented; the scheduled-time auto-dispatch assumes the host
  is present at start.

### 5.3 The big one — insight email stalled; bot lingered
- **Symptom:** After a real meeting, no email arrived for over a minute; the bot
  stayed in the Meet.
- **Root cause (two coupled bugs):**
  1. `/stop` only called `provider.stop()` — it **never changed the DB status**.
  2. The scheduler only advanced to `PROCESSING` when Vexa's `get_status` said the
     bot was gone — but **Vexa keeps reporting `active` for 1–2 min after a stop**
     (and after a human leaves), so the lifecycle stalled. The only thing that
     ever freed a lingering bot was a **3-hour** hard timeout.
- **Solution:** Decouple *detecting the end* from *running the pipeline*.
  - `/stop` → `orchestrator.stop_meeting` sets `PROCESSING` immediately (don't wait
    on Vexa).
  - New `scheduler.process_pending` is the single, idempotent finalize path.
  - `advance_active` **auto-stops** a bot lingering past `end_time` + grace.

```python
# orchestrator.stop_meeting — finalize without waiting on the provider's stale flag
if meeting.status in (MeetingStatus.JOINING, MeetingStatus.ACTIVE):
    meeting.status = MeetingStatus.PROCESSING
    meeting.end_time = meeting.end_time or _now()
    await db.commit()

# scheduler.end_reason — reliable, testable stop decision (NO participant count)
def end_reason(meeting, now, *, grace_seconds, hard_timeout):
    if meeting.end_time and now >= meeting.end_time + timedelta(seconds=grace_seconds):
        return "past_end_time"
    if meeting.bot_dispatched_at and now >= meeting.bot_dispatched_at + hard_timeout:
        return "hard_timeout"
    return None
```

### 5.4 Vexa `participants_count` is unreliable
- **Symptom:** Status logs showed `participants=0` even while a human was clearly
  present and speaking.
- **Root cause:** On the cloud provider, `participants_count` is not populated
  reliably. **Proof from production logs (meeting #244):** `participants=0` for the
  entire 9 minutes the human spoke (9 transcript segments captured), flipping to
  `1` only at the instant Vexa reported `completed`.
- **Solution:** **Never** gate logic on participant count. Auto-stop keys off
  `end_time` (5.3). Documented so no one "fixes" it by trusting the count later.

### 5.5 "End call for everyone" vs "Leave call"
- **Symptom:** User left via the red button but the bot stayed; expected it to end.
- **Root cause:** Google Meet's red button = **"Leave call"** (only you leave; the
  room and bot stay). Only the host's **"End call for everyone"** evicts the bot.
- **Solution:** Don't rely on how the user leaves — the `end_time`+grace auto-stop
  (5.3) is the reliable bot-exit.

### 5.6 Ended meeting sent **no** email — status-vocabulary collision
- **Symptom:** Meeting #244 reached `completed` but had no report, no transcript,
  no email.
- **Root cause:** `_VEXA_TO_STATUS["completed"]` mapped to our **terminal**
  `COMPLETED`. When the user ended the call, Vexa reported `completed`, so
  `refresh_status` marked the meeting fully done — and `process_pending` (which
  handles only `PROCESSING`) **never ran the pipeline.** Vexa "completed" means
  *recording finished*; our `COMPLETED` means *insights emailed* — different facts.
- **Solution:** Map **all** "meeting ended" Vexa statuses to `PROCESSING`; our
  `COMPLETED` is owned **solely** by `send_report_email`. A guard test enforces it.

```python
# orchestrator.py
_VEXA_TO_STATUS = {
    ...
    "processing": MeetingStatus.PROCESSING,
    "completed":  MeetingStatus.PROCESSING,  # recording done -> run OUR pipeline
    "stopped":    MeetingStatus.PROCESSING,
}

# tests/test_status_mapping.py
def test_no_vexa_status_maps_to_completed():
    assert MeetingStatus.COMPLETED not in _VEXA_TO_STATUS.values()
```

### 5.7 Data quality — never fabricate insights
- **Symptom (preventive):** LLM summaries can invent decisions/action items that
  weren't said.
- **Root cause:** Empty sections coerced to fake content mislead the reader.
- **Solution:** Empty report sections render **"None noted"**; a short/empty
  transcript **skips the model entirely**. Verified live (empty decisions/risks
  stayed empty, no hallucination).

### 5.8 Insight email arrived **~12 minutes** after the host left
- **Symptom:** After a real meeting, the host clicked the red **"Leave call"**
  button and the insight email didn't land for **~12 minutes**. End-to-end the
  pipeline was correct — it was just slow.
- **Root cause:** "Leave call" only removes *you*; the room stays open and the bot
  is now **alone** in it. Vexa's default `automatic_leave.max_time_left_alone` is
  **900000 ms (15 minutes)** — so the bot sits in the empty room for ~15 min before
  Vexa marks the meeting ended. Our finalize pipeline keys off *the bot being gone*
  (5.3), so it couldn't run until Vexa let the bot leave. We had never overridden
  the default, so we inherited the full 15-minute linger. (Confirmed against the
  cloned Vexa repo: `POST /bots` accepts `automatic_leave: {max_time_left_alone,
  no_one_joined_timeout, ...}` in ms.)
- **Solution (two config knobs, nothing hardcoded):**
  1. Send our **own** `automatic_leave` on every dispatch: leave **45 s** after the
     room empties (`VEXA_LEAVE_WHEN_ALONE_SECONDS=45`), and give a no-show meeting
     **5 min** to start (`VEXA_NO_ONE_JOINED_TIMEOUT_SECONDS=300`).
  2. Tighten the scheduler loop **30 s → 20 s** (`SCHEDULER_INTERVAL_SECONDS=20`) so
     end-detection + finalize fire sooner once the bot is gone.
  - **Result:** post-leave latency **~12 min → ~1 min**. ("End call for everyone"
    evicts the bot instantly, so that path is even faster.) Applies to **new**
    dispatches; the 45 s is the irreducible "confirm everyone's gone" wait —
    cutting it further needs Vexa webhooks, but the bot must still leave first.

```python
# services/vexa/cloud_provider.py — override Vexa's slow 15-min default per dispatch
payload["automatic_leave"] = {
    "max_time_left_alone": settings.vexa_leave_when_alone_seconds * 1000,   # 45s -> ms
    "no_one_joined_timeout": settings.vexa_no_one_joined_timeout_seconds * 1000,  # 300s
}
```

> **Why config, not constants:** these are timing trade-offs we expect to tune in
> production (a flaky network might want 60 s alone), so they live in `config.py` +
> env, never baked into the call site.

### 5.9 Meetings stuck in `SCHEDULED` **forever** — the "zombie" bug
- **Symptom:** Meeting **#83** sat in `SCHEDULED` for **3.5 hours** — no bot was
  ever dispatched, and it never moved to a failed state either. It just hung. A
  health scan found it lingering with `vexa_bot_id = NULL` long after its start
  time had passed.
- **Root cause:** A **gap between two passes** that each looked correct alone:
  - `dispatch_due._claim_due` deliberately only claims a meeting whose start is
    within the last **30 minutes** (`_STALE_AFTER`): `start_time >= now - 30m`. This
    is right — you don't want a bot joining a meeting that began an hour ago.
  - `recover_stale` rescued meetings stuck in `JOINING`/`ACTIVE`, but had **no
    branch** for a meeting whose start was missed by >30 min before any bot was sent
    (e.g. the app was redeploying at start time, or the invite was detected late).
  - So such a meeting was **never claimable** (too old for `_claim_due`) and
    **never retired** (no `recover_stale` branch) → a permanent zombie: not run,
    not failed, just stuck. Nothing in the system would ever touch it again.
- **Solution:** Add the missing branch. A pure, unit-testable helper decides
  "missed", and `recover_stale` retires those rows to `FAILED_JOIN` with an
  explicit reason. The boundary is aligned **exactly** with `_claim_due` (strict
  `>`), and `recover_stale` runs **before** `dispatch_due` in the same tick, so no
  meeting can be both retired and dispatched in one pass.

```python
# scheduler.py — pure, testable boundary (mirrors _claim_due's lower bound exactly)
def dispatch_window_missed(meeting, now, *, stale_after) -> bool:
    # _claim_due claims start_time >= now - stale_after (boundary still claimable),
    # so a meeting is "missed" only when strictly older: now > start + stale_after.
    return meeting.start_time is not None and now > meeting.start_time + stale_after

# scheduler.recover_stale — retire the unrecoverable instead of leaving it to hang
missed = (await db.execute(select(Meeting).where(
    Meeting.status.in_([MeetingStatus.SCHEDULED, MeetingStatus.PENDING]),
    Meeting.vexa_bot_id.is_(None),
    Meeting.start_time.is_not(None),
    Meeting.start_time < now - _STALE_AFTER,
))).scalars().all()
for m in missed:
    m.status = MeetingStatus.FAILED_JOIN
    m.failure_reason = "missed dispatch window: scheduled start passed by more than 30 minutes..."
    log.warning("recovered_missed_window", meeting_id=m.id, start_time=m.start_time.isoformat())
```

- **Why `FAILED_JOIN` and not a new `EXPIRED` enum:** adding an enum member is a
  schema/migration change with churn; the existing `FAILED_JOIN` already means
  "we never got a bot into the meeting," and `failure_reason` carries the precise
  distinction ("missed dispatch window..."). Cheapest correct fix.
- **Verified live, not assumed:** 5 new unit tests (incl. the exact boundary case)
  → **45/45 pass**; committed `8120c5a`, deployed; on the new container's **first**
  scheduler tick the log showed `recovered_missed_window meeting_id=83`; the DB then
  showed #83 = `FAILED_JOIN` with the reason, and **0 zombies** remaining. Because
  the fix is in the *rule*, no future meeting can hang this way again.

> **Lesson:** when one pass owns a lower bound (`_claim_due`: "only recent
> meetings"), **some other pass must own everything below it.** A guard that says
> "don't act" without a paired rule that says "then close it out" is how rows leak
> into limbo. Recovery coverage is now total: stuck `JOINING` → `SCHEDULED`;
> ancient `JOINING`/`ACTIVE` → `PROCESSING`; missed-window `SCHEDULED`/`PENDING` →
> `FAILED_JOIN`.

---

## 6. Deployment (Railway)

### 6.1 Railway CLI rejects empty env-var values
- **Symptom:** Bulk `railway variables --set` aborted on `PUBLIC_BASE_URL=`.
- **Root cause:** The CLI rejects `KEY=` with no value.
- **Solution:** Skip empty-valued keys during the bulk push; set
  `PUBLIC_BASE_URL` after the first deploy (once the domain exists).

```bash
key="${line%%=*}"; val="${line#*=}"
[ -z "$val" ] && continue            # skip empty values (e.g. PUBLIC_BASE_URL)
args+=(--set "$line")
```

### 6.2 Railway project link is directory-scoped (and `railway up` from root is worse than a no-op)
- **Symptom:** `railway variables ...` from the repo root → "No linked project."
  Worse: `railway up` from the repo root didn't error — it **silently created a
  brand-new stray project** named `centralagent` (status Failed, no domain) instead
  of deploying to the live one.
- **Root cause:** `railway init` linked the project to `backend/`; the link is
  **per-directory**. From an unlinked directory, `railway up` falls back to
  "create a new project," so a wrong CWD doesn't fail loudly — it forks your deploy
  target. The trap is that **Bash CWD persists between tool calls**: a `cd <root>`
  for an earlier `git` command left the shell at root, so the next `railway up` hit
  the wrong (unlinked) directory.
- **Solution:** Run **all** `railway` commands from `backend/` (where the Dockerfile
  and the link live). Before any `railway up`, confirm the target with
  `railway status` — it must show the live project / real URL, not a fresh one.

```bash
cd /Users/rohitbind/Desktop/centralagent/backend   # the ONLY linked dir
railway status   # verify: project beece035... / https://centralagent-production-457c.up.railway.app
railway up       # deploy to the live service
```

> **Lesson:** a CLI that "helpfully" creates a resource when it can't find a link
> turns a wrong working directory into a silent duplicate. Treat directory-scoped
> CLIs (Railway, Terraform workspaces, gcloud configs) like a loaded `cd` — verify
> the target before any mutating command.

### 6.3 `railway logs` is a snapshot, not a stream
- **Symptom:** A backgrounded `railway logs | grep` exited immediately instead of
  tailing.
- **Root cause:** The command returns the current log buffer and exits; it doesn't
  hold a live stream the way we assumed.
- **Solution:** Poll `railway logs` repeatedly in a loop and diff, rather than
  relying on a long-lived tail.

### 6.4 Production config fail-fast
- **Decision (not a bug):** In production the app **refuses to boot** if any
  required secret is missing — better a clear startup failure than a silent
  half-working service.

```python
# config.py
def missing_required(self) -> list[str]:
    required = {"DATABASE_URL": ..., "VEXA_API_KEY": ..., "GEMINI_API_KEY": ...,
                "GOOGLE_OAUTH_CLIENT_ID": ..., "GOOGLE_OAUTH_CLIENT_SECRET": ...,
                "GOOGLE_OAUTH_REFRESH_TOKEN": ..., "BOT_GOOGLE_EMAIL": ...}
    return [name for name, value in required.items() if not value]
```

---

## 7. Tooling & repo hygiene

### 7.1 `.gitignore` swallowed our own source
- **Symptom:** `app/services/vexa/` (our provider code) wasn't being committed.
- **Root cause:** A broad `vexa/` ignore line (meant for the vendored reference
  clone at the repo root) matched our app package too.
- **Solution:** Anchor the ignore to the root only.

```gitignore
# Vendored reference clone (root-level only; NOT our app/services/vexa)
/vexa/
```

> **Lesson:** anchor vendored-clone ignores with a leading `/` so they can't match
> same-named directories deeper in the tree.

---

## Meta-lessons

- **De-risk the scariest external dependency first** (1.1). One live test
  reordered the entire project.
- **Abstractions prove themselves at the platform wall** (1.2). The
  `BotProvider` seam turned an "impossible on this hardware" into a config switch.
- **Distinguish *the provider's* vocabulary from *your* domain states** (5.6).
  "Recording finished" ≠ "insights emailed."
- **Don't trust unreliable signals just because they exist** (5.4). Verify with
  real logs; key logic off facts you can trust (`end_time`).
- **Decouple detection from action** (5.3). A single idempotent convergence point
  (`process_pending`) made the lifecycle robust to stale provider flags.
- **Timezone bugs are usually display bugs** (4.2). Store/compare UTC everywhere;
  the surprise is almost always the calendar's zone vs the wall clock.
- **Inherited defaults are decisions you didn't make** (5.8). A third party's
  15-minute "leave when alone" default became *our* 12-minute latency until we
  overrode it. Audit the defaults of every external call on a hot path.
- **Every lower bound needs a paired catch-all** (5.9). The dispatcher's "only
  recent meetings" guard was correct, but nothing owned the rows below it, so they
  leaked into limbo forever. A "don't act" rule must be paired with a "then retire
  it" rule, or things get stuck silently.
- **Verify a query before trusting its zero** (2.4 / 5.9). The zombie nearly hid
  behind a lowercase `IN (...)` that matched nothing because the column stores
  uppercase enum names. "0 rows" is a claim to verify, not a fact to trust.
- **Fix the rule, not the row** (5.9). We retired meeting #83 by changing the
  *scheduler's logic*, not by hand-patching one record — so the class of bug is
  closed, not just one instance.

See also: [ARCHITECTURE.md](./ARCHITECTURE.md) (how it all fits together) and
[DEPLOY.md](./DEPLOY.md) (the deploy runbook).
