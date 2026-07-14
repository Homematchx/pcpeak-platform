# Front-end scrape trigger — integration runbook

Isolated tests are green (backend `test_scrape_jobs.py` 29/29, `test_scrape_worker.py` 18/18, browser
async-flow verified). Those prove the *pieces* — the exact place the old "Run button" bugs lived. They do
**not** prove the whole loop: browser → cloud queue → Mac worker → real portal scrape → result back. That
end-to-end check is yours to run. Two ways, local first (no deploy, no risk), then live.

## Architecture recap

The browser can't scrape (no cloud browser; scraping is deliberately local). It **enqueues** a job in the
cloud; the Mac **worker polls that queue outbound**, claims a job, runs the real `discover.py` CLI locally,
and reports status + a preview back. Nothing connects *into* the Mac. Scraped cases land **held**
(`prod_ready=0`) — the trigger scrapes, it does not publish. Two fail-closed tokens gate it:
`SCRAPE_TRIGGER_TOKEN` (who may enqueue/spend credits) and `SCRAPE_WORKER_TOKEN` (who may drain the queue).

---

## A. Local end-to-end (recommended first — no deploy, no credits if you use a stub)

Prove the plumbing entirely on your Mac against a local backend.

```bash
cd ~/Downloads/pcpeak_platform

# 1. Backend with both tokens set (fail-closed: unset ⇒ endpoints 503)
export SCRAPE_TRIGGER_TOKEN=dev-trigger
export SCRAPE_WORKER_TOKEN=dev-worker
(cd backend && python3 main.py)          # serves on :8000

# 2. In a second terminal: the worker, pointed at the local backend
cd ~/Downloads/pcpeak_platform
export SCRAPE_WORKER_TOKEN=dev-worker
PROD_URL=http://localhost:8000 python3 scrape_worker.py     # ← THE worker command

# 3. In a third terminal: enqueue a job as the browser would
curl -s -X POST http://localhost:8000/api/scrape-jobs \
  -H 'Content-Type: application/json' -H 'X-Scrape-Token: dev-trigger' \
  -d '{"case_number":"TX-26-00009"}'
# → {"job_id":N,"status":"queued","label":"TX-26-00009"}

# 4. Watch: the worker terminal claims it, runs discover.py (REAL scrape — hits the portal,
#    spends credits), then prints "✓ done job N ... held". Poll the job to see the result:
curl -s http://localhost:8000/api/scrape-jobs/N -H 'X-Scrape-Token: dev-trigger' | python3 -m json.tool
```

Expect: `status` walks `queued → claimed → running → done`; `result.cases[0].prod_ready == 0` (held); the
case is in the **local** DB but NOT published. To smoke the flow with **no credits/portal**, point the worker
at the test stub first: `SCRAPE_DISCOVER_CMD="python3 test/stub_discover.py"` (any script taking `--case`).

---

## B. Live end-to-end (the real thing)

1. **Deploy** (from your Mac clone — the `git merge main` gate is clean again after this session's reconcile):
   ```bash
   git fetch origin && git checkout production && git merge origin/main && git push origin production
   git checkout main
   ```
   The endpoints ship **fail-closed** — inert (503) until the tokens below are set, so this deploy changes
   nothing user-facing until you configure it.

2. **Set the two tokens in Railway** (service env; I can't set these from the build sandbox):
   `SCRAPE_TRIGGER_TOKEN` and `SCRAPE_WORKER_TOKEN` — two different strong values. Store them alongside the
   other secrets in `Anthropic_API_KEY.env`.

3. **Run the worker on your Mac** (the single command; keep it running):
   ```bash
   cd ~/Downloads/pcpeak_platform
   export SCRAPE_WORKER_TOKEN=<the worker token you set in Railway>
   python3 scrape_worker.py            # defaults PROD_URL to https://taxforeclosureanalyzer.com
   ```

4. **Trigger from the live site**: open taxforeclosureanalyzer.com → *Load New Deals* → paste a case #
   (`TX-26-00009`) or a pattern (`TX-26`) → **Scrape on my Mac**. First use prompts for
   `SCRAPE_TRIGGER_TOKEN` (held in `sessionStorage`, cleared on tab close).

**What to confirm (the real check):** the status walks queued → scraping → ✓ Scraped; the preview lists the
case with its guardrail outcome and **held**; the worker terminal shows discover.py running; and the case is
**not** on the live site until you approve it — `python3 sync_to_prod.py --pending` lists it,
`--approve "TX-26-00009"` then `sync_to_prod.py` publishes it. That last part is the prod_ready gate doing its
job: the trigger fills the review queue, it never auto-publishes.

## Stop / restart

The worker is a plain foreground process — Ctrl-C to stop, re-run to resume (it's stateless; the queue is the
state). A job stuck `running` because the worker died mid-scrape stays claimed; re-running discover.py by hand
or clearing that row is the manual recovery (a `--reset-stale` flag is a noted future add).
