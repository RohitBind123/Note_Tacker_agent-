# How Docker Works Here — Plain English, End to End

This explains, with no jargon, **how CentralAgent gets packaged and run** using
Docker — from the files on your laptop to a running app in the cloud. If you've
never used Docker, start here.

---

## What is Docker? (the simple idea)

Imagine you cooked a meal and want to mail it to a friend so it tastes **exactly**
the same when they eat it. You don't just send the food — you send a **sealed
lunchbox** with the food *and* everything needed to serve it: the plate, the
cutlery, the right temperature, the instructions.

Docker is that lunchbox for software. It packs your app **plus** everything it
needs to run — the right version of Python, all the libraries, the start
command — into one sealed bundle called an **image**. Anyone, anywhere, can open
that bundle and the app runs **identically**. No "but it worked on my machine."

- **Image** = the sealed lunchbox (a recipe's finished result; read-only).
- **Container** = the lunchbox actually opened and running (a live copy of the image).
- **Dockerfile** = the recipe that says how to build the lunchbox.

> **Why we use it here:** early on we hit the classic "it works on my machine"
> wall — the meeting-bot software was built for one type of computer chip (amd64)
> and the dev Mac is a different type (arm64), so things crashed
> (see [CHALLENGES.md §1.2](./CHALLENGES.md)). Docker on a matching cloud machine
> sidesteps all of that: build the lunchbox once, run it on the right chip in the
> cloud, identical every time.

---

## The recipe: our `Dockerfile`, line by line

This is the whole file (`backend/Dockerfile`), translated into plain English.

```dockerfile
FROM python:3.12-slim
```
**"Start from a small, ready-made kitchen that already has Python 3.12."** We don't
build Python ourselves — we start from an official tiny Linux box that has it.
("slim" = stripped down, so the lunchbox stays small.)

```dockerfile
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app PIP_NO_CACHE_DIR=1
```
**"Set a few house rules."** In plain terms: print logs immediately (don't hold
them back), don't litter the box with temp files, know where our code lives
(`/app`), and don't keep installer junk. Housekeeping that keeps things clean and
logs flowing.

```dockerfile
WORKDIR /app
```
**"Work inside the `/app` folder."** Everything from here happens in that folder —
like saying "set up on this counter."

```dockerfile
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
```
**"Copy the shopping list, then buy all the ingredients."** `requirements.txt`
lists every library the app needs (FastAPI, the database driver, etc.). We copy
*just that list first* and install it. Why first? Because Docker is smart: if the
list hasn't changed since last time, it **reuses** the already-installed
ingredients and skips re-buying them — making rebuilds much faster.

```dockerfile
COPY . /app/
```
**"Now copy in all our actual code."** Everything in `backend/` goes into the box.
(We deliberately leave some things out — see `.dockerignore` below.)

```dockerfile
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```
**"When the box is opened, do this:"** two steps, in order:
1. `alembic upgrade head` — **update the database shape** to the latest (apply any
   new tables/columns). Safe to run every time; if nothing's new, it does nothing.
2. `uvicorn app.main:app ...` — **start the web server**, which boots the app and
   kicks off the two background loops (the calendar Watcher + the Planner).

`--host 0.0.0.0` means "accept visitors from outside the box." `${PORT:-8000}`
means "listen on the door number the cloud gives us, or 8000 if none is given."

---

## What we leave OUT: `.dockerignore`

```
tests/  .venv/  __pycache__/  *.pyc  .pytest_cache/  .ruff_cache/  *.log  .env  .env.*
```
**"Don't pack these in the lunchbox."** Test files, local virtual environments,
caches, logs — none of that belongs in production. **Most importantly, `.env` is
excluded** so our **secrets never get baked into the image.** (Secrets are handed
to the running container separately — see below.)

---

## How the cloud builds and runs it: `railway.json`

This file tells Railway (our cloud host) how to handle the lunchbox.

```json
{ "build":  { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": { "startCommand": "... alembic upgrade head && uvicorn ...",
              "healthcheckPath": "/health", "healthcheckTimeout": 120,
              "restartPolicyType": "ON_FAILURE", "restartPolicyMaxRetries": 3 } }
```
In plain English:
- **build → use our Dockerfile.** "Build the lunchbox using our recipe."
- **healthcheckPath `/health`.** After starting, Railway knocks on the app's
  `/health` door. If the app answers "ok" within 120s, the deploy is live. If it
  never answers, Railway knows the deploy failed.
- **restart ON_FAILURE (up to 3 times).** If the app crashes, the cloud
  automatically restarts it a few times before giving up. Self-healing.

---

## The full journey: from your laptop to "Online"

When you run **`railway up`** (from the `backend/` folder), this happens:

```
  your laptop                      Railway cloud (amd64 machine)
  ───────────                      ─────────────────────────────
  railway up
     │  1. zip up the backend/ folder (minus .dockerignore stuff)
     ├───────────────────────────▶ 2. read the Dockerfile (the recipe)
     │                              3. BUILD the image:
     │                                   - start from python:3.12-slim
     │                                   - install requirements.txt
     │                                   - copy our code in
     │                                 = a sealed image (the lunchbox)
     │                              4. INJECT env vars (DATABASE_URL, keys...)
     │                                 ← these live in Railway, NOT in the image
     │                              5. RUN a container from the image:
     │                                   - alembic upgrade head  (update DB)
     │                                   - uvicorn starts the app + 2 loops
     │                              6. health check /health → 200 ok
     ▼                              7. status: ● Online  ✅
  done
```

Key points in plain words:
- **The image has the code; it does NOT have the secrets.** Railway hands the
  secrets (database URL, API keys) to the container *as it starts*, like slipping
  a note into the lunchbox at serving time. That's why rotating a key is just a
  Railway setting change + restart — no rebuild.
- **The database lives outside the container** (Neon, a separate cloud service).
  The container just connects to it. So restarting/replacing the container never
  loses data.
- **Migrations run first, every start.** Before serving traffic, the container
  brings the database schema up to date. If there's nothing new, it's instant.
- **One container, two loops inside it.** Once `uvicorn` boots, the app starts the
  calendar Watcher (every 60s) and the Planner (every 30s) *inside the same
  container*. No separate worker boxes needed.

---

## Doing it yourself (the commands)

```bash
# Build the lunchbox locally and check it (optional sanity test):
cd backend
docker build -t centralagent-backend:test .

# Deploy to the cloud (build happens on Railway's amd64 machine):
railway up

# See it running / logs:
railway status
railway logs
```

> We build on **Railway's amd64 machine**, not the arm64 Mac, on purpose — same
> chip type as production, no emulation surprises.

---

## One-line summary

Docker packs the app + Python + libraries + start command into one sealed
**image**; Railway builds that image from our `Dockerfile` on a matching cloud
machine, slips in the secrets, runs it (update DB → start server → two background
loops), and keeps it healthy — so it runs **identically** every time, everywhere.

See also: [DEPLOY.md](./DEPLOY.md) (deploy + day-2 operations) and
[ARCHITECTURE.md](./ARCHITECTURE.md) (what runs inside).
