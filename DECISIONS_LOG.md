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

- **M7 optimization (A) — lock-free sold-out/replay fast path** (`SOLDOUT_FASTPATH=1`, default; `0` = baseline path). Design: ONE statement `sale LEFT JOIN tickets ON request_id` = one snapshot. sold_count and ticket rows commit in the same txn, so a snapshot with sold_count ≥ total contains every ticket row → "sold out + no row" is final → 409 without the lock. Row found → same response as locked path (shared helper, incl. 422). Otherwise → unchanged locked txn. Race it prevents: two separate reads (tickets, then sale) let a concurrent copy's ticket commit between them → same request_id told both "ticket N" and "sold out". Assumes sold_count never decreases within a sale — **the waitlist stretch would invalidate it**. No FOR UPDATE on this path → no xid, no WAL record, no commit flush. Functional curl check of all branches OK. `bench.sh` now sets + verifies `SOLDOUT_FASTPATH` (4th env value).
- **M7 optimization run — 50k/60s/0ms/pool 10/fast path — INVALID (client not clean)** (`results/m7opt_50k_0ms_fastpath.*`): sched lag p99 3,279ms; every worker ≥90% CPU for 40–50s (p50 104%); 190 ReadError (sent, no answer); buyer container 710% CPU vs 137% in baseline. Throughput numbers not usable.
  - **Valid (DB-side):** lock contention gone — busy samples 83 vs 939 (baseline), zero lock waiters in 81.9% of busy samples, WALSync on holder ~0. Correctness 5/5 PASS, incl. **0 "told sold-out but holds a ticket"** (the race the single snapshot prevents).
  - **Contradiction with earlier framing:** answered rate in 5s windows ≈ 220–240/s in both runs, and 409s still spent 6–12s between send and answer although Postgres was idle. Time is spent outside Postgres — seller event loop / asyncpg pool acquire / network / the pegged client — not distinguishable from this run.
  - **Correction (mine):** the pool sweep cannot distinguish H1 (lock is the ceiling) from H3 (seller process is the ceiling and inflates lock-hold time via `idle in transaction`). Both predict "more pool doesn't help". My summaries "row lock confirmed as bottleneck" overstated the correlational evidence. Current status: **lock contention = demonstrated and removable; lock as the throughput ceiling = NOT shown; seller process = leading suspect, untested.**
  - Also unexplained: why the same client config pegs when the server answers differently.

- **300/s fast-path A/B — PRE-REGISTERED before either run** (decision by the user): 18,000 arrivals @ 300/s (60s), 8 workers, cap 2000, 0ms, pool 10, in-network, seed 1; identical code, only `SOLDOUT_FASTPATH` 0 vs 1 (set + verified by `bench.sh`, re-checked externally).
  - **Client-clean (same criteria as the sweep):** arrivals within ±2% of 300/s; sched lag p99 < 100ms; no worker ≥90% CPU > 3s; 0 sent-no-answer; seller container unchanged. A run failing these is INVALID.
  - **"Keeps up":** 0 QueueTimeouts AND served ≥ 97% of arrivals over the arrival window + drain, i.e. served/s ≥ 291 while arrivals last. **"Falls behind":** any QueueTimeouts OR served/s < 291.
  - **Interpretation:** fastpath=0 falls behind AND fastpath=1 keeps up → supports the lock being a significant ceiling. Both fall behind similarly → the lock is not the primary ceiling → profile the seller before any further optimization. Any other pattern → report as-is, no forced interpretation.

- **300/s fast-path A/B RESULTS** (`results/m7ab_300rps_*`, `scripts/run_eval.py`, summary `results/m7ab_300rps_summary.txt`):

  | | fastpath=0 | fastpath=1 |
  |---|---|---|
  | arrivals delivered | 300.3/s | 300.4/s |
  | sched lag p99 / max worker s≥90% | 7.1ms / 0 | 3.4ms / 0 |
  | sent-no-answer | 0 | **1** (ReadError at t=23.09s, 15ms, no queue wait) |
  | **client (pre-registered)** | **CLEAN** | **NOT CLEAN** (1 sent-no-answer) |
  | served | 18000/18000 = **196.1/s** | 17999/18000 = **300.3/s** |
  | QueueTimeouts | 0 | 0 |
  | keeps up (pre-registered) | **FALLS BEHIND** | KEEPS UP |
  | 409 latency p50 / p99 | 17,753 / 31,464 ms | **11.1 / 1,171 ms** |
  | 200 latency p50 / p99 | 1,393 / 3,319 ms | 1,038 / 1,880 ms |
  | seller CPU mean / max | 80% / 97% | 71% / 100% |
  | db CPU mean / max | 44% / 64% | 22% / 42% |
  | pg busy samples / mean lock waiters | 845 of 959 / 8.10 | 59 of 612 / 1.54 |
  | correctness | 5/5 PASS | 5/5 PASS |

  - **Strict pre-registered outcome: not decided** — fastpath=0 is a valid "falls behind"; fastpath=1 "keeps up" but is invalid by the client rule (one isolated ReadError). Criteria not relaxed after the fact.
  - Effect size if the fastpath=1 run is taken at face value: served 196 → 300/s (= arrival rate), 409 p50 17.8s → 11ms. One isolated error cannot plausibly produce that, but that judgment is not the pre-registered test.
  - Reconciliation with the invalid 833/s run (if replicated): the lock is a significant ceiling around ~200–230/s; removing it moves the ceiling above 300/s; at 833/s a further ceiling appears (seller CPU reached 100% max even at 300/s → leading suspect), masked by client contamination.
  - fastpath=0 at 300/s served 196/s vs 226/s in the 833/s baseline — consistent with the known run-to-run variance.

- **300/s A/B replication — PRE-REGISTERED before running:** 3 × FASTPATH=0 and 3 × FASTPATH=1, alternating (0,1,0,1,0,1), identical setup to the A/B above (18k @ 300/s, 8 workers, cap 2000, 0ms, pool 10, seed 1), no code changes. Client-clean and keeps-up criteria unchanged. **Confirmed** iff each side has ≥ 2 valid runs AND every valid FASTPATH=0 run falls behind AND every valid FASTPATH=1 run keeps up. Invalid runs are reported, not dropped. The original A/B pair is not counted in the replication.

- **300/s A/B replication RESULTS** (`results/m7rep_300rps_*`, summary `results/m7rep_300rps_summary.txt`): **all 6 runs client-clean** (0 excluded).

  | | FASTPATH=0 rep1 / rep2 / rep3 | FASTPATH=1 rep1 / rep2 / rep3 |
  |---|---|---|
  | served/s | 210.2 / 215.5 / 213.2 — all FALL BEHIND | 300.3 / 300.3 / 300.3 — all KEEP UP |
  | QueueTimeouts | 0 / 0 / 0 | 0 / 0 / 0 |
  | 409 latency p50 | 13,984 / 13,080 / 16,254 ms | 13.1 / 11.0 / 8.6 ms |
  | 409 latency p99 | 25,490 / 23,331 / 24,243 ms | 1,076 / 933 / 800 ms |
  | mean lock waiters (busy) | 8.08 / 8.09 / 8.08 | 1.38 / 1.01 / 1.23 |
  | pg busy samples | 812/940, 775/873, 761/907 | 65/651, 84/674, 69/657 |
  | seller CPU max | 98 / 96 / 98% | 103 / 104 / 95% |
  | db CPU mean | 41 / 42 / 40% | 27 / 27 / 25% |
  | correctness | 5/5 each | 5/5 each |

  - **Pre-registered rule satisfied → CONFIRMED: the sale-row lock is a significant throughput ceiling (~210–215/s on this machine); the lock-free sold-out path removes it** (keeps up with 300/s, 409 p50 ~10ms).
  - This revises the earlier correction: the pool sweep alone could not show it, but the A/B does. The 833/s fast-path run's lack of gain is attributed to a *further* ceiling above 300/s plus client contamination — to be tested, not assumed.
  - Seller CPU reaches ~100% (one core) with the fast path at only 300/s → seller process is the leading suspect for the next ceiling.

