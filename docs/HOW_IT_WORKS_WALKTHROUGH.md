# How CentralAgent Works — A Simple End-to-End Walkthrough

This is the "explain it like I'm five" version. We follow **one real meeting**
from the moment someone invites the bot, all the way to the summary email landing
in your inbox — and along the way we stop and explain, in plain words, **every
part that does work.** No jargon (and where a technical word is unavoidable, we
define it right there).

If you read only one doc to *understand* the system, read this one.

---

## First, meet the cast (each part, in one sentence)

Think of CentralAgent as a tireless **assistant** named *centralagentai* who you
invite to meetings. Behind that assistant, a few helpers do the actual work:

| Part | In plain words | Its job |
|---|---|---|
| **The assistant's email/calendar** (`centralagentai@gmail.com`) | The bot's own identity | Receives invites; the bot "is" this account |
| **The Watcher** (calendar poller) | A helper who checks the calendar every minute | Notices new invites, says "yes" to them |
| **The Notebook** (database) | A notebook where every meeting is written down | Remembers each meeting and what stage it's at |
| **The Planner** (scheduler) | A helper who checks the clock every 30 seconds | Sends the bot in at the right time, cleans up after |
| **The Bot** (Vexa) | The thing that actually sits in the Google Meet | Joins the call, listens, writes down who said what |
| **The Summarizer** (Gemini AI) | A smart reader | Turns the raw transcript into a tidy summary |
| **The Mailer** (Gmail) | The post office | Emails the summary to whoever organized the meeting |

Two of these helpers — **the Watcher** and **the Planner** — never sleep. They run
in a loop, around the clock, on a small always-on computer in the cloud (Railway).
Everyone writes notes in the same shared **Notebook** so they stay in sync.

> **Why a "Notebook" instead of helpers just talking to each other?** If the
> computer restarts, conversations are lost — but the Notebook isn't. So the
> Notebook is the single source of truth. Every helper reads it, does a bit of
> work, writes back, and moves on.

---

## The story: one real meeting, start to finish

**The setup:** Priya schedules a Google Meet called **"Project Sync,"** sets it for
**10:00–10:30 AM**, and adds **`centralagentai@gmail.com`** as a guest (just like
inviting a colleague). She does nothing else. Here's everything that happens.

---

### Step 1 — The invite arrives (and the bot says "yes")

Within a minute, **the Watcher** does its every-60-seconds check. It looks at the
bot's *own* calendar and sees a new meeting it's been invited to, with a Google
Meet link. It automatically marks the bot as **"Yes, attending,"** then writes the
meeting into the **Notebook** marked **"scheduled."**

> **Under the hood, simply:**
> - The Watcher doesn't get *pinged* when you invite the bot — instead it
>   **asks the calendar "anything new?" every 60 seconds.** This asking-on-a-timer
>   is called **polling**. (There's a fancier "ping me instantly" method called
>   *push*, but it needs a website address we don't have yet, so we poll. It works
>   the same, just up to a minute slower.)
> - To read the calendar, the Watcher first needs a temporary **access pass**. It
>   has a long-term "refresh" key and trades it for a short-lived pass each time —
>   like showing your membership card to get a day-pass. (This is OAuth.)
> - It only cares about meetings that have a **Meet link**; anything else is
>   ignored.
> - **Saying "yes" automatically** matters because Google only puts an invite on
>   your calendar properly once you've responded. So the bot RSVPs "yes" itself.
> - In the Notebook it records: the title ("Project Sync"), the start time
>   (10:00), the end time (10:30), who organized it (Priya), and the Meet link.
>   The meeting's **status** is now **"scheduled."**

> **One real gotcha (explained simply):** times are always stored in a worldwide
> standard clock called **UTC**. If your calendar's timezone is set wrong, "10 AM"
> can get saved as a *different* 10 AM and the bot looks like it's waiting for the
> wrong time. Fix: set your calendar's timezone to your real city.

---

### Step 2 — The Planner waits for the right moment

**The Planner** checks the clock every 30 seconds. Each time, it asks the Notebook:
*"Any meeting that's about to start?"* For most of the morning, "Project Sync" is
still hours away, so the Planner leaves it alone.

> **Under the hood, simply:**
> - "About to start" means **within the next 60 seconds.** So the Planner ignores
>   the meeting until ~9:59:00, then springs into action. We send the bot in
>   *one minute early* so it's already standing at the door when the meeting opens
>   — not scrambling to join after everyone's talking.
> - Because the Planner runs every 30 seconds and "due" means "starts within 60
>   seconds," the bot gets sent in sometime in the last minute before 10:00.

---

### Step 3 — The bot is sent in (just before 10:00)

At about **9:59**, the Planner sees "Project Sync" is due. It **claims** the
meeting (so no one else grabs it), marks it **"joining,"** and tells **the Bot**:
*"Go join this Meet."* The Bot heads to the meeting's front door.

