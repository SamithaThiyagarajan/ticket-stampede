# Decision log (running — becomes DECISIONS.md)

| # | Decision | Status | Rationale / evidence | Still uncertain |
|---|---|---|---|---|
| D0 | Project has its own git repo | done | Parent repo was rooted at home dir (exposed .ssh/.aws) | — |
| D1 | Shared state + correctness in Postgres, not an in-process lock | accepted | Process lock protects one process's memory only | Evidence due in multi-instance stretch (M8) |
| D2 | Naive race window is configurable (`NAIVE_DELAY_MS`, default 50); 0 ms run also reported | accepted | Delay stands in for real work between check and act; the 0 ms run answers "did you rig it?" | Whether 0 ms still reproduces the failure — to measure in M3 |
| D3 | asyncpg + raw SQL | accepted | Transaction boundaries and locks visible line by line | — |
| D4 | Naive seller keeps a separate `sold_count`, updated by Python-side read-modify-write | accepted | `COUNT(*)` cannot drift by definition; a stored counter with lost updates can, so the naive run can show drift. Corrected version must update it atomically with the insert | — |
| D5 | `/status.tickets` is a list of `{ticket_number, user_id}` | accepted | JSON object keys are unique; duplicate ticket numbers would be silently collapsed | — |
| D6 | Sold out → HTTP 409 `{"sold_out": true}` | accepted | — | — |
| D7 | `/reset` drops and recreates tables from the schema file | accepted | Schema changes (naive → constrained) apply on reset with no stale volume; reset is never under load | — |
| D8 | Single uvicorn worker | accepted | Multiple workers = multiple processes = the multi-instance problem, deliberately deferred | — |
| D9 | Buyer: semaphore-capped concurrency, httpx connection limits set to match | accepted | httpx defaults to 100 max connections; would silently cap concurrency and measure the client, not the server | Client-side limits at high concurrency (M7) |
| D10 | Duplicate copies sent adjacent (in flight together) | accepted | The retry-while-original-pending case is the hard one; a sequential replay passes even the naive seller | Sequential replay not separately reported |
| D11 | Idempotency checked twice: server rows (one user_id ↔ one request_id, so a user with >1 row = violation) and client responses (copies told different numbers) | accepted | Independent sources; one catches stored duplicates, the other catches contradictory answers | — |
| D13 | Safe `/buy`: one READ COMMITTED txn — `SELECT … FOR UPDATE` sale row → idempotency check → capacity → insert → `sold_count = sold_count + 1` → commit | accepted | Lock serializes check+act; after acquiring it Postgres re-reads the latest committed row; idempotency check sits *after* the lock so it sees all prior commits. Evidence: M4 runs below | Throughput cost of one hot row (M7) |
| D14 | Constraints: `sale.sold_count BETWEEN 0 AND total`, `tickets.ticket_number` PK, `request_id` UNIQUE, single sale row `CHECK (id=1)` | accepted | Backstops for invariants 1–3 — defense in depth, NOT the mechanism (the lock is). Invariant 4 has no constraint backstop; it relies on insert + counter in one txn. Backstop experiment: naive code + these constraints → 100 rows exactly, but 73–78% HTTP 500 | `request_id` UNIQUE never fired independently (PK caught it first) |
| D20 | `SELLER_SCHEMA` override (defaults to mode) | accepted | Only for the backstop experiment: racy code on constrained tables | — |
| D15 | No `sale_id` | accepted (pushback) | Single sale, `/reset` wipes all; multi-sale is a problem we don't have | Easy live change if asked |
| D16 | `SELLER_MODE=naive\|safe`, default `safe` | accepted | Before/after reproducible from one clean checkout; correct system is the default | — |
| D17 | Same request_id + different user_id → 422 | accepted | Stripe-style: reused key with different payload is a client error; returning the ticket would leak it to another user. Verified by curl | — |
| D18 | `NAIVE_DELAY_MS` renamed `WORK_DELAY_MS`; applied in both modes at the same point (in safe mode: while holding the lock) | accepted | Fair comparison — safe mode must not pass just because the window was removed | — |
| D19 | `/status` reads in one REPEATABLE READ read-only txn | accepted | Both reads from one snapshot → no false drift mid-sale | Not tested mid-sale (buyer reads after storm) |
| D21 | Load client supports closed loop (default) and open loop (`--rate`) | accepted | Closed loop = throughput at fixed concurrency, but hides queueing (coordinated omission: slow server slows the client). Open loop = arrival rate, matches "50k in 60s"; latency measured from *scheduled* time | Client's ability to hold 833/s on one Python process — unmeasured |
| D22 | Check 5: every 200 (request_id → N) must appear in `/status` as N owned by that request's user; a 409'd request's user must hold no row; rows whose buyer never got a 200 reported as info (ambiguous) | accepted | Neither responses nor `/status` alone can detect a confirmed sale that is lost/contradicted. Mapping relies on 1 user_id ↔ 1 request_id (harness property, not API) | No mode yet produces a real lost sale; proven only by tamper test |
| D23 | Verifier is a pure function over the saved raw file (`--recheck`), with tamper tests (`buyer/test_verify.py`) | accepted | Raw evidence is self-verifying; checker's true positives demonstrated, not assumed | — |
| D24 | Report `max_in_flight`, send lag, latency per status code | accepted | Measures (not assumes) achieved concurrency; separates client lag from server latency; 200 vs 409 latency is a bottleneck clue | — |
| D25 | Benchmarks run the buyer inside the compose network (`docker compose run --rm buyer --url http://seller:8000 …`, `tools` profile) | accepted | Host buyer: 8 transport errors / 6000 requests (5 of 6 runs INCONCLUSIVE); in-network: 0 / 6000 (6 of 6 PASS), interleaved runs, same seller/workload/seeds. P(0 \| expected 8) ≈ e^-8 | Confounds: Linux vs Windows event loop; buyer now shares the VM's CPUs with seller+DB, so throughput/latency across the two setups are not directly comparable |
| D12 | Verdict: any invariant fails → FAIL; all pass but non-200/409 or transport errors → INCONCLUSIVE; PASS only if clean | accepted (revised after M3) | Errors can hide a violation but cannot create one (violations measured from server rows). Original rule marked a 466-for-100 run "inconclusive", which was wrong | — |

