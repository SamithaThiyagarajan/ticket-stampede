# Ticket Stampede

A ticket-selling backend that sells **exactly 100 tickets** when tens of thousands of purchase requests arrive within about a minute, and stays correct when its database fails. It comes with a load-testing buyer that independently verifies correctness from raw responses and the final database state.

**The correctness problem.** A naive seller reads the sold count, checks capacity, then writes — so many concurrent buyers read the same count and the service oversells and issues duplicate ticket numbers (measured: 466–570 tickets issued for 100). This implementation guarantees, and every run is checked for:

1. no overselling (at most 100 tickets),
2. no duplicate ticket numbers,
3. idempotency — the same `request_id` always returns the same ticket, never a second one,
4. response consistency — every "you got ticket N" matches the final database, and nobody told "sold out" holds a ticket,
5. `sold_count` equals the number of ticket rows.

How and why the design evolved: **[DECISIONS.md](DECISIONS.md)** (two pages). Full working log with every run: `DECISIONS_LOG.md`.

## Architecture

```
buyer (load generator) ──> nginx (round-robin) ──> seller1 / seller2 / seller3 ──> PostgreSQL
                                                    (stateless FastAPI)            (the single source of truth)
```

Each purchase is one PostgreSQL transaction that locks the sale row (`SELECT … FOR UPDATE`), checks the `request_id`, checks capacity, inserts the ticket and increments `sold_count`. Constraints (primary key, `UNIQUE(request_id)`, `sold_count ≤ total`) back this up. After sell-out, a lock-free read answers "sold out" from one database snapshot. The default `docker compose up` starts **one seller + PostgreSQL**; the three-seller cluster behind nginx is the `cluster` profile.

| Path | Contents |
|---|---|
| `seller/` | FastAPI service (`app.py`), naive and safe schemas, Dockerfile |
| `buyer/` | load generator + verifier (`buyer.py`), PostgreSQL sampler, verifier tests |
| `lb/` | nginx configuration for the three-seller cluster |
| `scripts/` | benchmark runners (`bench.sh`, `bench_cluster.sh`), fault injector, evaluation and replay checks, viewer builder |
| `diag/` | test-only diagnostics: COMMIT-reply-drop proxy, packet-capture analysis, fault-delay calibration |
| `results/` | saved raw evidence of every experiment (per-request records, logs, CPU and database samples) |
| `viewer/` | offline HTML evidence report (`index.html`) |
| `logs/` | AI coding-session logs |

## Prerequisites

