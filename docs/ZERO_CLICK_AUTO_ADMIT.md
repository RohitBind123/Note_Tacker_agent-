# Zero-Click Auto-Admit — design & plan (the hard 10%)

**Goal:** remove the one remaining manual step — the human "Admit" click — so a
meeting is captured with *zero* human action.

Status: **not built.** This is the planned next workstream. This doc records the
problem, the options, the chosen path, and the honest risks.

---

## In plain English (no jargon)

Think of the bot like a guest you invited to a meeting. Two things have to
happen: it has to **say "yes, I'll come"**, and then it has to **actually get
into the room**.

**1. Saying "yes" (auto-accept) — ✅ already working.**
When you invite `centralagentai@gmail.com` to a meeting, the bot automatically
replies **"yes"** to the invite. You don't do anything. This part is done.

**2. Getting into the room (auto-admit) — ⏳ the part we still want to automate.**
Right now, when the bot shows up at the Google Meet door, Google puts it in a
**waiting room** and asks a real person inside to click **"Admit."** That one
click is the *only* manual step left.

**Why does Google stop it?** Because today the bot walks up as an **anonymous
guest** — it has no name badge, no Google login. Google treats any nameless guest
like a stranger at the door and makes a human approve it.

**How we'll make it fully automatic.** We'll have the bot **log in as its own
Google account** (`centralagentai@gmail.com`) — the *same* account that was
invited. A person who is both **invited** and **logged in** doesn't get stopped at
the door; Google lets them walk straight in. So: same bot, but wearing its name
badge instead of arriving anonymous.

**Why it's the hard part.** To log the bot in, it has to run a real Chrome browser
signed into the bot's Google account, on a stronger type of server (the kind
Google's tools actually run on). And Google is **suspicious of robots that log in
by themselves** — it sometimes blocks automated logins. That's the risky, fiddly
bit, and the reason it's not done yet.

**If it doesn't work, what happens?** Two safety nets:
- The meeting host can simply **turn off the waiting room** ("Quick access"), and
  even today's anonymous bot walks in.
- Or we keep the **single "Admit" click** — everything *else* (detect, accept,
  join, transcribe, summarize, email) is already 100% automatic, so it's one
  click per meeting at most.

**One-line summary:** *the bot already auto-says-yes to invites; to also let it
walk in without a click, we'll have it join while logged in as its own Google
account instead of as an anonymous guest — the tricky part is doing that login
automatically without Google blocking it.*

---

## The problem
The Vexa **cloud** bot joins as an **anonymous guest** (no Google identity).
Google Meet sends unknown participants to a **lobby**, so a human in the meeting
must click **"Admit."** That's the only non-automatic step left in the pipeline.

There are exactly two ways to skip the lobby:

| # | Approach | Skips lobby? | Cost / risk |
|---|---|---|---|
| 1 | **Host disables the lobby** ("Quick access" / Host management) | Sometimes — may admit even an anonymous bot | Free; depends on each host's setting — not reliable across organizers |
| 2 | **Signed-in bot** — the bot joins **as `centralagentai@gmail.com`**, the *invited* attendee | Yes — an invited, signed-in account isn't lobbied | Needs self-hosted Vexa on amd64 + the bot's Google session; fragile (Google bot-detection) |

We auto-RSVP "yes" already, so the bot *is* a confirmed invitee — it just needs to
join **as that identity** instead of anonymously.

---

## Chosen path: signed-in self-hosted bot
Move from the cloud provider to a **self-hosted Vexa** (`authenticated-meetings`)
running on real amd64 infra, where the bot drives a Chrome session **logged in as
the bot account**. Then a signed-in invited participant joins without a lobby.

This composes cleanly with the existing design:
- The `BotProvider` abstraction means this is a **new provider impl**, not an
  engine rewrite: add `SelfHostVexaProvider` next to `CloudVexaProvider`, select
  it by config (`VEXA_PROVIDER=selfhost`).
- Everything downstream (status, transcript, stop, scheduler, Gemini, email) is
  unchanged.

```
   factory.get_provider()
        ├─ CloudVexaProvider     (today: anonymous, lobbied, 1 admit click)
        └─ SelfHostVexaProvider  (target: signed-in as bot, zero-click)   ← new
```

---

## Why it's genuinely hard (the honest risks)
1. **amd64 infra required.** `vexa-lite` is amd64-only; it can't run on the arm64
   dev Mac (qemu/Redis/Chrome all break — CHALLENGES §1.2). Needs a Linux/amd64
   host (Railway service, Fly machine, or a VM).
2. **Bot Google session.** The bot's Chrome must be authenticated as
   `centralagentai@gmail.com` — via stored session cookies or a scripted login.
   - Cookies go stale and must be refreshed.
   - **Google bot-detects automated logins** — Vexa's own cookie-based test had a
     failure here. This is the single biggest unknown.
3. **Headless Chrome in a container** joining real Meet calls is finicky; expect
   iteration on flags, fingerprint, and stability.
4. **Security.** Storing a real Google session for the bot account is sensitive —
   secret-manager only, scoped host, rotation plan.

---

## Implementation sketch (when we build it)
1. **Stand up self-hosted Vexa on amd64** (its own service/VM); confirm the
   `authenticated-meetings` flow works with a manually supplied bot session.
2. **Capture the bot's Google session** (cookies) and inject into the bot's
   browser context; verify a signed-in join skips the lobby on a test Meet.
3. **`SelfHostVexaProvider`** implementing the same `BotProvider` ABC (join /
   status / transcript / stop) against the self-host API.
4. **Config switch** `VEXA_PROVIDER` (`cloud` | `selfhost`) in `factory.py`;
   default stays `cloud` until self-host is proven.
5. **Session-refresh job** (cookies expire) + health alarm if the bot login fails.
6. **Fallback:** if a signed-in join fails, fall back to the cloud bot (anonymous,
   manual admit) so capture still happens.

---

## Interim mitigations (no new infra)
- Ask organizers to **turn off the Meet lobby / enable Quick access** for meetings
  the bot should join (option 1) — works per-meeting, no code.
- Keep the **one admit click** as the accepted cost until self-host is proven —
  the rest of the pipeline is already zero-touch.

See [ARCHITECTURE.md](./ARCHITECTURE.md) (provider abstraction) and
[CHALLENGES.md §1.2, §5.1](./CHALLENGES.md).