- **Seller profile at 833/s, fast path on** (`results/m7prof_833rps_fastpath1.*`; 25k arrivals/30s — not a benchmark). py-spy 100Hz, 20s, `--idle`, from a separate container sharing the seller's PID namespace (seller image/code untouched). Summarized by `scripts/profile_summary.py`: main thread only (executor idle threads excluded, 3,998 samples).
  - **Event loop busy 86.9%** of sampled time (idle in `select` 13.1%); seller CPU max 106%; Postgres nearly idle (busy samples 90/792, mean lock waiters 0.93, db CPU mean 26%).
  - **Busy time is HTTP/framework/event-loop overhead, not the database:** inclusive — FastAPI/Starlette 59.6%, asyncpg 16.9%, app handler 13.0%, access logging 9.6%. Self — asyncio loop/sockets 34.4% (socket `write` alone 11.9%), uvicorn/**h11 pure-Python HTTP parser** 24.6%, framework 14.2%, logging 6.9%, asyncpg 6.4%.
  - Answer rate under profiling ≈ 290–377/s per 5s window (profiler overhead + client not clean: sched p99 117ms, 5 hot s, 46 ReadTimeout → run INVALID as a benchmark; correctness 5/5 PASS).
  - **Conclusion: with the lock removed from the sold-out path, the next ceiling is the single seller process's per-request CPU (HTTP parsing + ASGI framework + event loop), with the database having large headroom.**
  - Caveats: py-spy pauses the process per sample (proportions, not absolute speed); one run.
  - Found: buyer image was stale (built before the `served by server` report line); JSON format unaffected, `run_eval.py` computes from JSON. Rebuild before the next run.

- **M8 client-capacity validation — PRE-REGISTERED before running:** current seller unchanged (1 uvicorn worker, fastpath=1, pool 10, 0ms); 25,000 arrivals @ 833.33/s (30s), 8 buyer workers, cap 2000, seed 1. Same client-clean criteria (arrivals ±2%, sched p99 <100ms, no worker ≥90% >3s, 0 sent-no-answer, seller unchanged). Scope: shows whether the buyer delivers arrivals cleanly while the seller is saturated (backpressure); it cannot show the buyer handles 833 *answers*/s — that is re-checked on every multi-worker run. If the buyer degrades there, add buyer workers stepwise (measured), keeping buyer + seller + db within 16 CPUs.

- **M8 client-capacity RESULT** (`results/m8_client_833rps_1seller_8buyer.*`): arrivals 837.2/s; sched lag p99 50.2ms; max worker hot seconds 3 (limit ≤3); buyer container CPU mean 298% / max 712% across 8 processes (~37% each); served 23,571 (361.7/s), 1,407 QueueTimeouts; 5/5 correctness PASS. **Strict verdict: NOT CLEAN** (22 sent-no-answer). Diagnosis: all 22 are `ReadTimeout` at exactly 30.0s after send; slowest answered request 29.2s; seller logged exactly 23,571 `/buy` = client-received (the 22 were never answered); 0 late-confirmed sales. → **The buyer is not the bottleneck; the errors are server-side saturation.** No extra buyer workers needed yet.
  - **Criterion flaw exposed (not changed by me):** "0 sent-no-answer" also fires when a saturated server fails to answer within the timeout, so every saturated single-worker run at 833/s is INVALID by construction. Proposed refinement for the user to decide: early transport failures (ReadError / RemoteProtocolError / timeouts well before the limit) → client/network → INVALID; `ReadTimeout` at the full timeout with client CPU/lag clean → server outcome, reported as unanswered (like QueueTimeout), not invalidating. Until decided, runs are reported under the strict rule plus this breakdown.
- **M8 multi-worker seller:** `Dockerfile` no longer hard-codes `--workers 1`; uvicorn reads `WEB_CONCURRENCY` (compose: `${SELLER_WORKERS:-1}` → default still 1 worker). `app.py` untouched. Each worker has its own pool → DB connections = 10 × W (confound, recorded). `bench.sh` sets + verifies `WEB_CONCURRENCY` and verifies the **actual** worker count via live seller DB connections = W × POOL. Functional check at W=4: parent + 4 server processes, 40 connections, all branches correct.
- **M8 before/after — PRE-REGISTERED before running:** workload identical to the client-capacity test (25,000 @ 833.33/s, 8 buyer workers, cap 2000, 0ms, pool 10 per worker, fastpath=1, seed 1). W = 1 (fresh, post-change regression/baseline), 2, 4. Report per run: strict validity + error breakdown; served/s, 200s, 409s, QueueTimeouts, p50/p99, seller/db CPU, lock waiters, buyer CPU/lag, 5 checks.
  - **Prediction (hypothesis under test):** served/s rises with W until another bottleneck (Postgres CPU, lock contention, host CPU, or the buyer) takes over; all 5 invariants pass at every W. Expected: W=2 ≈ 1.6–2× W=1; W=4 may reach the 833/s arrival rate (then only "keeps up" can be claimed — the arrival rate caps measurable throughput). A buyer that degrades as answers speed up → that run INVALID, add buyer workers stepwise.

- **M8 before/after RESULTS (8 buyer workers) — ALL THREE INVALID** (`results/m8_833rps_{1,2,4}worker.*`, summary `results/m8_833rps_summary.txt`):

  | seller workers (verified conns) | 1 (10) | 2 (20) | 4 (40) |
  |---|---|---|---|
  | client (strict) | NOT CLEAN: 5 hot s; 37 sent-no-answer (all ReadTimeout @30s) | NOT CLEAN: arrivals 772.5/s, sched p99 3,860ms, 48 hot s, 51 early ReadError | NOT CLEAN: sched p99 4,120ms, 41 hot s, 21 early ReadError |
  | buyer CPU mean/max (8 procs) | 243/819% | 800/873% | 668/867% |
  | served/s | 341.8 | 285.3 | 492.0 |
  | QueueTimeouts | 2,660 | 5,844 | 0 |
  | 409 latency p50/p99 | 21.7s / 40.4s | 22.8s / 45.7s | 13.6s / 25.0s |
  | seller CPU mean/max | 78/105% | 95/210% | 164/355% |
  | db CPU mean/max | 27/54% | 28/73% | 44/143% |
  | lock waiters (busy) | 1.03 | 3.84 | 8.11 |
  | correctness | 5/5 | 5/5 | 5/5 |

  - As the seller speeds up, the 8-worker buyer saturates (all 8 pegged at W=2) and fails with *early* ReadErrors → client-limited. W=2's lower served/s reflects the client delivering only 772/s, not a slower seller.
  - W=4 answered every request it received (0 QueueTimeouts) at 492/s while the client was degraded → signal the seller got faster; its capacity (≥ 492/s) is unmeasured. **Not evidence.**
  - Invariants 5/5 at W = 1, 2, 4 with no application lock: correctness across processes holds in all three runs (valid regardless of client state, since checks are on server rows).
  - Host CPU budget becomes binding: W=4 peaks ≈ buyer 867% + seller 355% + db 143% ≈ 13.6 of 16 cores.
- **Step authorized by the user: 12 buyer workers** (cap 2000 total → ~167 connections each). Added check: host saturation = sum of all containers' CPU per docker-stats sample (flag if ≥ 1400% of 1600%). Order: W=4 first; run the W=1/2 before-pair with 12 buyers only if W=4 is client-clean.

- **M8 12-buyer step — INVALID, host saturated** (`results/m8_833rps_4worker_12buyer.*`): W=4, 12 buyer workers. Arrivals 812.3/s (outside ±2%), sched p99 2,862ms, 43 hot s, 55 early ReadError; buyer CPU mean 1,200% / max 1,473%; **host (sum of containers) ≥1400% in 15 of 20 samples**, mean 1,352%; seller 216/409%, db 54/101%; served 364.5/s, 1,707 QueueTimeouts; 5/5 correctness. More buyer processes consumed more CPU without making the client clean and took CPU from the system under test. Stopped here per the user's constraint (don't starve seller/Postgres).
  - **Conclusion: on this single 16-CPU host, the Python/httpx buyer cannot generate 833/s cleanly against a multi-worker seller** — the load generator, not the seller, is the binding constraint for this experiment. The before/after throughput comparison at 833/s is **undecided**. Why the buyer's CPU per request rises sharply when the server answers faster is not diagnosed.
  - What IS established: all 5 invariants held across 1, 2 and 4 seller processes with no application lock (4 runs); W=4 answered every request it received at ≥ 492/s under a degraded client (signal only).

- **M9 distributed stretch — 3 seller instances behind nginx, ONE Postgres.** User stopped the 500/s 1-vs-4 experiment (enough M7/M8 evidence of a single-process HTTP/ASGI CPU ceiling). Architecture: compose `cluster` profile — `seller1..3` (same image, 1 uvicorn process each, pool 10 each, identical `DATABASE_URL` → the one `db`), `lb` = nginx:1.27-alpine round-robin on host :8080. `app.py`, schema, fast path, FOR UPDATE path, idempotency, constraints unchanged; no Redis/queue/app lock; sellers hold no ticket state. Single `seller` service kept (stopped during cluster runs) so M1–M8 stay reproducible; `bench.sh` untouched, new `scripts/bench_cluster.sh`.
  - nginx: upstream `keepalive 256` (else a new upstream TCP connection per request → ~50k TIME_WAIT/min vs ~28k ephemeral ports); `add_header X-Upstream $upstream_addr` (evidence only; buyer records it per request); `access_log off` on nginx only; nginx does not retry POST on another upstream by default.
  - **Found and fixed during smoke test:** with `worker_processes auto` (16 nginx workers) every low-volume request went to seller1 — round-robin state is per nginx worker, and fresh workers start at the first server. `worker_processes 1` → strict rotation (verified: 1 worker process; replays rotated .5 → .3 → .4).
  - **Smoke (curl via nginx):** ticket 1 bought via seller2; replays via seller3 and seller1 returned ticket 1; 422 for reuse by another user from a different instance; sold-out 409 from all three; `/status` byte-identical (same md5) through each instance.
  - **Confound:** 3 × pool 10 = 30 DB connections vs 10 in single-seller baselines.
