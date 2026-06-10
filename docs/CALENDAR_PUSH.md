# Calendar Push (events.watch) — enabling real-time detection

Today detection is **polling** (the poller reads the bot's calendar every 60s).
Push is **built but gated off** because Google requires a verified domain. This
doc is the runbook to turn it on once you own one.

## Why it's off in dev
Google Calendar push (`events.watch`) delivers a webhook to your URL when the
bot's calendar changes — but the webhook host must be on a **domain you've
verified ownership of** (Search Console + the GCP project). ngrok and
`*.up.railway.app` can't be verified, so push can't run there. (CHALLENGES §4.3.)

## The design: push primary + reconcile-poll backstop
We never rely on push alone — a missed/expired channel would make us blind. So:

```
   Google Calendar change
        │  events.watch
        ▼
   POST /webhooks/google/calendar   ── validates X-Goog-Channel-Token
        │   (resource_state != "sync")
        ▼
   calendar_poller.poll_once()      ── same sync the loop uses
        ▲
        │  reconcile backstop: the poller keeps running, just SLOWER in prod
   LOOP 1 (e.g. 600s)               ── catches anything push missed
```

Code already in place:
- `api/routes/webhooks.py` — `POST /webhooks/google/calendar` receiver (token-validated, always 200).
- `services/google/calendar_watch.py` — channel register/stop.
- `runner._maybe_register_calendar_push()` — registers on boot **iff** enabled + https.
- Gated by `CALENDAR_PUSH_ENABLED` + `PUBLIC_BASE_URL`.

## Enabling it (once you have a verified domain)
1. **Point a domain at the Railway service** (Railway → service → Settings →
   Networking → Custom Domain; add the CNAME at your DNS).
2. **Verify domain ownership** in [Google Search Console](https://search.google.com/search-console)
   **with the bot account**, then add it as a **verified domain** in the GCP
   project (APIs & Services → Domain verification).
3. **Set env vars** and redeploy:
   ```
   PUBLIC_BASE_URL=https://your-verified-domain.com
   CALENDAR_PUSH_ENABLED=true
   CALENDAR_WEBHOOK_TOKEN=<the same random secret the receiver checks>
   CALENDAR_POLL_INTERVAL_SECONDS=600   # slow the poller to a backstop
   ```
4. On boot you'll see `calendar_watch` register a channel. Test by creating an
   invite — the webhook should fire `calendar_webhook_received` within seconds.

## Operational notes
- **Channel expiry:** Calendar watch channels expire (max ~7 days for events).
  Add a renewal job (re-register before expiry) — currently a follow-up.
- **Handshake:** the first POST has `X-Goog-Resource-State: sync` (no change) —
  the receiver ignores it; real changes come as `exists`.
- **Security:** the receiver validates `X-Goog-Channel-Token` against
  `CALENDAR_WEBHOOK_TOKEN` and returns 200 regardless (so a spoofer learns
  nothing and Google never retry-storms).
- **Gmail Pub/Sub alternative:** if you never get a domain, Gmail + Pub/Sub gives
  event-driven detection by *pull* (no public URL), at the cost of `gmail.readonly`
  + Pub/Sub setup + 7-day watch renewal. Polling remains the simplest reliable
  fallback.

See [ARCHITECTURE.md §2](./ARCHITECTURE.md#2-pull-vs-push-how-detection-actually-works).
