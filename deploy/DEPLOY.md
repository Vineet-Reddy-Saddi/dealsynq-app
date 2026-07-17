# Deploying the DealSynq web app

The app is `fivetownplaza/webapp/server.py` — a small Python web server (stdlib +
`requests`). It needs a host that runs a **persistent Python process** with outbound
internet. Static hosts (GitHub Pages) and serverless functions (Vercel/Netlify/Cloud Run
functions) will **not** work — the second because the web-research feature holds job
state in memory across requests. See the chat discussion for the full rationale.

Everything deploy-related lives in this `deploy/` folder. Three code changes were already
made to make deployment possible:
1. Server binds `HOST` (env var) — set `HOST=0.0.0.0` on the host; local stays `127.0.0.1`.
2. `PORT` is read from the env (hosts inject it).
3. Proxy credentials load from the `DEALSYNQ_PROXY_CONFIG` env var (a secret), falling
   back to the local `axisgis/proxy_config.json` for dev.

---

## Recommended: Render (free web service)

**Prerequisites**
- Push this repo to GitHub. **Do not commit `axisgis/proxy_config.json`** — the root
  `.gitignore` already excludes it. You'll paste its contents as a secret instead.
- The 30 MB `outputs/springfield_ownportal.csv` must be in the repo (it's the parcel
  index). That's under GitHub's 100 MB/file limit — fine to commit. (If you'd rather keep
  the repo light, use Git LFS for that one file.)

**Steps (dashboard, ~4 fields — no root file needed)**
1. Render → **New → Web Service** → connect the GitHub repo.
2. **Runtime:** Python 3.
3. **Build command:** `pip install -r deploy/requirements.txt`
4. **Start command:** `python fivetownplaza/webapp/server.py`
5. **Environment variables:**
   - `HOST` = `0.0.0.0`
   - `PYTHON_VERSION` = `3.12.6`
   - `DEALSYNQ_PROXY_CONFIG` = *(secret)* paste the full JSON contents of
     `axisgis/proxy_config.json`. Optional — omit to run research directly (may
     rate-limit from a datacenter IP).
   - `PORT` is provided by Render automatically — don't set it.
6. **Create Web Service.** First build takes a couple of minutes; you get an
   `https://<name>.onrender.com` URL.

**Blueprint alternative:** copy `deploy/render.yaml` to the repo root and use Render →
New → Blueprint. Same result; the dashboard route above is simplest for a first deploy.

**Free-tier note:** the free instance **sleeps after ~15 min idle** (~30–60 s cold
start). For a scheduled demo, just open the URL a minute beforehand.

---

## What's bulletproof vs. best-effort (important for the demo)

Regardless of host, the **record-card scraper runs direct from the host IP**, and cloud
IPs get rate-limited by the assessor site more than a home IP. So:

- **Instant & reliable everywhere:** Five Town Plaza (fully cached via `PROFILE.json` +
  `RESEARCH.json`) and the featured demo addresses in `fivetownplaza/webapp/precache/`
  (415 Cooley St, 1391 Main St, 115 Cooley St — the example buttons). These load from
  disk, no live call.
- **Best-effort:** any *other* address typed live — the assessor scrape may be slow or
  throttled from a datacenter. That's expected; the demo should lead with the cached set.

To refresh or add cached demo addresses: edit the `DEMOS` list in
`fivetownplaza/precache_demo.py` and run `python -u fivetownplaza/precache_demo.py`,
then redeploy.

---

## Alternatives (also free)

- **Fly.io** — persistent small always-free VM, doesn't sleep; needs a Dockerfile
  (`CMD ["python","fivetownplaza/webapp/server.py"]`, `ENV HOST=0.0.0.0`, expose `$PORT`).
- **Oracle Cloud "Always Free" VM** — a real always-on Linux VM (most control, most
  setup): `pip install -r deploy/requirements.txt`, run under `systemd`, open the port in
  the security list.
- **Avoid:** PythonAnywhere free tier — its outbound whitelist blocks the assessor,
  DuckDuckGo, and the proxies.

---

## Run locally (unchanged)

```
python fivetownplaza/webapp/server.py          # http://localhost:8770/
PORT=9000 python fivetownplaza/webapp/server.py # custom port
```