- Docker with Compose v2. Ports **5432, 8000, 8080** free.
- For the scripts in `scripts/` and `diag/`: a POSIX shell with GNU `date` (Linux, or Git Bash on Windows; macOS's default `date` lacks `%N`), `curl`, and Python 3.12 with the buyer's packages on the host:
  ```bash
  pip install -r buyer/requirements.txt
  ```
  The buyer itself runs inside Docker; the host Python is needed for the evaluation scripts (`scripts/*.py` import `buyer/buyer.py`).

## Quick start (clean checkout)

```bash
git clone <this repository> ticket_stampede && cd ticket_stampede
docker compose up -d --build        # PostgreSQL + one seller on http://localhost:8000
```

The seller defaults to `SELLER_MODE=safe` and **`WORK_DELAY_MS=50`**: a simulated 50 ms of work inside each purchase, used to make race windows visible. Benchmarks set it explicitly (the headline runs use 0). To start without it:

```bash
WORK_DELAY_MS=0 docker compose up -d
```

### Smoke test

```bash
curl -s -X POST localhost:8000/reset -H 'Content-Type: application/json' -d '{"tickets":3}'
curl -s -X POST localhost:8000/buy -H 'Content-Type: application/json' -d '{"user_id":"alice","request_id":"r1"}'   # {"ticket_number":1}
curl -s -X POST localhost:8000/buy -H 'Content-Type: application/json' -d '{"user_id":"alice","request_id":"r1"}'   # same ticket again
curl -s localhost:8000/status       # sold count and every ticket_number -> user_id
```

`POST /reset {"tickets": N}` wipes all state and starts a new sale. `POST /buy {user_id, request_id}` returns `{"ticket_number": n}`, or HTTP 409 `{"sold_out": true}`; re-using a `request_id` with a different user returns 422. `GET /status` returns the sold count and the ticket list.

### Load test with verification

```bash
docker compose run --rm buyer --url http://seller:8000 --requests 1000 --tickets 100 --concurrency 200
```

This fires 1,000 concurrent purchases (20% of `request_id`s sent twice), reads `/status`, recomputes all five checks and prints `OVERALL: PASS` / `FAIL` / `INCONCLUSIVE`. To see the race it prevents, run the same load against the deliberately naive seller (`--no-deps` keeps `docker compose run` from recreating the seller with default settings):

```bash
SELLER_MODE=naive docker compose up -d seller
docker compose run --rm --no-deps buyer --url http://seller:8000 --requests 1000 --tickets 100 --concurrency 200   # checks FAIL
docker compose up -d seller            # back to the safe seller
```

## Reproducing the experiments

Use a new result name (first argument) so the saved evidence in `results/` is not overwritten. Each run writes `results/NAME.*` and verifies the seller's configuration before starting.

**50,000 arrivals over ~60 s on the three-seller cluster** (≈833 open-loop arrivals/s; not 50,000 simultaneous connections):

```bash
scripts/bench_cluster.sh my_50k 0 10 --client aiohttp --processes 4 --requests 50000 --rate 833.33 --concurrency 2000 --tickets 100 --seed 1 --timeout 30
python scripts/run_eval.py my_50k        # validity of the load generator + the five checks
```

**Database failures** — pause, or kill-and-restart, PostgreSQL during the run (`MODE:OFFSET_S:DURATION_S`, offset relative to the buyer's start; Docker applies the fault ~2 s after the command, which is why the mid-sale kill uses a negative offset):

```bash
FAULT=pause:1.0:10  scripts/bench_cluster.sh my_pause 0 10 --client aiohttp --processes 4 --requests 50000 --rate 833.33 --concurrency 2000 --tickets 100 --seed 1 --timeout 30
FAULT=kill:-1.15:10 scripts/bench_cluster.sh my_kill  0 10 --client aiohttp --processes 4 --requests 50000 --rate 833.33 --concurrency 2000 --tickets 100 --seed 1 --timeout 30
python scripts/fault_timeline.py my_kill   # fault timeline, outcomes per second, PostgreSQL recovery
python scripts/replay_check.py my_kill     # replays every confirmed and uncertain request_id, SQL audit
```

**Committed-but-unacknowledged purchases** (test-only proxy that drops PostgreSQL's COMMIT reply, `diag/compose.commitdrop.yml`). On Git Bash, `export MSYS_NO_PATHCONV=1` first.

```bash
C="docker compose -f docker-compose.yml -f diag/compose.commitdrop.yml"
docker compose --profile cluster stop seller1 seller2 seller3 lb
DROP_EVERY=5 MAX_DROPS=15 $C up -d --build --force-recreate commitproxy
SELLER_MODE=safe WORK_DELAY_MS=0 DB_POOL_SIZE=10 SOLDOUT_FASTPATH=1 $C up -d --force-recreate --no-deps seller
$C run --rm -T --no-deps buyer --client aiohttp --processes 1 --url http://seller:8000 --requests 400 --rate 100 --concurrency 50 --tickets 100 --seed 1 --timeout 30 --out /results/my_commitdrop.json.gz
$C logs --no-log-prefix commitproxy > results/my_commitdrop.proxy.log
$C stop seller commitproxy
SELLER_MODE=safe WORK_DELAY_MS=0 DB_POOL_SIZE=10 SOLDOUT_FASTPATH=1 docker compose --profile cluster up -d --force-recreate seller1 seller2 seller3 lb
python scripts/replay_check.py my_commitdrop
python diag/analyze_commit_drop.py my_commitdrop   # per dropped COMMIT: buyer answers, DB row, replay
```

## Evidence and the viewer

Open **`viewer/index.html`** in a browser (offline, no server). It walks through the problem, the fix and every experiment with the numbers computed from `results/`. The optional replay at the bottom **plays back saved per-second data embedded in the page; it does not start the backend or send any request**. To rebuild it from `results/`: `python scripts/build_viewer.py`.

AI coding-session logs: **`logs/`**.

## What was measured (summary)

- **Correctness:** all five checks passed in every run of the final design shown in the viewer (29 runs), including overloaded, invalid and database-failure runs. The naive seller failed them in all 6 runs.
- **50k workload:** 5 valid clean runs answered 836.9–838.5 arrivals/s with 0 errors. Two more 833/s runs were disturbed by nginx 502s and are not counted as clean.
- **Capacity sweep:** 1,000/s was sustainable in one valid run. At 1,200 and 1,400/s answers levelled off at about 1,060–1,070/s — an *observed saturation region on this machine, not a proven maximum* (both runs invalid by the pre-set rules). Seller CPU (one core per seller process) was the observed limit.
- **Database failures:** pause and kill/restart recovered without seller restarts; a transaction killed mid-INSERT was rolled back; with the COMMIT reply dropped, 10 purchases committed while the buyer saw an error, and replaying their `request_id`s returned the exact original tickets. No confirmed purchase was lost.

## Limitations

- Everything ran on **one host with one PostgreSQL** — a single failure domain. No replication or failover was implemented.
- Crash tests killed the PostgreSQL *process*; **power-loss / host-crash durability was not tested**.
- After a database failure, nginx kept sellers out of rotation 3.6–5.3 s longer than PostgreSQL needed to recover. Separately, **spontaneous nginx 502s** occurred in a few no-fault runs; their **root cause is unresolved**.
- Capacity figures are single runs per rate on one machine; the tests cover the specific faults described, not every failure mode.

Production extensions (not implemented) are listed at the end of [DECISIONS.md](DECISIONS.md).
