# Deploying for 24/7 operation

## Status: NOT YET DEPLOYED, NOT YET TESTED
Everything below is real, working configuration — but it has never
actually been deployed or run on Railway/Render/Fly.io. This sandbox
has no Docker and no access to any of these platforms. Treat this as a
carefully-written starting point, not a proven path — the first real
deployment attempt is likely to surface at least one thing that needs
fixing, the same way every other "first live run" has in this project
so far.

## Why Railway (recommended)
- Deploys straight from a git push, auto-detects this is a Python app
  (or uses the Dockerfile if present — it does here)
- Runs a normal long-running container, so the scheduler and executor
  background threads work exactly as they do on your machine
- Offers a one-click managed Postgres add-on on the same bill, which
  also resolves the still-open "set up real Postgres" item
- Predictable ~$5/mo floor (Hobby plan) once you're past free-trial credits

## CRITICAL: SQLite will NOT survive here
Railway (and Render, and Fly.io) run your app in a container with an
**ephemeral filesystem** — every redeploy wipes local files, including
`holding_os.db`. Deploying with SQLite as-is means losing all your
businesses/agents/tasks/opportunities on the next deploy. This is not
optional to fix — set up real Postgres as part of this same step, not
as a separate later task.

## Deploy steps (Railway)
1. Push this code to a GitHub repo (Railway deploys from a repo, not a
   zip upload).
2. At railway.app, create a new project, "Deploy from GitHub repo",
   pick your repo.
3. In the same Railway project, click "+ New" → "Database" → "Add
   PostgreSQL". Railway provisions it and gives you a `DATABASE_URL`
   automatically available as an env var to your other service.
4. On your app service, go to Variables and confirm `DATABASE_URL` is
   set (Railway usually references the Postgres service's variable
   automatically — check it's actually pointing at the Postgres
   service, not blank).
5. Also set `ANTHROPIC_API_KEY` as a variable (Settings → Variables).
   Never commit it to the repo. For the Automated Stock Trading vertical,
   also set `ALPHAVANTAGE_API_KEY` (free key at alphavantage.co) — without
   it, `trading_cycle` tasks fail loudly instead of trading on fabricated
   prices, which is correct behavior but means the feature is otherwise
   invisible.
6. Railway should detect the `Dockerfile` and build/deploy from it. If
   it instead tries to auto-detect Python directly, that's fine too —
   it'll pick up `requirements.txt`. Either path should work; if one
   fails, try forcing Docker build in Railway's settings.
7. Once deployed, Railway gives you a public URL
   (`something.up.railway.app`). Open `https://that-url/dashboard`.

## If you'd rather use Render or Fly.io instead
The Dockerfile is portable — the app itself doesn't change.
- **Render**: "New Web Service" → connect repo → it detects the
  Dockerfile. You'll need the paid Starter tier ($7/mo) for a
  background-thread-capable, always-on service — the free tier sleeps
  on inactivity, which would silently kill the scheduler/executor
  threads. Add a Render Postgres instance the same way (New →
  PostgreSQL), and set `DATABASE_URL`/`ANTHROPIC_API_KEY` as
  environment variables on the web service.
- **Fly.io**: requires installing the `flyctl` CLI on your machine,
  then `fly launch` in this directory (it reads the Dockerfile
  automatically), `fly postgres create` for a database, `fly secrets
  set ANTHROPIC_API_KEY=...`. Fly's current free-allowance situation
  was genuinely unclear from what I could find — confirm actual costs
  on fly.io's own pricing page before committing.

## After deploying, verify for real (don't just assume it works)
1. Open `https://your-url/dashboard` — does it load?
2. Create a business, an agent, a task — does the same flow that
   worked locally still work?
3. Create a scheduled job with a 30s interval — does a new task appear
   on its own after ~30-60 seconds, proving the background thread
   survived deployment (this is the part most likely to have platform-
   specific gotchas — some platforms are stricter about long-running
   background threads inside a request-serving process)?
4. Submit a real Opportunity Discovery research request — does it
   complete with a real assessment?
5. Restart/redeploy the service, then check that your businesses/
   agents/tasks are still there — this proves Postgres (not the
   ephemeral local disk) is actually being used.

Report back whatever happens at each step — especially anything that
fails, since that's exactly the kind of platform-specific issue no
amount of local testing can catch in advance.