- **M9 PRE-REGISTERED before running:**
  - F1 functional: 3,000 arrivals @ 300/s, 100 tickets, 20% of request_ids × 2 copies. F2 hot-duplicate: 2,000 arrivals @ 300/s, 50 tickets, **every** request_id × 10 adjacent copies (in flight together, spread over instances by round-robin). Pass = 5/5 checks, traffic on all 3 instances, and every request_id whose copies hit >1 instance and hold a ticket got the same ticket on every copy.
  - Main: 50,000 @ 833.33/s, 8 buyer workers, cap 2000, pool 10 per instance, fastpath 1, 0ms, seed 1. Same client-clean criteria as before (strict). Keeps-up = 0 QueueTimeouts and served ≥ 808/s.
  - **Predictions:** all 5 invariants hold; traffic ≈ 1/3 per instance. From M8 (8 buyers pegged whenever the seller answered faster), **the 8-worker buyer is likely to become the bottleneck → run likely INVALID** as a seller-capacity benchmark; if so it is reported as a buyer limitation, not a seller failure.

- **M9 functional tests — VALID, PASS** (`results/m9_F{1,2}_functional.*`): each run's pre-check proved one Postgres: 30 connections = 3 client IPs × 10, IPs matched to seller1..3 containers; all containers unchanged.
  - F1 (3,000 @ 300/s, 20% × 2 copies): client CLEAN; keeps up (300.1/s); per instance 1000/1000/1000 (seller logs = X-Upstream); 483 request_ids had copies on >1 instance, 18 held tickets → all 18 got the same ticket on every copy; 5/5 PASS.
  - F2 (2,000 @ 300/s, 50 tickets, every request_id × 10 adjacent copies): client CLEAN; keeps up; 666/667/667; **all 200 request_ids hit >1 instance; all 50 ticket holders got the same ticket on all 10 copies across instances**; 5/5 PASS.
  - Found: `run_eval.py` looked for bench.sh's phrase "seller container unchanged"; bench_cluster writes "all containers unchanged" → fixed to accept either (regression: earlier verdicts unchanged).
- **M9 main — 50k @ 833.33/s, 3 sellers, 8 buyer workers — INVALID as a benchmark (buyer-limited); correctness VALID** (`results/m9_50k_833rps_3sellers.*`):
  - Client: arrivals 832.7/s (OK), **sched lag p99 4,019ms, a worker ≥90% for 77s**, buyer container CPU mean 729% / max 848% (8 workers ~pegged), 0 sent-no-answer. Host NOT saturated (sum of containers mean 918%, max 1,224%, 0 samples ≥1,400%).
  - Arrivals 50,000; sent 49,946 (54 QueueTimeout); responses 49,946 = 133 × 200, 49,811 × 409, **2 × 502**; served 554.7/s averaged over 90s. Latency (all served) p50 17.3s / p99 34.5s; 200s p50 1.6s / p99 8.5s.
  - Per-5s windows: cluster answered ~720–755/s in steady windows (t=20–40, 60–80) while the client was degraded → **signal only**, cluster capacity unmeasured.
  - CPU mean/max: seller1 57/102%, seller2 58/101%, seller3 50/93%, nginx 26/53%, db 46/80%. pg: busy 181/1029 samples, lock waiters 0 in 90.1% of busy samples (mean 2.25 — the sale phase).
  - Distribution: seller logs 17,362 / 17,363 / 15,219 = X-Upstream exactly (+2 502s on seller3).
  - **5/5 correctness PASS**; 5,657 request_ids had copies on >1 instance, 27 held tickets → all copies got the same ticket.
  - **502s + skew explained:** at t≈49.9s nginx logged `recv() failed (104: Connection reset by peer)` from seller3 for 2 requests → 502 → nginx passive health check (`max_fails=1 fail_timeout=10s` default) removed seller3 for 10s (answers: t=50 window 5, t=55 window 0, back at t=60). Neither 502'd request_id holds a row; their other copies got 409 → no invariant impact. **Cause of the reset unverified**; hypothesis: uvicorn closes idle keep-alive connections after 5s while nginx keeps upstream connections up to 60s (reuse-vs-close race); a throughput dip at t=45 precedes it. Nothing changed.
  - Buyer not increased: at 8 workers it uses ~7.3 cores; host peak leaves ~3.8 free; M8 showed 12 workers saturating the host → insufficient headroom.

- **M10 aiohttp load generator** (user decision; no seller/schema/nginx/correctness changes): `buyer.py --client httpx|aiohttp` (default httpx → all earlier runs reproducible). Only the HTTP call differs (`http_client()` yields `post_buy`); schedule, in-flight semaphore, QueueTimeout, sched_lag/queue_wait, CPU monitor, multi-process, X-Upstream, `/reset`+`/status`, `verify()`, raw output are shared. aiohttp: `TCPConnector(limit=cap)` = semaphore cap; `ClientTimeout(sock_connect=sock_read=--timeout, total=None)`. Error mapping: ConnectionTimeoutError→ConnectTimeout, ClientConnectorError→ConnectError (never sent); SocketTimeoutError/asyncio.TimeoutError→ReadTimeout; all others keep their class name (sent-no-answer). Only library transport errors are caught. JSON parsed only on 200 (nginx 502 bodies are HTML) — caught in review before any run. `args.client` saved in raw files and printed. Checks: verifier tests OK; old httpx run rechecks identically; 20-request smoke via nginx identical for both clients (6×200, 14×409, PASS).
- **M10 PRE-REGISTERED before running:**
  - 5k validation: 5,000 @ 833.33/s, 3-seller cluster via nginx, 100 tickets, 20% × 2 dup, cap 2000, pool 10/instance, fastpath 1, seed 1. Buyer workers ladder **2 → 4 → 8**, stop at the first client-clean run (same strict criteria: arrivals ±2%, sched p99 <100ms, no worker ≥90% >3s, 0 sent-no-answer, containers unchanged) that also has 0 QueueTimeouts. That count is the "minimum necessary".
  - 50k only if a 5k is clean: 50,000 @ 833.33/s with that worker count, same settings. VALID iff client-clean (strict). **Sustains 833/s** iff VALID AND 0 QueueTimeouts AND served ≥ 808/s (97%).
  - 502s: recorded as they are; if present, collect nginx error log lines, upstream, timing, and the affected seller's log. No timeout/keep-alive changes.

- **M10 5k validation** (`results/m10_5k_aiohttp_2buyer.*`): **2 aiohttp workers → CLIENT CLEAN** at the first rung (arrivals 839.5/s, sched p99 37.2ms, 0 hot s, 0 errors); keeps up (5000/5000, 839.0/s, 0 QueueTimeouts, p50 48ms / p99 2,155ms); buyer container CPU mean 133% / max 156% (vs ~730% for 8 httpx workers in M9); 5/5 PASS.
- **M10 50k, 2 aiohttp workers — INVALID (strict: sched lag p99 133.3ms ≥ 100ms)** (`results/m10_50k_833rps_3sellers_aiohttp.*`): everything else clean — arrivals 842.5/s, 1 hot s, 0 sent-no-answer. Sent 50,000 / answered 50,000 (134 × 200, 49,866 × 409), 0 QueueTimeouts, **0 × 502, 0 nginx error-log lines**; 842.5/s; p50 5.6ms / p99 2,255ms; distribution 16,666 / 16,667 / 16,667; 5/5 PASS.
  - **Lag is startup-only:** all 609 lagged requests (1.22%) are in t=0–5s (p99 618ms, worker CPU 75%); every later 5s window has p99 1.5–9ms. But t=0–5s is the sale phase, so the client distorted exactly the purchases → the INVALID label stands.
  - Likely cause (not proven): each of 2 workers opening up to 1,000 new connections at once as arrivals start.
- **M10 step — PRE-REGISTERED:** one 50k run with **4 aiohttp workers** (cap 2000 → 500 each), otherwise identical. Justification: necessary (client-invalid) and headroom exists (host peak 4.98 of 16 cores). Same strict criteria; no further steps without reporting back.

- **M10 50k, 4 aiohttp workers — VALID (pre-registered criteria), SUSTAINS 833/s** (`results/m10_50k_833rps_3sellers_aiohttp_4buyer.*`, window analysis `.windows.txt`):
  - Client: arrivals 838.6/s over 59.62s; sched lag p99 33.4ms; max hot s 0 (worker CPU p50 30%, max 62–63%); 0 sent-no-answer; containers unchanged. Buyer container CPU mean 134% / max 372%. Host mean 403% / max 591% of 1,600%.
  - Scheduled 50,000; sent 50,000 (0 QueueTimeouts); answered 50,000 = 129 × 200 + 49,871 × 409; **0 × 502, 0 nginx [error] lines, 0 transport errors**. Served **838.5/s** over 59.63s (first send → last answer). 100th purchase answered at t=1.87s.
  - Latency (all): p50 6.6ms / p99 2,641ms. By arrival window: t=0–5s p50 1,423ms / p99 4,036ms (sale phase: all requests on the locked path until sell-out, up to 29 of 30 connections waiting); arrivals after t=5s p50 6.4ms / p99 149.8ms.
  - CPU mean/max: seller1 67/104%, seller2 67/104%, seller3 67/104%, nginx 29/49%, Postgres 49/81%.
  - Locks: 0 waiters in 86.2% of busy samples; mean 2.61 (busy); up to 28–29 waiters during the sale phase (30 connections); ungranted tuple 2.42 / transactionid 0.22.
  - Distribution: 16,666 / 16,667 / 16,667 (seller logs = X-Upstream). 8,309 request_ids had copies on >1 instance; 14 ticket holders among them → same ticket on all copies. **5/5 PASS.**
  - **Disclosed caveat:** startup lag remains in t=0–5s (sched-lag p99 369ms there; 282 requests >100ms = 0.56%, 102 >300ms; 2 workers had 609 / 324). It passes the pre-registered overall p99 rule, but falls in the sale phase.
