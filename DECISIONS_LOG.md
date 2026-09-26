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

## Open questions / to revisit
- ~~Transport errors~~ → resolved by D25 (Windows-side client path).
- **Client send-lag jitter (0–27ms):** hypothesis = Windows ~15.6ms timer resolution. At 833/s arrivals would leave in bursts of ~13 rather than evenly. Average rate holds; burstiness must be reported with the 50k run.
- DB pool size (`DB_POOL_SIZE=10`) caps how many requests are inside the race window at once. The concurrency the DB sees is the pool size, not the client count. Matters for M3 and M7.
- `/status` does two separate reads (sale row, then tickets): not a consistent snapshot. Fine for naive; decide for the corrected seller.
- Same `request_id` sent with a *different* `user_id`: what should happen? Not yet decided.

## Interview flags
- Naive lost-update feedback loop: `sold_count` drifts low → the capacity check passes for more buyers → oversell gets worse.
- "Didn't you just add a sleep to make it break?" → answer with the 0 ms run data.
