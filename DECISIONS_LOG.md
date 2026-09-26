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

## Open questions / to revisit
- `ReadError` in 50ms run 1: seller logged no errors and no non-200/409, so the failure was at the connection level (suspect: Docker Desktop port proxy on Windows). Unproven. Watch whether it recurs under higher load (M7).
- DB pool size (`DB_POOL_SIZE=10`) caps how many requests are inside the race window at once. The concurrency the DB sees is the pool size, not the client count. Matters for M3 and M7.
- `/status` does two separate reads (sale row, then tickets): not a consistent snapshot. Fine for naive; decide for the corrected seller.
- Same `request_id` sent with a *different* `user_id`: what should happen? Not yet decided.

## Interview flags
- Naive lost-update feedback loop: `sold_count` drifts low → the capacity check passes for more buyers → oversell gets worse.
- "Didn't you just add a sleep to make it break?" → answer with the 0 ms run data.