- **M9 502s did not reproduce** in either M10 50k run (0 × 502, 0 nginx error lines). Differences from M9: client library (aiohttp vs httpx) and seller load pattern (M9 cluster was saturated with a pegged buyer; here sellers mean 64–67%). One non-reproduction does not identify the cause; the keep-alive hypothesis stays untested.

- **M11 datastore-failure experiment — DESIGN APPROVED by the user, PRE-REGISTERED before any harness code or run.**
  - Facts it rests on (read-only inspection): `fsync=on`, `synchronous_commit=on`, `full_page_writes=on` → a COMMIT that returned is durable across a Postgres crash. Seller returns 200 only after the transaction block exits (`app.py:118`). `app.py` has no try/except around DB calls → DB failure = HTTP 500. db data on an anonymous volume, restart policy `no` → only pause/unpause and kill/start on the SAME container are safe; never down/rm/recreate. asyncpg 0.29, 10 pooled conns per seller. nginx default `proxy_next_upstream error timeout` → seller 500s do not eject upstreams. Constraints present (7): sale.{sale_check, sale_id_check, sale_pkey, sale_total_tickets_check}, tickets.{tickets_pkey, tickets_request_id_key, tickets_ticket_number_check}.
  - Workload: the valid M10 workload (50,000 @ 833.33/s, 100 tickets, 20% × 2 dup, 3 sellers + nginx, 4 aiohttp workers, cap 2000, pool 10/instance, fastpath 1, seed 1). Fault at **t = 1.0s** after the shared start (M10 sold the 100th ticket at 1.87s → mid-sale), **10s**. Order: **C0** (no fault, modified harness, must reproduce M10) → **E1** `docker pause` db → **E2** `docker kill -s SIGKILL` db then `docker start` the same container.
  - Expected after recovery: ≤ 100 rows; no duplicate ticket_number; no request_id with 2 tickets; no lost confirmed sale (every 200 in final state AND replay returns the same ticket); sold_count = rows. A committed-but-unacknowledged sale (COMMIT flushed, ack lost → buyer 500, row exists) is NOT a violation; replay resolves it. At most the single lock holder can be committing at the kill → expect 0 or 1; not observing it is not evidence it cannot happen.
  - Classification (no retries during load): 200 confirmed; 409 sold out; 5xx (seller 500 / nginx 502/504) outcome unknown; ReadTimeout/ReadError/ServerDisconnected sent-no-answer (unknown); ConnectError/ConnectTimeout/QueueTimeout never sent (a row for these = FAIL, check 5). Replay only in a separate post-run phase.
  - **Validity for fault runs only (user-approved):** client validity judged by arrivals ±2%, sched lag p99 < 100ms, no worker ≥90% > 3s, and container identity (db same container + volume; sellers/nginx unchanged). 5xx and sent-no-answer are expected fault outcomes: reported in full per second, not invalidating. C0 keeps the full strict criteria.
  - Lost-sale detection: check 5 at run end + `scripts/replay_check.py` after recovery and sell-out: S1 = `/status`; replay every 200'd request_id → must be 200 with the same ticket; replay every unknown-outcome request_id → must be 200 with its S1 ticket if S1 has that user's row, else 409 (skipped, and recorded, if not sold out); S2 = `/status` must equal S1 byte-for-byte. Plus direct SQL audit (count(*), sold_count, max(ticket_number), count(distinct ticket_number), count(distinct request_id)) and the 7 constraints present + validated.
  - **Predictions — E1 (pause):** no 5xx; requests stall ~10s then complete; in-flight cap fills (833 × 10 ≈ 8,300 arrivals vs 2,000 cap) → some QueueTimeouts possible during catch-up; latency spike; 5/5 and replay match. **E2 (kill):** fast 500s during outage (connection refused, then "database system is starting up"); crash recovery in seconds; sellers recover without restart, with an initial burst of up to ~30 × 500 as stale pooled connections fail once; 0 lost confirmed sales; 0–1 committed-but-unacknowledged sales found by replay; 5/5 after recovery; buyers failed during the outage get no ticket (no retries) — a fairness finding, not a correctness violation.

- **M11 harness changes (no seller/schema/nginx/correctness changes):** `buyer.py` `--start-at` (refuses if < 2s remain after /reset), `start_at` in raw output, 5xx reported as their own class; `pgsample.py` survives outages (per-call timeouts because a paused db hangs rather than fails; failed samples written as `unreachable` with error type; reconnect; summary reports unreachable spans); new `scripts/inject_db_fault.sh` (pause|kill on the captured container ID only; trap restores; timestamped log; bounded `docker events` to a separate file; waits for `pg_isready`; verifies container + volume); new `scripts/replay_check.py` (S1 vs buyer snapshot, replay confirmed + unknown-outcome ids, S1 == S2 byte-for-byte, SQL audit, 7 constraints); `bench_cluster.sh` optional `FAULT=`, shared `--start-at` on every run, db volume identity, always-collected db/nginx/seller logs, per-instance counts by status. Tested non-destructively: py_compile, verifier tests, sampler outage path (29/29 unreachable, ConnectionRefusedError, no crash).
- **INCIDENT (my error): one unapproved 12-second db pause before C0.** A "safe" dry run of the first injector version (offset 3600s, then interrupted) actually paused the db: `trap restore ... TERM` *returned* instead of exiting, so bash continued past the interrupted sleep into the injection. Docker's event log (authoritative): exactly one `pause` 22:16:10 → `unpause` 22:16:22; the two earlier dry runs did not pause. No buyer/sampler running (no load), db never restarted (same StartedAt, restarts=0), same container + volume, Postgres log clean. Violated the user's "no injection before C0". Fixes found through further DRYRUN-only testing: (1) INT/TERM trap now restores and EXITS; (2) `DRYRUN=1` mode logs instead of executing state-changing docker commands — the only way the injector is tested from now on; (3) sleeps are `sleep & wait` (bash defers traps during a foreground sleep — an interrupt would have waited up to an hour); (4) `docker events` writes to its own file (the Windows `docker.exe` ignores POSIX append and overwrote the main log in a test); (5) stdin from /dev/null when backgrounded. DRYRUN tests: interrupt before injection → exits, no injection; interrupt during window → restore then exit; full cycle → all steps logged; db event log shows no pause/kill/start during these tests.
- **M11 C0 control — NOT CLEAN; faults NOT injected** (`results/m11_C0_control.*`):
  - Client CLEAN (arrivals 839.2/s, sched p99 49.7ms, 0 hot s, 0 sent-no-answer); 0 QueueTimeouts; served 811.7/s (M10: 838.5); latency p50 46.3ms / **p99 8,474ms** (M10: 6.6 / 2,641). Correctness 5/5; **replay check PASS** (S1 = buyer snapshot; 100 confirmed replays match; 3 unknown-outcome ids → 409, 0 committed-but-unacknowledged; S2 == S1 byte-for-byte; SQL audit consistent 100/100/100/100/100; 7/7 constraints). DB: same container + volume, 0 Postgres log lines this run.
  - **Not clean because: 3 × 502** (all seller2, within 1ms at t=43.47s; nginx: `recv() failed (104: Connection reset by peer) while reading response header from upstream`) → nginx passive health check ejected seller2 for 10s (0 answers t=44–52) → distribution 18,018 / 13,960 / 18,019 and the p99 inflation. **The M9 502 issue reproduced with no fault injected** (now 2 of 4 50k@833 runs: M9 yes, M10 ×2 no, C0 yes).
  - Evidence: uvicorn `timeout_keep_alive` = 5s (default); nginx upstream `keepalive 256`, `keepalive_timeout` unset = 60s. A throughput dip on all three instances (t=42–43s) preceded the resets, as in M9. Consistent with the keep-alive race hypothesis (nginx reuses a cached upstream connection that uvicorn closed after 5s idle); NOT proven — no capture of the failing connection's idle age.
  - Impact on M11: a spontaneous 502 + 10s ejection inside the fault window would confound attribution (distinguishable by status/log — nginx 502 vs seller 500 — but the traffic shift and latency would overlap).