> **Under the hood, simply:**
> - **"Claiming"** means the Planner puts a temporary lock on that meeting row in
>   the Notebook while it works on it. If we ever run two Planners at once, they
>   can't both send a bot to the same meeting. (The technical name is a
>   *row lock that skips already-locked rows*.)
> - It marks the meeting **"joining"** *before* contacting the Bot, so if anything
>   crashes we can tell the bot was already on its way.
> - The Planner tells the Bot to join by sending it the meeting's short code (the
>   `abc-defg-hij` part of the Meet link). The Bot replies with its own ID number
>   so we can check on it later. All of this is written back to the Notebook.
> - Every message to the Bot (and to Google and the AI) has a **time limit and a
>   few automatic retries** — if a request hiccups, we try again a couple of times
>   instead of giving up. (So a momentary network blip doesn't break a meeting.)

---

### Step 4 — Someone clicks "Admit," the bot is in

The Bot reaches the Meet and **knocks** — Google shows *"CentralAgent wants to
join."* A person in the call clicks **Admit.** Now the Bot is inside, and the
meeting status flips to **"active."** From here the Bot quietly **listens and
writes down who said what.**

> **Under the hood, simply:**
> - That one **Admit click is the only manual step in the whole system.** It
>   happens because today the Bot joins as an *anonymous guest*, and Google makes
>   a human approve strangers. (Making even this automatic is the future
>   "zero-click" work — see [ZERO_CLICK_AUTO_ADMIT.md](./ZERO_CLICK_AUTO_ADMIT.md),
>   which has a simple explanation too.)
> - Every 30 seconds the Planner now asks the Bot *"how's it going?"* and updates
>   the Notebook (joining → active). It also fetches the live **transcript** —
>   the text of who said what, with names attached.
> - **A quirk worth knowing:** the Bot's "how many people are here?" number is
>   unreliable on the current setup — it often says **0 even when people are
>   clearly talking.** So we never trust that number for anything; we use the
>   meeting's scheduled end time instead (next step).

---

### Step 5 — The meeting ends

Priya wraps up at 10:30 and leaves. The Bot needs to leave too — and it does,
through whichever of these happens first:

1. **Google tells us the call ended** (the host ended it), or
2. **Someone presses "stop"** in our system, or
3. **The clock passes the scheduled end (10:30) + a 2-minute grace**, and the
   Planner **automatically pulls the Bot out.**

However it ends, the meeting's status becomes **"processing"** — meaning *"the
talking is over, now make the summary."*

> **Under the hood, simply:**
> - That third trigger (the 2-minute auto-pullout) is important because of a real
>   problem we hit: in Google Meet, the red **"Leave call"** button only makes
>   *you* leave — the **Bot keeps sitting in the empty room.** Only the host's
>   "End call for everyone" kicks the bot out. So to be safe, the Planner watches
>   the clock: a little after the meeting was *supposed* to end, it removes the
>   Bot itself. No more bots stuck in empty rooms.
> - All three triggers lead to the **same** next stage ("processing"). We
>   deliberately funnel everything into one path so nothing falls through the
>   cracks.

> **Another real bug we fixed (in simple terms):** the Bot has its own word
> "completed," which to *it* means "I finished recording." We used to mistake that
> for "the summary's been emailed, all done" — so the system thought it was
> finished and **never made the summary.** Now we treat the Bot's "completed" as
> just "recording done → go make the summary," and only *we* declare a meeting
> truly **done** after the email is sent.

---

### Step 6 — Making the summary, then emailing it

Within ~30 seconds of the meeting ending, the Planner picks up the "processing"
meeting and does three quick things:

1. **Grabs the final transcript** from the Bot and saves it in the Notebook.
2. **Hands the transcript to the Summarizer (Gemini AI)**, which reads it and
   returns a tidy report: a short **summary**, plus any **decisions**, **action
   items** (with who owns them), **risks**, and **next steps**.
3. **Emails that report** to Priya (the organizer), from the bot's own address.
   Then it marks the meeting **"completed."** Done.

> **Under the hood, simply:**
> - We ask the AI for its answer in a **fixed shape** (summary / decisions /
>   actions / risks / next steps) so it's always neat and never rambly.
> - **Honesty rule:** if the meeting had no decisions, the report says **"None
>   noted"** — it never *invents* decisions or action items that weren't said.
>   And if the meeting was too short to have any real content, we skip the AI
>   entirely rather than make something up.
> - The email is sent as the bot account using Google's mail service. Once it's
>   sent, and only then, the meeting is officially **"completed."**

---

## The meeting's journey, as a simple status ladder

Every meeting climbs the same ladder. The "status" is just a word in the Notebook
saying which rung it's on:

```
  scheduled   →  the invite is detected, waiting for start time
      ↓
  joining     →  bot has been sent in, knocking at the door
      ↓
  active      →  bot is inside, listening + transcribing
      ↓
  processing  →  meeting ended, making the summary
      ↓
  completed   →  summary emailed — all done ✅
```

If something goes wrong, it lands on a clearly-named rung instead, like
`failed_join` (couldn't get in) or `email_failed` (summary made, email bounced) —
so we always know exactly where it stopped.

---

## What you would actually see

- **In your calendar:** the bot shows as **"Yes"** to the invite (it RSVP'd itself).
- **During the meeting:** one **"Admit"** prompt — click it once.
- **A minute or so after the meeting:** an email titled **"Meeting Insights —
  Project Sync"** with the summary, action items, and so on.
- **Behind the scenes (logs):** a clean trail like
  `poller_upserted → calendar_rsvp_accepted → scheduler_claimed →
  dispatch_existing_ok → refresh_status_change (active) → transcript_stored →
  analysis_stored → report_emailed`.

---

## The whole thing in five sentences

1. You invite the bot to a meeting and do nothing else.
2. A helper checks the bot's calendar every minute, spots the invite, and says
   "yes" for the bot.
3. Just before the meeting, another helper sends the bot in; you click "Admit"
   once and it listens.
4. When the meeting ends (or its time is up), the bot is pulled out and the talk
   is turned into a tidy summary by an AI.
5. That summary is emailed to the organizer — fully automatic from step 2 onward.

---

### Want more depth on any piece?
- The exact diagrams, timings, and components → [ARCHITECTURE.md](./ARCHITECTURE.md)
- Every problem we hit and how we fixed it → [CHALLENGES.md](./CHALLENGES.md)
- The buttons/commands to run it → [DEPLOY.md](./DEPLOY.md)
- How the bot will one day skip even the "Admit" click → [ZERO_CLICK_AUTO_ADMIT.md](./ZERO_CLICK_AUTO_ADMIT.md)