## Observations
- **M1 smoke test (sequential):** naive seller is correct with no concurrency: replayed `r1` got the same ticket, sold out at 2, `sold` matched rows. The bug only exists under interleaving, so a concurrent client is the only way to expose it.

- **M2 checker false-positive test:** `--requests 150 --tickets 100 --concurrency 1` against naive seller → all PASS; 119×200 + 31×409 matches expected accounting exactly (126 unique ids, 24 replayed). Checker's ability to catch failures (true positives) is proven in M3.
- **Prediction recorded before M3** (the user's own): oversell + duplicate numbers (many readers see same count), drift (lost update), idempotency failure for concurrent same-request_id copies. Magnitude: unknown, to be measured.

- **M3 naive failure runs** (1000 req, 100 tickets, concurrency 200, 20% dup, pool 10; raw JSON + reports in `results/m3_naive_*`):

  | delay | run | rows issued | dup numbers | request_ids with >1 row (server) / told different numbers (client) | /status sold | max rows per number |
  |---|---|---|---|---|---|---|
  | 50ms | 1 | 466 | 80 | 37 / 5 | 100 | 10 |
  | 50ms | 2 | 564 | 99 | 9 / 1 | 100 | 10 |
  | 50ms | 3 | 570 | 100 | 9 / 3 | 100 | 10 |
  | 0ms | 1 | 160 | 41 | 3 / 1 | 100 | 5 |
  | 0ms | 2 | 160 | 47 | 1 / 1 | 100 | 4 |
  | 0ms | 3 | 164 | 37 | 2 / 0 | 100 | 6 |

  - All four invariants failed in all 6 runs. 50ms run 1 is formally INCONCLUSIVE (1 `ReadError`), but its failures are measured from server rows (see D12 revision question).
  - **Pool-size prediction confirmed:** max rows for a single ticket number is exactly 10 = `DB_POOL_SIZE` in every 50ms run, never more. With 0ms the window closes before all 10 align (max 4–6).
  - **The delay only amplifies the race:** 0ms still oversells about 60% (160–164 rows for 100 tickets). Answer to "did you rig it?"
  - **Drift is always an under-count, and the counter is what stops the oversell:** `sold` ended at exactly 100 in all runs; rows kept being inserted until the lost-update counter finally reached the cap. Oversell ≈ average number of writers per counter increment.
  - **The two idempotency checks disagree, and that is informative:** e.g. 37 server-side vs 5 client-side. Two concurrent copies both read `sold=k`, both insert `k+1`, and the client hears the *same* number twice — looks idempotent from the client, but two rows exist. The server-row check (D11a) catches what the response check cannot.

- **M4 naive regression after refactor:** 493 rows for 100 (in M3 range 466–570) → refactor preserved naive behavior. `results/m4_naive_regression_50ms.*`
- **M4 safe runs** (same workload + seeds as M3; `results/m4_safe_*`):

  | delay | run | rows | dup numbers | idempotency (server/client) | sold | verdict | wall time |
  |---|---|---|---|---|---|---|---|
  | 50ms | 1 | 100 | 0 | 0 / 0 | 100 | PASS | 24.3s |
  | 50ms | 2 | 100 | 0 | 0 / 0 | 100 | PASS | 27.2s |
  | 50ms | 3 | 100 | 0 | 0 / 0 | 100 | INCONCLUSIVE (1 ReadError) | 28.2s |
  | 0ms | 1 | 100 | 0 | 0 / 0 | 100 | PASS | 15.5s |
  | 0ms | 2 | 100 | 0 | 0 / 0 | 100 | PASS | 14.1s |
  | 0ms | 3 | 100 | 0 | 0 / 0 | 100 | PASS | 11.4s |

  - Invariants held in 6/6 runs. The 200 count above 100 (116–124) = replays of winning request_ids getting their original ticket back (response check confirms same number).
  - Second `ReadError` (now 2 across 13 concurrent runs). Revised D12 correctly refused to call it PASS.
  - **Unexplained slowness:** 0ms safe still takes 11–15s for 1000 requests (~70–90 req/s). Serialized 100 × 50ms only explains ~5s of the 50ms runs. Not investigated — M7. Hypotheses, unverified: sold-out requests still queue on the lock; Docker Desktop networking; client overhead.

- **M4 constraint-backstop experiment** — naive code (no txn, no lock) against the safe schema (`SELLER_MODE=naive SELLER_SCHEMA=safe`, 50ms, same workload/seeds; `results/m4_backstop_*`). Prediction recorded before running: invariants 1–3 hold, many 500s, INCONCLUSIVE.

  | run | rows | dup numbers | idempotency | sold | 200 / 409 / **500** | `tickets_pkey` / `request_id_key` / sale CHECK violations | verdict |
  |---|---|---|---|---|---|---|---|
  | 1 | 100 | 0 | 0 / 0 | 100 | 115 / 152 / **733** | 733 / 0 / 0 | INCONCLUSIVE |
  | 2 | 100 | 0 | 0 / 0 | 100 | 113 / 118 / **769** | 769 / 0 / 0 | INCONCLUSIVE |
  | 3 | 100 | 0 | 0 / 0 | 100 | 113 / 111 / **776** | 776 / 0 / 0 | INCONCLUSIVE |

  - **Prediction confirmed.** Constraints alone kept the *data* correct (same race that produced 466–570 rows now yields exactly 100), but 73–78% of buyers got a 500. The lock (M4 safe runs) keeps data correct **and** gives every buyer a clean 200/409. That is the evidence for "lock = primary mechanism, constraints = defense in depth".
  - Every 500 matched a `tickets_pkey` violation 1:1 in the seller log.
  - **Surprise: `request_id` UNIQUE never fired.** Concurrent copies read the same `sold`, so they collide on ticket_number too; Postgres aborts on the first unique index it checks (the PK). UNIQUE(request_id) would fire alone only if copies read *different* counts — possible, not observed. Its independent value is therefore argued, not demonstrated.
  - Invariant 4 held, as predicted — but by a property of this code path (a failed insert never reaches its counter update), not by any constraint.
  - **Implied rejected alternative — optimistic concurrency (constraints + catch-and-retry, no lock):** ~7.3–7.8 conflicts per successful sale at this contention. Each retry re-reads and re-contends, so under a stampede it trades lock waiting for wasted round-trips. Pessimistic row lock chosen for the high-contention case; not benchmarked head-to-head.

- **M6 load client v2 validation** (`results/m6_*`, `buyer/test_verify.py`):
  - *My prediction corrected before running:* I first claimed check 5 would catch the naive seller. It cannot: naive inserts before responding and never rolls back, so every response has a matching row. Confirmed: naive run → checks 1–4 FAIL (430 rows / 100), check 5 PASS.
  - Safe, closed loop, 50ms, seeds 1–3: checks 1–5 PASS, max in-flight = 200 (concurrency actually reached). 32.6–38.7 req/s. Latency p50 4.6–5.7s, p99 14.0–16.4s. **409 p99 (14–16s) > 200 p99 (6–7s)** → sold-out requests appear to queue on the lock too (signal for M7, not proof).
  - **Transport errors now in every closed-loop run** (2–4/run: ReadError, one RemoteProtocolError) → all 3 INCONCLUSIVE. None produced an ambiguous row (0 rows without a 200).
  - Tamper tests on a real saved run: control passes; deleted confirmed sale → only check 5 fails; ticket reassigned to a 409'd user → only check 5 fails (both sub-checks); response turned into ReadError → invariants pass, 1 ambiguous row, INCONCLUSIVE. `--recheck` reproduces the saved report byte-identical.
  - Open loop 400 @ 20/s: storm 20.02s at 20.0 req/s (schedule held), max in-flight 8, **0 transport errors, first clean PASS since v2**. Send lag p50 9.8ms, max 26.8ms, spread across the whole run (first attempt showed one ~100ms spike; did not reproduce, unexplained).

- **Network-path experiment** (`results/m6net_*`; safe seller, 50ms, pool 10, 1000 req, concurrency 200, seeds 1–6, host and in-network runs interleaved):

  | buyer | runs | errors | runs with error | verdicts | throughput |
  |---|---|---|---|---|---|
  | Windows host | 6 | 8 ReadError | 5/6 | 5 INCONCLUSIVE, 1 PASS | 40.8–45.4 req/s |
  | in compose network | 6 | 0 | 0/6 | 6 PASS | 60.3–68.2 req/s |

  - All 12 runs: all 5 invariant checks PASS — errors never affected correctness.
  - **Errors disappear** → attributable to the Windows-side client path (Docker Desktop proxy and/or Windows network stack), not the seller. Experiment does not separate proxy from stack.
  - In-network was ~50% faster despite the CPU-contention confound biasing the other way → the host path likely also costs throughput (suggestive, not proof).
  - Side note: raw file from the first open-loop run (~100ms send-lag spike) was overwritten by the rerun; its numbers survive only in this log and the transcript.

- **M7 prediction (recorded before any instrumentation or run):** H1 — the sale-row lock — is the primary bottleneck at 0ms. Expect ~8–9 of the 10 seller DB connections waiting on the same lock/transaction at any instant while one holds the sale row. Alternatives to rule out: H2 app pool size, H3 seller CPU (single Python worker), H4 commit/WAL fsync, H5 load client. 50k open-loop outcome predicted INCONCLUSIVE (capacity ~70–90 req/s ≪ 833/s → queueing + timeouts).

- **M7 instrument prep:** container limits checked before using 2000 connections: nofile 1,048,576; ephemeral ports 32768–60999; somaxconn 4096; uvicorn backlog 2048 (tightest, just above 2000). Verifier tests A–D pass (D = row for never-sent request → FAIL); `--recheck` verdicts of all saved v2 runs unchanged.
- **M7 experiment-control error (mine):** `docker compose run buyer` starts its `depends_on` services and silently **recreated the seller with the default `WORK_DELAY_MS=50`** because the new shell lacked `WORK_DELAY_MS=0`. Consequences: the sampler smoke test ran at **50ms, not 0ms** as I first claimed (files renamed `m7_smoke_50ms_*`); the first 50k attempt also ran against 50ms. The env check printed `safe 50 10` but did not abort. Fix: every run uses `--no-deps`, and the run script aborts on env mismatch. Also: Git Bash rewrites `/results/...` args to Windows paths → `MSYS_NO_PATHCONV=1` required.
  - Smoke (1000 req, closed loop, **50ms**) sampler output — tool validation only: 9 of 10 sessions waiting on Lock in 59% of busy samples; single root blocker 82%; the holder was mostly `idle in transaction` (65/85) — i.e. DB waiting on the app while the lock is held. Ungranted locks: `tuple` 5.8, `transactionid` 0.8.
- **M7 50k attempt 1 — INVALID as a benchmark, valid as a client-bottleneck finding** (`results/m7_STALL_*`): open loop 50k @ 833/s, 2000 connections. After 10 min: 1 `/buy` reached the seller (seller log), 1 ticket sold. Mean CPU over 150s: **buyer 104%, seller 0.4%, db 6.9%**. py-spy stack dumps (4/4) in `httpcore` `_assign_requests_to_connections` → `can_handle_request` → `__eq__`: httpcore rescans every queued request × every connection on each pool event; open loop puts tens of thousands of requests into that queue → quadratic work, event loop never reaches socket I/O. Closed-loop runs never hit it (semaphore capped in-flight at 200). **H5 (client) was the bottleneck** — so severe it tested nothing about the server.

- **M7 client fix (a) — bounded open-loop queue:** arrivals still scheduled at i/rate; at most `--concurrency` handed to httpx, the rest wait in our semaphore; latency from scheduled time. Waiting > `--timeout` for a slot → `QueueTimeout` (never sent), preserving httpx's previous PoolTimeout semantics. Lag split into `sched_lag` (client woke late) and `queue_wait` (backpressure). Per-second buyer CPU timeline via `time.process_time`. `scripts/bench.sh` sets seller env explicitly per run (`--force-recreate`), aborts on mismatch, `--no-deps` everywhere, and verifies the seller container ID is unchanged after the run.
- **M7 validation of fix (a) — FAILED 2 of 4 criteria** (`results/m7_validate_5k_0ms.*`, 5000 @ 833/s, cap 2000, 0ms): requests reached seller ✅ (3363 logged = 200s+409s exactly); verifier ✅; **buyer CPU p50 104%, ≥90% in 32/32 s ❌; sched lag p50 2.3s, max 4.3s ❌**. Server numbers from this run are client-limited → not evidence. py-spy (5s record): **67.7% of buyer CPU still in httpcore `_assign_requests_to_connections`**, now dominated by `is_idle` scans — cost scales with the *number of connections*, not only queued requests.
- **Diagnostic, cap 200** (`results/m7_diag_5k_0ms_cap200.*`): 150.9 req/s, sched lag p50 198ms, 0 errors, PASS — but buyer still 104% CPU in 31/31 s while seller 48%, db 23%. **Single-process Python/httpx client ceiling on this machine ≈ 150 req/s, about 1/5 of the required 833/s.** Docker VM has 16 CPUs; benchmark uses ~2.5.
- Bug fixed in `bench.sh`: awk `max` compared CPU strings lexically (db showed max < mean).

- **M7 client fix (c) — multi-process buyer** (`--processes K`): main process resets, sets a common wall-clock start (+3s), spawns K workers (`spawn`, not `fork`); worker k sends arrivals `i % K == k` at the unchanged global schedule `start + i/rate`, with its own httpx client and `ceil(cap/K)` connections; main merges records, reads `/status` once, runs the unchanged `verify()`. Report adds achieved arrival rate (scheduled + sched_lag) and per-worker CPU.
- **M7 validation, 5k @ 833/s, 8 workers, cap 2000, 0ms** (`results/m7_validate_5k_0ms_8proc.*`) — **PASSED all criteria:** arrivals delivered 835.4/s over 5.98s; sched lag p50 1.1ms, p99 66ms; 5000/5000 `/buy` in seller log; all 5 checks PASS, 0 errors. Per-worker CPU: 83–90% only in t=1–2s (opening 250 connections each), then 15–34% while arrivals continued at full rate, 13–15% while draining — client throughput is bounded by server responses (backpressure), not by CPU. The single-process ceiling (~150 req/s, 104% CPU) is gone.
  - Early server signals (validation run, not conclusions): 8–9 of 10 sessions waiting on Lock in 95% of busy samples, one root blocker 97.8%; seller CPU peaked 102% (one core); lock holder mostly `idle in transaction` (111) or `WALSync` (88). Throughput under backpressure 208.5 req/s.

- **M7 BASELINE — 50k arrivals / 60s / 0ms / pool 10 / 2000 conns / 8 buyer workers** (`results/m7_50k_0ms.*`):
  - **Client not the bottleneck (valid run):** arrivals delivered 836.5/s over 59.77s; sched lag p50 1.2ms, p99 19.8ms; per-worker CPU p50 17%, max 82–86% (connection ramp only), 0 seconds ≥90% in any worker. Seller container unchanged during run.
  - **Correctness: all 5 checks PASS** — 100 sold, 0 duplicates, 0 idempotency violations, no drift, every response consistent with `/status`, 0 late-confirmed sales.
  - **Verdict INCONCLUSIVE** (as predicted by the user): 27,825 `QueueTimeout` (never sent — waited 30s for one of 2000 slots). 0 sent-no-answer. `/buy` in seller log = 22,175 = 132×200 + 22,043×409 exactly.
  - **Server capacity: 226 req/s**, flat for the whole run (199, 231, 234, 231, 214 per 20s window). The sale itself was over at t≈3.1s (100th sale); **~97s and 99.4% of server work was answering "sold out" — each through the same locked row.**
  - Latency: 200 p50 2.7s; 409 p50 37.5s (≈30s client queue + ~8s in server: Little's law 2000 in flight / 226 per s ≈ 8.9s).
  - **pg sampler, 939 busy samples: waiting on Lock mean 8.13; 8 waiters in 76.9% of samples, 9 in 19.9% (8–9 in 96.8%); exactly one root blocker in 98.4%.** Ungranted: `tuple` 7.18, `transactionid` 0.90. **→ H1 prediction ("8–9 of 10 waiting on one holder") matches the observation.** Correlational; causal test = pool sweep.
  - **What the lock holder is doing** (944 holder observations): `idle in transaction` 44% (DB waiting for the seller to send the next statement), `WALSync` 41% (commit fsync), `waiting_lock` 8% (sampling-transition artifact), CPU 4%. → lock hold time (~1/226 s ≈ 4.4ms) ≈ app round-trips + one WAL fsync. H3 (app) and H4 (fsync) are the components of H1's hold time, not competing explanations.
  - CPU (docker stats mean/max): seller 72%/89%, db 42%/58%, buyer container 137%/306% across 8 processes.
  - Report fix: the `throughput` line counts QueueTimeouts as completions (508 req/s — misleading); added `served by server` line (226.0/s). Verdict unchanged on recheck.

- **M7 secondary — 50k / 60s / 50ms** (`results/m7_50k_50ms.*`), prediction recorded before running (delay only on the 100 success paths → ~5s extra lock time early, post-sellout rate unchanged, ~−1,100 served, sale 5–8s):

  | | 0ms | 50ms |
  |---|---|---|
  | 100th sale by | 3.12s | 6.88s |
  | served by server | 22,175 | 21,364 (−811) |
  | served/s per 20s window | 199 231 234 231 214 | 155 228 235 233 217 |
  | 8–9 lock waiters / one root blocker | 96.8% / 98.4% | 96.5% / 98.8% |
  | correctness | 5/5 PASS | 5/5 PASS |
  | client | arrivals 836.5/s, 0 s ≥90% | arrivals 835.9/s, max 77%, 0 s ≥90% |

  → Prediction held. At 50k the 50ms work only matters until sell-out; post-sellout capacity is set by the sold-out path through the lock.
- **Experiment-control error #2 (mine), caught before any result was used:** first sweep command hard-coded `bench.sh … 0 10` instead of `0 $P`, so the "pool 5" run was actually pool 10, plus a `sed` that relabelled the env file instead of failing. Stopped mid-run; all its files deleted. `bench.sh` verifies the seller against its *arguments*, so it cannot catch a wrong argument → sweep loop now also asserts the recorded pool equals the loop variable.
- **Pool sweep, closed loop @ 2000 concurrency — CLIENT-CONTAMINATED, not valid as causal evidence** (`results/m7_sweep_pool{5,10}.*`):

  | pool | throughput | ReadErrors | worker CPU ≥90% (per worker, s) | mean lock waiters | one root blocker |
  |---|---|---|---|---|---|
  | 5 | 178.3/s | 46 | w0:45 w1:24 w2:1 w3:2 w4:49 w5:30 w6:33 w7:24 | 2.97 (mode 3) | 96.1% |
  | 10 | 186.2/s | 27 | w0:24 w1:1 w2:1 w3:1 w4:32 w5:45 w6:48 w7:16 | 7.78 (mode 8) | 95.9% |

  - Pool 10 closed loop gives 186/s vs 226/s in the validated open-loop baseline at the same pool, and some workers are saturated for long mid-run stretches with unexplained ReadErrors (seller logged nothing) → fails the "client not the bottleneck" criterion. Cause unknown (hypothesis: different connection churn in closed loop), not investigated.
  - DB-side observations still consistent with H1 (waiters ≈ pool − 2; single root blocker ~96%; same holder breakdown), but throughput comparison is contaminated. Pools 20/40 not run.

- **Pool sweep v2 — criteria recorded BEFORE any run** (decision by the user): same workload as the valid baseline (50k open loop @ 833.33/s, 8 workers, cap 2000, 0ms, in-network, seed 1), `DB_POOL_SIZE` ∈ {5, 10, 20, 40}; pool 10 is re-run as the comparison point.
  - **Metric:** `served by server` req/s (first send → last answer).
  - **Material improvement threshold:** > 15% above the pool-10 re-run.
  - **Client-clean criteria (all required, else run INVALID for the causal conclusion):** arrivals delivered within ±2% of 833.33/s; sched lag p99 < 100ms; no worker ≥90% CPU for more than 3 s (connection-ramp allowance); 0 "sent, no answer" transport failures; seller container unchanged; recorded pool size equals the intended pool (checked outside `bench.sh`).
  - **H1 predicts:** all valid pools within 15% of pool 10; mean lock waiters grow ≈ pool − 2.

- **Pool sweep v2 RESULTS** (`results/m7_sweepv2_*`, evaluated by `scripts/sweep_eval.py` against the criteria above; summary `results/m7_sweepv2_summary.txt`):

  | pool | served/s | vs pool 10 | lock waiters (mean) | one root blocker | client-clean |
  |---|---|---|---|---|---|
  | 5 | 201.1 | +7.8% | 3.08 | 99.1% | VALID |
  | 10 | 186.6 | — | 8.07 | 98.3% | VALID |
  | 20 | 189.9 | (+1.8%) | 17.94 | 98.8% | INVALID: sched p99 915ms, a worker ≥90% for 38s, 14 ReadError |
  | 40 | 194.4 | (+4.2%) | 37.70 | 98.5% | INVALID: 2 ReadTimeout (sent-no-answer) |

  - All 4 runs: 5/5 invariant checks PASS.
  - **Pre-registered conclusion: no valid pool exceeded the threshold (214.6/s).** But only pools 5 and 10 are valid, so the upward direction (20, 40) is **not established by valid runs**. Invalid runs' numbers are shown in parentheses and not used; for the record, neither would have crossed the threshold.
  - **Strongest valid causal evidence is downward:** halving the pool (10 → 5) did not reduce throughput (201 vs 187). If the pool were the limiter, pool 5 should be roughly half.
  - **Lock waiters track pool size almost exactly (≈ pool − 2)** — 3.08, 8.07, 17.94, 37.70 — with a single root blocker in 98–99% of samples at every size. Extra connections become extra waiters on the same row.
  - **Run-to-run variance is larger than the threshold:** identical config (pool 10) gave 226.0/s in the baseline and 186.6/s in the rerun (−17%). So the 15% threshold cannot resolve small effects; the sweep supports "no large/proportional gain from more connections", not "exactly flat". Cause of the variance unknown (host/Docker load?). Pool 20's client degradation is also unexplained — the same client config was clean in 5 other 50k runs.

## Open questions / to revisit
- **Run-to-run variance (−17% on identical config)** and occasional client degradation (pool 20) — unexplained. Needs repeated runs per config before any fine-grained claim.
- **What should the WAL question's answer be backed by?** Sold-out path (`FOR UPDATE` + `SELECT` + `COMMIT`) shows the holder in `WALSync` 41% of the time. To demonstrate (not assert) before explaining.
- ~~Transport errors~~ → resolved by D25 (Windows-side client path).
- **Client send-lag jitter (0–27ms):** hypothesis = Windows ~15.6ms timer resolution. At 833/s arrivals would leave in bursts of ~13 rather than evenly. Average rate holds; burstiness must be reported with the 50k run.
- DB pool size (`DB_POOL_SIZE=10`) caps how many requests are inside the race window at once. The concurrency the DB sees is the pool size, not the client count. Matters for M3 and M7.
- `/status` does two separate reads (sale row, then tickets): not a consistent snapshot. Fine for naive; decide for the corrected seller.
- Same `request_id` sent with a *different* `user_id`: what should happen? Not yet decided.

## Interview flags
- Naive lost-update feedback loop: `sold_count` drifts low → the capacity check passes for more buyers → oversell gets worse.
- "Didn't you just add a sleep to make it break?" → answer with the 0 ms run data.