- **M11b 502 diagnosis — PRE-REGISTERED (user chose option A: measurement only; no nginx/uvicorn/app/Postgres changes, no retries, no fault injection).** One normal 50k @ 833.33/s cluster run (M10/C0 setup) with a packet capture of nginx's upstream side (tcp port 8000, captured in the lb container's network namespace by a tcpdump sidecar, snaplen 128).
  - **Keep-alive race PROVEN for a 502 only if** its TCP connection shows: last data at t₀ → **seller FIN at ≈ t₀ + 5s** (uvicorn idle close) → **nginx sends a new request on that connection** at/after the FIN's arrival → **seller RST**, coinciding with nginx's 502. **Refuted for that 502** if the RST has no preceding seller FIN, or the FIN comes at an idle time far from 5s.
  - If no 502 occurs in this run (2 of 4 prior runs had them): report exposure only (count of seller FINs after ≈5s idle; nginx requests sent shortly after a seller FIN). Non-reproduction is not disproof.

- **M11b 502 diagnosis — RESULTS (measurement only; no config/app/db changes):**
  - Tooling: `diag/Dockerfile.tcpdump` (python:3.12-slim + Debian tcpdump), `bench_cluster.sh` optional `CAPTURE=1` (tcpdump sidecar in lb's netns, `tcp port 8000`, snaplen 128), `diag/analyze_resets.py` (pure-python pcap parser; RST/FIN/idle/reuse analysis). pcaps git-ignored.
  - **Positive control** (`results/m11b_capture_smoke.*`, 1,000 @ 100/s): 9 upstream connections, 9 closed by the **seller after 5.000–5.144s idle (9 of 9)**, nginx never closed first, 0 RST, 0 reuse-after-FIN. → **Measured: uvicorn closes idle nginx upstream connections at ≈5.0s; nginx keeps them open until then.** (Half of the proposed mechanism.)
  - **50k diagnostic run** (`results/m11b_diag_50k_capture.*`): **502 did NOT reproduce** (0 × 502, 0 nginx error lines; 837.7/s, 50,000/50,000, 0 QueueTimeouts, 16,666/16,667/16,667, p99 2,613ms, 5/5). Now 2 of 5 50k@833 runs had 502s.
  - **Capture unreliable at 50k — per-connection results NOT valid:** raw dump of one 4-tuple shows a full FIN/FIN close at t=11.83s followed by data until t=60s with no SYN (impossible without a new handshake) → SYNs (and other packets) were lost by the capture; ~31,700 packets sat on 4-tuples with no captured SYN; the "request 46s after FIN" and idle-time figures are artifacts. Likely kernel buffer drops at ~90k packets; tcpdump's own drop counter was lost because the sidecar ran with `--rm` (my omission). Parser also initially merged reused source ports (fixed: new generation per SYN).
  - Still unproven: that a 502 is a request sent on a connection the seller had just closed after 5s idle. Missing: a loss-free capture of a run that actually produces a 502.

- **M11c repeat-until-502 series — PRE-REGISTERED (user-approved; measurement only; no nginx/uvicorn/seller/Postgres/Docker config, app or db changes; no fault injection; no timeout changes).**
  - Capture fixes: tcpdump `-B 262144` (256 MiB buffer); write to the sidecar's own /tmp (not the Windows bind mount), copy out with `docker cp` after stop; no `-U`; filter = tcp port 8000 AND (SYN|FIN|RST OR payload > 0) (pure ACKs excluded); snaplen 96; tcpdump's exit statistics saved (`.capstats`).
  - **Diagnostically valid capture iff:** "dropped by kernel" = 0 AND analyzer consistency = 0 (no data on a 4-tuple after a full FIN/FIN close without a new SYN; no packets on 4-tuples whose SYN was not captured).
  - Series: normal 50k @ 833.33/s, 3-seller cluster, C0/M10 settings; up to 5 runs; **stop at the first run with a 502 and a valid capture.**
  - **Keep-alive race PROVEN for that 502 only if** the capture shows on its connection: last data (t₀) → seller FIN at ≈ t₀ + 5s → nginx request data after the FIN arrived → seller RST, and the RST coincides with the buyer's 502 answer time and nginx's error-log line. Otherwise report the actual sequence.
  - If no valid 502 in 5 runs: hypothesis remains unproven; report idle-close behavior and capture quality.

- **M11c series RESULTS — 5 of 5 captures VALID, 0 × 502 → keep-alive race NOT proven** (`results/m11c_*`):
  - Low-rate check of the fixed capture (`m11c_capture_smoke`): 3,030 packets, 0 kernel drops, consistency 0/0; 6/6 connections closed by the seller at 5.001–5.002s idle (positive control reproduced).
  - Analyzer improvement during the series: who closed a connection = the side whose FIN came first (earlier counts mixed initiating FINs with reply FINs). Re-run on saved pcaps.

    | rep | capture (kernel drops / consistency) | 502 | seller-initiated closes (idle) | nginx-initiated closes (idle p50) | nginx request after seller FIN | seller RST | client (benchmark rule) | 5 checks |
    |---|---|---|---|---|---|---|---|---|
    | 1 | 155,553 pkts, 0 / 0 VALID | 0 | 328 (4.990–5.005s) | 1,056 (0.43s) | 0 | 0 | clean | PASS |
    | 2 | 163,348 pkts, 0 / 0 VALID | 0 | 566 (4.991–5.003s) | 2,755 (0.81s) | 0 | 0 | not clean (sched p99) | PASS |
    | 3 | 156,208 pkts, 0 / 0 VALID | 0 | 525 (4.994–5.007s) | 1,004 (0.42s) | 0 | 0 | clean | PASS |
    | 4 | 157,144 pkts, 0 / 0 VALID | 0 | 387 (5.000–5.022s) | 1,392 (0.40s) | 0 | 0 | clean | PASS |
    | 5 | 157,598 pkts, 0 / 0 VALID | 0 | 502 (4.998–5.012s) | 1,389 (0.66s) | 0 | 0 | clean | PASS |

  - **Proven (measured, loss-free):** uvicorn closes idle nginx upstream connections at 4.990–5.022s (2,308 closes across 5 runs, all in that band); nginx never idle-closes first (its own closes are cache trimming at 0–3.7s, not idle timeouts); in these runs nginx never sent a request on a connection after the seller's FIN arrived, and no seller ever sent a RST.
  - **Not proven:** that the observed 502s (M9, C0) are this race — no 502 occurred under a valid capture.
  - Frequency tally, 50k @ 833/s cluster runs: 502 in 2 of 10 (M9, C0); 0 of 6 with capture on vs 2 of 4 without — too few runs to separate an observer effect (the capture sidecar shares the lb network namespace and adds CPU) from chance.

- **M11 E1/E2 — authorized by the user; PRE-REGISTERED additions before running** (methodology otherwise as in the M11 design; no nginx/uvicorn/app/db configuration changes):
  - No-fault reference: the clean runs of the identical workload with this harness — M10 (4 workers) and M11c reps 1, 3, 4, 5 (0 × 5xx, 0 transport errors, 50,000/50,000, ~837–838/s, 5/5).
  - **502 confound rule:** any 502 whose nginx error-log line is `Connection reset by peer ... while reading response header from upstream` is a baseline/network confound — reported, and neither it nor that upstream's subsequent 10s ejection window is attributed to the PostgreSQL fault.
  - E2 classification: database-induced 5xx = seller 500 (confirmed by the exception types in the seller logs); transport errors/timeouts = non-HTTP outcomes; committed-but-unacknowledged = unknown-outcome request_id whose user holds a row in S1 (replay returns that ticket); **lost confirmed purchase = a 200'd request_id whose replay does not return the same ticket or is absent from S1** — an error response alone is never counted as a lost sale.
  - Client validity for fault runs: arrivals ±2%, sched lag p99 < 100ms, no worker ≥90% CPU > 3s, container identity (db same container + volume; sellers/nginx unchanged). 5xx and sent-no-answer are fault outcomes, not invalidating.

- **M11 E1 — PostgreSQL `docker pause` 10s — RESULTS** (`results/m11_E1_pause.*`, timeline `.timeline.txt`, replay `.replay.txt`):
  - **Fault landed AFTER sell-out, not mid-sale (pre-registered intent missed):** `docker pause` issued t=1.397s, took effect t=3.676s (docker event; ~2.3s Docker Desktop latency); unpause issued t=14.341s, effective t=16.424s; pg_isready OK t=19.247s. The 100th sale was at t=1.698s → the pause hit the sold-out phase; no purchase transaction in flight. My design assumed docker commands take effect immediately. Not re-run with a new offset (would change the pre-registered protocol).
  - Client VALID under fault-run rules (arrivals 836.9/s, sched p99 8.8ms, 0 hot s, containers unchanged).
  - HTTP: 130 × 200, 49,868 × 409, **0 × 5xx**, 0 QueueTimeouts, **2 × ReadTimeout** (sent-no-answer): `req-1391` sent t=1.976s, `req-12732` sent t=28.977s (well after recovery); neither ever answered nor logged by any seller (49,998 logged); replays → 409, no rows. **Unexplained.**
  - Pause behaviour: zero answers t=4–15 (requests hang, no errors); answering resumed the second the container unpaused (t=16: 467 × 409). 0 nginx error lines; 0 seller exceptions.
  - Backpressure/latency: queue wait p50 6.8s / p99 11.8s / max 11.9s (in-flight cap 2000 full), 0 QueueTimeouts; latency p50 by arrival window 13.9s (t=0–5) falling to 3.7s (t=55–60) — the backlog (~12.7s × 833/s) was still draining at the end; catch-up answer rate 900–1,237/s per second. Served 774.6/s over 64.55s.
  - pg sampler: 11 unreachable samples (TimeoutError — pause hangs, as designed).
  - **Correctness 5/5; replay PASS** (S1 = buyer snapshot; 100 confirmed replays same ticket; 2 unknown → 409, 0 committed-but-unacknowledged; S2 == S1 bytes; SQL audit 100/100/100/100/100; 7/7 constraints).
- **M11 E2 — PostgreSQL SIGKILL, `docker start` same container after 10s — RESULTS** (`results/m11_E2_kill.*`):
  - **Fault also landed AFTER sell-out:** kill issued t=1.412s, `kill` event t=3.102s, `die` t=3.758s; start issued t=14.335s, `start` event t=16.850s; Postgres: "database system was interrupted" 04:15:22.182 → "not properly shut down; automatic recovery in progress" → redo 0/4668AF0..0/46A11C0 (0.00s) → end-of-recovery checkpoint → **"ready to accept connections" 04:15:23.320 UTC = t=18.41s** (≈1.2s after process start; ≈15.3s after the kill). Same container + volume; db restart count 0 (explicit docker start).
  - Client VALID under fault-run rules (arrivals 837.5/s, sched p99 18.7ms, 0 hot s, containers unchanged).
  - HTTP totals: 122 × 200, 32,554 × 409, **189 × 500** (sellers), **17,135 × 502** (nginx), 0 transport errors, 0 QueueTimeouts. Non-200/409 from t=3.134s to t=23.793s.
  - **Database-induced 5xx (seller 500s), by exception:** ConnectionRefusedError 80, CannotConnectNowError ("database system is starting up") 66/75, ConnectionDoesNotExistError ("connection was closed in the middle of operation") 30, ConnectionResetError 17, and **7 × `AttributeError: 'NoneType' object has no attribute 'terminate'` inside asyncpg 0.29 `pool.release()`** (a library error path when a pooled connection dies mid-operation).
  - **nginx 502s:** 11 × `recv() failed (104: Connection reset by peer) while reading response header from upstream` — 10 in the kill second (04:15:08, t≈3.1, all three sellers), 1 at 04:15:19 (seller .4) — then **17,124 × `no live upstreams`** (04:15:08–04:15:28): nginx's default passive health check (max_fails=1, fail_timeout=10s) had marked all three sellers down, so almost no requests reached them.
  - **502 confound rule vs evidence:** by the pre-registered rule the 11 reset-502s and each upstream's following 10s ejection window are "baseline/network confounds" (not attributed to the fault). The evidence contradicts that premise here: all resets fall in the kill second (+1 during recovery), on all three sellers at once, while 0 resets occurred in 5 loss-free captured baseline runs. The seller-side mechanism of the reset is not established.
  - Recovery: sellers were **not restarted** and answered again as soon as Postgres accepted connections (t=17–19: 500s then 192 × 409 at t=18, 92 at t=19) — asyncpg pools reconnected by themselves; nginx re-ejected after the 04:15:19 reset; **sustained service resumed t≈23.75–23.82s on all three**, ≈5.4s after Postgres was ready, due to nginx's ejection.
  - Latency: arrivals during the outage failed fast (p50 ~2.3ms, 502); arrivals t=0–5s p50 1.4s / p99 25.2s (in flight at the kill; some answered only at recovery); from t=30s p99 ≤ 141ms.
  - pg sampler: 31 unreachable samples (TimeoutError, ConnectionRefusedError, CannotConnectNowError, InterfaceError).
  - **Correctness 5/5; replay PASS** (100 confirmed replays same ticket; 14,393 unknown-outcome replays → 409, 0 rows; S2 == S1 bytes; SQL audit after crash recovery 100/100/100/100/100; 7/7 constraints). **0 lost confirmed purchases; 0 committed-but-unacknowledged.** 2 ticket holders (req-150, req-112) received a 500 on a duplicate copy during the outage — tickets intact, replay returns them.
  - **Scope limit:** 100th sale t=1.679s < kill t=3.102s → no purchase in flight at the kill; the committed-but-unacknowledged window was not exercised. Demonstrated: committed sales survive SIGKILL + crash recovery; sold-out answers stay correct after recovery.
- `scripts/fault_timeline.py` added (per-second outcomes, fault/docker-event timeline, latency by arrival window, seller exceptions, nginx/db logs). `run_eval.py` fixed to skip the sampler's `unreachable` rows.

- **M11 kill-delay calibration** (`diag/calibrate_kill_delay.sh`, `results/m11_calibration.txt`): throwaway `postgres:16` container (default network, own volume, removed each trial; ticket db verified untouched — same container and StartedAt). `docker kill -s SIGKILL` issue → Docker `die` event over 10 trials: 1.692, 1.760, 1.978, 2.045, 2.049, 2.105, 2.235, 2.466, 2.645, 4.395s — median 2.05s; 9/10 within 1.69–2.65s; one 4.4s outlier. Consistent with E2 (issue → die 2.35s).
- **M11 E2b — mid-sale PostgreSQL SIGKILL — PRE-REGISTERED before running** (user-approved; workload unchanged: 50,000 @ 833.33/s, 100 tickets, 20% × 2 dup, 3 sellers + nginx, 4 aiohttp workers; no app/schema/config changes):
  - Scheduling: `FAULT=kill:-1.15:10` → kill issued 1.15s BEFORE the buyer's t=0, targeting `die` ≈ t=0.9s (calibrated median delay 2.05s; the M10/C0 sale window ≈ t=0.1–1.7s).
  - **VALID E2b iff** (a) client-valid under the fault-run rules (arrivals ±2%, sched lag p99 < 100ms, no worker ≥90% CPU > 3s, container identity), and (b) **Docker's `die` event occurs while the sale is in progress: at least one 200 (purchase) answered before `die`, and the 100th distinct ticket's first 200 answered after `die`.** Supporting (not required): the last pg sample before `die` shows purchase-path activity (lock waiters or an active purchase).
  - Up to **3 attempts**; the first valid run is E2b; every attempt is reported.
  - Per unique request_id, three outcome classes (using final DB state + replay, never the error alone): (1) **rolled back** — no 200 on any copy, no ticket row, replay → 409 after sell-out; (2) **committed and acknowledged** — a 200 was received; replay returns the same ticket; (3) **committed but unacknowledged** — no 200 on any copy, but a ticket row exists; replay returns it. A lost confirmed purchase = a 200'd request_id whose ticket is missing/different. Also: seller tracebacks classified by the statement that failed (lock wait / INSERT / UPDATE / COMMIT).
  - Expectation stated up front: a random kill is unlikely to hit the sub-millisecond commit-acknowledgement window, so class (3) will probably be 0; if so, that is documented as not exercised, not as proven impossible.

- **M11 E2b — mid-sale PostgreSQL SIGKILL — VALID on attempt 1 of 3** (`results/m11_E2b_kill_midsale_a1.*`; outcome analysis `.outcomes.txt`):
  - Timeline (t rel. to buyer start): kill issued t=−0.769s; Docker `kill` event t=1.590s; `die` t=2.130s (issue→die 2.90s — within calibration range, upper tail; target was ~0.9s); `docker start` issued t=13.112s, `start` event t=17.307s; Postgres "interrupted" → "not properly shut down; automatic recovery in progress" → redo 0/46A1260..0/46D0500 (0.00s) → **"ready to accept connections" t=18.809s** (1.5s after the start event; 17.2s after the kill). First purchase answer t=1.086s (later than in no-fault runs — unexplained, possibly host CPU spent on the docker command).
  - **Validity:** client clean (arrivals 838.1/s, sched p99 32.5ms, 0 hot s, containers unchanged); **sale in progress at `die`** (34 distinct tickets confirmed before `die`; the 100th only at t=23.395s) → VALID.
  - **In-flight transaction at the crash (seller tracebacks → app.py line):** 30 requests inside the purchase transaction: 29 waiting for the sale-row lock (`SELECT … FOR UPDATE`, line 93), **1 holding the lock and executing the ticket `INSERT` (line 113) — before `UPDATE sold_count` and before `COMMIT`**; 0 failed at COMMIT (the 30 `Transaction.__aexit__` InterfaceErrors are chained to those failures). 75 more failed acquiring a new connection while Postgres was down (ConnectionRefusedError, line 69). 1 ConnectionResetError.
  - **Committed vs rolled back:** tickets #1–#33 first confirmed before the kill (contiguous); **#34 confirmed at t=1.591s — 1ms after SIGKILL: committed before the kill, acknowledgement already in transit (closest approach to the ambiguous window; acknowledged)**; #35–#100 first confirmed only after recovery (t=22.44–23.40s). → the in-flight INSERT was writing #35 and **was rolled back** by crash recovery (no row, no counter change); #35 then sold normally after recovery — which also shows atomicity (a surviving INSERT without its counter update would have made the next #35 insert hit the primary key).
  - **Per unique request_id (final state + replay):** (2) committed + acknowledged 100; (1) no committed ticket 14,492 (all replayed → 409, no row); **(3) committed but unacknowledged 0 — not exercised** (the in-flight transaction died at INSERT, not in the sub-ms commit-acknowledgement window). 0 lost confirmed purchases.
  - Duplicate copies around the kill (6): req-236/req-150 confirmed #14/#15 before the kill, second copy 500 during the outage (tickets intact). **req-42/154/20/220: one copy 500 at the kill; the other copy (sent t≈0.38s) stayed pending through the whole outage and bought a post-recovery ticket (#88/#50/#59/#42) at t≈22.7–23.3s** (~22.9s latency, under the 30s client timeout) — exactly one ticket each.
  - HTTP: 123 × 200, 32,422 × 409, **105 × 500** (sellers: database errors), **17,350 × 502** (nginx: 3 × `Connection reset by peer` at the kill — one per seller — then 17,347 × `no live upstreams` until ≈t=21.5s), 0 transport errors, 0 QueueTimeouts.
  - **Recovery:** sellers not restarted (containers unchanged); Postgres ready t=18.809s; **first seller answers after recovery t=22.38–22.78s on all three → ≈3.6–4.0s of additional outage after Postgres was ready**, attributable to nginx's passive ejection (sellers answered immediately once nginx re-admitted them; re-admission follows nginx's 10s fail_timeout cadence from the kill). Same pattern as E2 (5.4s).
  - Latency: arrivals during the outage failed in ~2.3ms (502); arrivals t=20–30s p99 3.3–5.0s (backlog + pending requests completing); from t=30s p99 ≤ 235ms.
  - **Correctness 5/5; replay PASS** (100 confirmed same ticket; 14,492 unknown → 409; S2 == S1 bytes; SQL audit after crash recovery 100/100/100/100/100; 7/7 constraints).
- **Architecture notes (for DECISIONS.md, user-requested):**
  - **Scaling constraint:** the seller tier scales horizontally because sellers are stateless (M9/M10: 3 instances, ~838/s); **ticket allocation is intentionally serialized through one PostgreSQL row** (`SELECT … FOR UPDATE` on the sale row). Measured sale-phase ceiling ≈ 210–215 purchases/s single-seller (M7 A/B) and the 100-ticket sale takes ~1.7s at 833/s arrivals. PostgreSQL — specifically the hot sale row — is the scaling constraint; the system is not "infinitely scalable".
  - **Durability:** ticket state lives in PostgreSQL (fsync=on, synchronous_commit=on, full_page_writes=on), never in seller memory. E2/E2b demonstrated PostgreSQL crash recovery from WAL after SIGKILL: every acknowledged sale survived, the in-flight transaction rolled back, invariants held.
  - **Limitation:** one database container on one Docker volume on one host = a single database/storage failure domain. Crash recovery covers process death, not loss of the disk/host; that would need synchronous replication with fenced failover or backups (production extension, not implemented — see the replica discussion).

- **Replay viewer (user-approved; no monitoring stack):** `scripts/build_viewer.py` + `viewer/template.html` → self-contained `viewer/index.html` (≈52 KiB, no external libraries, opens offline) for E2b, E2, E1 and the M10 baseline. Everything derived from saved results: per-second outcomes/latency/sold/per-seller answers from the raw per-request records; events from Docker's event stream and the Postgres log (ms precision); nginx's "no live upstreams" shown only as a window (its log has 1 s resolution); checks from `verify()`; replay output from `replay_check.py`. Shows the architecture state at a scrubbed second, a per-second component-state strip (status colours always with icon + label), responses per second (200/409/500/502), tickets sold, p50/p99 latency (log scale), answers per seller, the five checks, replay results, a timeline and a data table. Reference palette slots validated with the dataviz validator (light + dark: all checks pass; light-mode contrast WARN for aqua/yellow → legend + data table as relief). Rendered and inspected; two event-ordering artefacts fixed before commit (nginx 1 s log resolution; recovery bucketed to the second).
  - Shows the E2 nuance precisely: first answer 18.537s (0.13s after Postgres ready), sustained service only from 23.750s (nginx re-ejection); E2b: sustained from 22.381s (3.6s after Postgres ready).

- **M12 commit-reply-drop experiment — PRE-REGISTERED before building/running** (user-approved; test-only; no app/schema/production-config changes):
  - Test topology (compose override `diag/compose.commitdrop.yml`): buyer → single `seller` (normal safe code, fast path on) → **`commitproxy`** (`diag/commit_drop_proxy.py`) → PostgreSQL. Replay/verification goes through the normal cluster (sellers connect to PostgreSQL directly).
  - Proxy: reassembles Postgres protocol messages both ways; on server→seller `CommandComplete` with tag `COMMIT` it applies the drop rule (every Nth COMMIT, capped): the message is not forwarded and the seller's connection is reset. Postgres sends that message only after the commit is durable. It tracks Parse/Bind on the seller→server side to log, per dropped COMMIT, the INSERT's request_id / user_id / ticket_number (or "no INSERT in this transaction").
  - **Proof the drop happens after commit (probe, no tables):** three transactions through the proxy with `pg_current_xact_id()`: #1 COMMIT forwarded, #2 COMMIT dropped, #3 ROLLBACK; then a direct connection reads `pg_xact_status`. Expected: committed / **committed (client saw an error)** / aborted.
  - Workload: 400 arrivals @ 100/s, 100 tickets, 20% × 2 dup, 1 aiohttp worker, drop every 5th COMMIT, max 15.
  - **Success criterion:** ≥ 1 dropped COMMIT of a purchase whose request_id received no 200 on any copy, whose ticket row exists in PostgreSQL, and whose replay returns exactly that ticket with the SQL row count unchanged; probe shows the dropped transaction committed; 5/5 checks.

- **M12 commit-reply-drop — RESULTS: committed-but-unacknowledged case DEMONSTRATED** (`results/m12_*`; `diag/commit_drop_proxy.py`, `diag/commit_drop_probe.py`, `diag/analyze_commit_drop.py`, `diag/compose.commitdrop.yml`):
  - Two bugs caught before trusting results: the proxy's first version would have reset its INSERT tracking on every ReadyForQuery (status 'T' after each statement) — fixed to reset only on status 'I'; the probe passed xid8 as text — fixed. Probe output also initially hidden by my stderr redirect.
  - **Probe (proves the drop happens after commit):** through the proxy, tx#1 COMMIT forwarded → client normal reply, `pg_xact_status` = committed; **tx#2 COMMIT reply dropped → client `ConnectionDoesNotExistError: connection was closed in the middle of operation`, `pg_xact_status` (direct connection) = committed**; tx#3 ROLLBACK → normal reply, aborted. Proxy log: 2 COMMITs seen, #2 dropped; ROLLBACK not counted.
  - Routing verified: all 10 of the test seller's PostgreSQL connections came from the proxy's IP (172.22.0.3), none from the seller's (172.22.0.4). Verification afterwards through the normal cluster (sellers direct to PostgreSQL).
  - Workload: 400 arrivals @ 100/s, 100 tickets, 20% × 2 dup, 1 aiohttp worker, proxy dropping every 5th COMMIT reply (max 15). Proxy: 125 COMMIT replies, 15 dropped (14 in purchase transactions with a ticket INSERT, 1 read-only).
  - **Per-transaction evidence (targeted replay ×2, SQL count before/after):** **10 committed-but-unacknowledged purchases** — for each: proxy logged the INSERT (e.g. ticket #5 for req-7/user-7) and dropped the COMMIT reply; the buyer's only response for that request_id was 500; PostgreSQL holds the row (e.g. `5|user-7`); replay ×2 → (200, same ticket) both times. 4 more dropped purchase commits where a duplicate copy of the same request_id received 200 with the same ticket. 1 read-only commit. **SQL count(*) 100 before and after the replays.**
  - replay_check: S1 == buyer snapshot; 90 confirmed replays match; 22 unknown-outcome replays match (10 hold a row = committed-but-unacknowledged); S2 == S1 byte-for-byte; SQL audit 100/100/100/100/100; 7/7 constraints. Buyer run: **5/5 checks PASS** (check 5 info: 10 late-confirmed). Success criterion met.
  - **Side finding — cause of the seller-side connection resets (strongly supported, not packet-proven):** each dropped COMMIT also produced exactly one buyer `ClientOSError` on a different request 3–5ms after the 500 (15 ↔ 15; one at 24ms). Source (starlette 0.38.6 / uvicorn 0.30.6, h11): Starlette's `ServerErrorMiddleware` sends a plain 500 (no `Connection: close`) and **re-raises**; uvicorn, seeing an exception after the response started, calls `transport.close()`. The client (buyer or nginx) believes the keep-alive connection is reusable → next request on it fails. Explains nginx's "Connection reset by peer" at the E2/E2b kills (coinciding with seller 500s) that triggered the all-seller ejection. Does NOT explain the spontaneous M9/C0 502s (C0 had 0 × 500). Not changed (would be an application change: e.g. return 503 for DB errors instead of raising).
- **M12 arrival-rate sweep — PRE-REGISTERED before running:** 3 sellers + nginx + PostgreSQL, aiohttp buyer 4 workers, cap 2000, pool 10/instance, fastpath 1, 0ms, 100 tickets, 20% × 2 dup, seed 1; 60s per rate: **833.33, 1000, 1200, 1400/s** (requests = rate × 60). One run per rate (limitation).
  - **Valid** iff: arrivals ±2%; sched lag p99 < 100ms; no worker ≥90% CPU > 3s; 0 sent-no-answer; containers unchanged; **host not saturated: 0 docker-stats samples with summed container CPU ≥ 1400% (of 1600%)**. If a run is invalid only because of the buyer (client CPU/lag) while the host has headroom, one retry at 6 workers is allowed (reported).
  - **Sustainable** at R iff valid AND 0 QueueTimeouts AND 0 × 5xx AND served ≥ 0.97 R AND p99 latency of arrivals after t=5s ≤ 1,000ms (post-sale steady state; baseline ~150–250ms).
  - Result statement = highest R that is sustainable with all lower rates sustainable; the first non-sustainable rate is attributed to the component whose CPU/queue saturates (seller CPU, PostgreSQL CPU, lock waits, nginx CPU), from measurements only.

- **M12 arrival-rate sweep — RESULTS** (`results/m12_sweep_*`, evaluated by `scripts/sweep_rate_eval.py` against the pre-registered criteria; one run per rate unless the pre-registered buyer-only retry applied):

  | run | buyer workers | VALID | SUSTAINABLE | served/s | QueueTO | 5xx | post-sale p50/p99 | seller1/2/3 CPU mean/max | postgres | nginx | buyer | host max |
  |---|---|---|---|---|---|---|---|---|---|---|---|---|
  | 833/s | 4 | yes | **no** (2 × 502; post-sale p99) | 838.7 | 0 | 2 × 502 | 478 / 18,564 ms | 71/103, 57/103, 58/103 | 49/84 | 29/48 | 130/304 | 598% |
  | 1000/s | 4 | **no** (sched p99 151ms) | no (894.6/s; p99) | 894.6 | 0 | 0 | 5,038 / 14,880 ms | 72/102, 74/102, 74/102 | 59/110 | 32/44 | 144/200 | 587% |
  | **1000/s (retry)** | 6 | **yes** | **yes** | **1004.2** | 0 | 0 | **8.3 / 410 ms** | 64/101, 64/102, 64/102 | 53/88 | 30/50 | 130/176 | 608% |
  | 1200/s | 4 | no (1 ReadTimeout — server-side, no retry) | no | 1060.4 | 0 | 0 | 6,194 / 15,454 ms | 69/103, 71/102, 70/102 | 58/96 | 31/47 | 139/266 | 600% |
  | 1400/s | 4 | no (sched p99 160ms; 5 ReadTimeout) | no | 1069.9 | 0 | 1 × 502 | 12,627 / 24,962 ms | 62/102, 75/104, 76/104 | 55/84 | 33/50 | 140/331 | 597% |

  - Correctness 5/5 in all six runs (including overloaded ones). Lock waiters: mean ~1.8–2.5, max 29 (sale phase only) in every run.
  - 833/s: the 2 × 502 are nginx "Connection reset by peer" (t=14.35/14.47s, seller2 and seller3) with **0 seller 500s** → the spontaneous, unexplained 502 type (M9/C0), not the Starlette mechanism; two sellers ejected 10s → seller1 carried 20,422 vs ~14,800 → arrivals t=15–25s p99 ~24s, drained by t=55s. Not a capacity failure, but by the pre-registered rule the run is not sustainable.
  - 1000/s: the two runs disagree (4 workers: client lag, 894.6/s; 6 workers: clean, 1004.2/s) → the 4-worker shortfall is not attributable to the servers.
  - 1200 and 1400/s: served rate **plateaus at ≈1,060–1,070/s** regardless of offered load (+17% offered → +0.9% served), latency grows to p50 6–13s. At the plateau all three seller processes peak at 102–104% (one core each; docker-stats means 62–76% are diluted by sale/drain phases and ~2s sampling); PostgreSQL peaks 84–110% with means 55–59%; nginx ~30–33%; host ≤ 608% of 1,600%.
  - **Conclusion (conservative):** by the strict pre-registered rule no rate is certified as "sustainable with all lower rates sustainable", because the 833/s run was hit by the known intermittent 502 confound. Per rate: 1,000/s sustainable in one valid run; 833/s sustainable in 5 earlier valid runs (M10, M11c reps 1/3/4/5 — separate runs, same workload); 1,200 and 1,400/s not sustainable. **Measured saturation plateau ≈ 1,060–1,070 answers/s; best-supported limiting component: the three single-process sellers** (each at its one-core ceiling at peaks; PostgreSQL, nginx and host with headroom) — consistent with the M7 profile (HTTP/ASGI CPU). Single runs per rate; the sustainable ceiling lies between ~1,000/s and the ~1,065/s plateau on this host (estimate).
  - Buyer note: at ≥ 1,000/s the 4-worker aiohttp buyer can exceed the sched-lag limit; 6 workers were clean at 1,000/s.

- **Viewer rewritten as a story-first evidence report (user request; no new experiments, no backend/methodology changes):** `viewer/index.html` now reads simple → technical: 1 Overview · 2 What "correct" means (5 checks; headline 5/5 across 29 distinct final-design runs, de-duplicated by result name) · 3 Concurrency problem (naive vs row-lock flow; tickets-issued chart from M3/M4) · 4 Idempotency (M9 F2: 200 request_ids × 10 copies across 3 sellers) · 5 Load test (all 833/s runs with per-run verdicts: 5 clean, 1 INVALID, 2 VALID-but-502-affected, not averaged) · 6 Database failure (E1/E2/E2b cards with mini state strips, recovery times, replay; E2b in-flight breakdown) · 7 Committed-but-unacknowledged (vertical scenario diagram, probe, per-transaction table) · 8 Capacity sweep (answered-vs-offered scatter with markers at exact values, no limit line; findings worded as observed) · 9 After sell-out (fast-path A/B) · 10 Decisions (implemented vs production) · 11 Limitations · 12 Production extensions (not implemented) · 13 the existing detailed replay viewer (now incl. sweep 833/1,000/1,400) + raw run table.
  - All numbers derived from saved results: `scripts/sweep_rate_eval.py` refactored to `compute()` (output verified byte-identical to the saved sweep reports) and imported by `scripts/build_viewer.py`; `compute()` now also accepts bench.sh's "seller container unchanged" phrase (fast-path A/B runs were wrongly invalid in the viewer until fixed; sweep output unchanged).
  - Issues found and fixed during verification: sweep markers had been nudged off their true values to separate labels (now exact; verified programmatically); headline double-counted one run (30 → 29); several hard-coded counts replaced by derived ones; narrow-width layout bugs (grid min-width, race chart, commit-drop diagram, table wrapping); five over-strong phrasings softened (422 is a manual check; SERIALIZABLE reasoning not benchmarked; "observed" limiting component; p99 attribution scoped to the reference run; sold-out finality assumes no returned tickets).
  - Deliberately not shown on the page (could be misread as capacity or correctness evidence): M8 multi-worker 833/s runs and the M9 httpx 50k run (buyer-limited, invalid); M7 closed-loop pool sweep (client-contaminated); M11b lossy packet-capture run; the profiling run's throughput (profile proportions are shown, with the caveat); M1–M6 development runs. All remain in `results/` and this log.

## Open questions / to revisit
- **Committed-but-unacknowledged window** not exercised by E2b (random kill; sub-ms window). Deterministic test would need a network fault proxy that drops the COMMIT reply (e.g. Toxiproxy) — documented, not built.
- **Mid-sale fault not achieved:** Docker Desktop applies pause/kill 1.7–2.3s after the command; with the sale over by ~1.7s, a mid-sale fault needs the injection lead time accounted for (or a longer sale) — a protocol change requiring approval.
- **E2 reset-502s** at the kill instant: seller-side mechanism unknown (asyncpg release AttributeError path is a candidate, not established).
- **E1 2 × ReadTimeout** never answered by any seller — unexplained.
- **nginx ↔ uvicorn keep-alive reset (2 × 502 at 50k)** — test the timeout-mismatch hypothesis before changing timeouts.
- **Postgres connection budget:** instances × pool must stay under `max_connections` (100): at pool 10 that caps at ~9 instances; more connections also means more lock waiters in the sale phase (F2 mean 18.7 with 30 conns).
- **Seller multi-worker capacity is unmeasured.** Options: (1) pre-registered keeps-up test at a rate the client can drive cleanly (like the 300/s A/B), e.g. W=1 vs W=4 at ~500/s; (2) a more efficient load generator (aiohttp, or a compiled open-loop tool) — now justified by measured client cost; (3) buyer on a separate machine.
- **Criterion refinement pending the user's decision:** ReadTimeout at the full timeout with a clean client = server outcome vs early transport failures = client/network.
- **Where do the ~8s per 409 go with the fast path on?** Candidate tests: (1) arrival rate the client can sustain (e.g. 300/s) with fast path 0 vs 1 — if fast-path capacity > 226/s, it keeps up where baseline cannot; (2) py-spy on the seller during load; (3) server-side timing of pool-acquire vs handler.
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
