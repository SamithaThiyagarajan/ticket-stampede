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

## Observations
- **M1 smoke test (sequential):** naive seller is correct with no concurrency: replayed `r1` got the same ticket, sold out at 2, `sold` matched rows. The bug only exists under interleaving, so a concurrent client is the only way to expose it.

## Open questions / to revisit
- DB pool size (`DB_POOL_SIZE=10`) caps how many requests are inside the race window at once. The concurrency the DB sees is the pool size, not the client count. Matters for M3 and M7.
- `/status` does two separate reads (sale row, then tickets): not a consistent snapshot. Fine for naive; decide for the corrected seller.
- Same `request_id` sent with a *different* `user_id`: what should happen? Not yet decided.

## Interview flags
- Naive lost-update feedback loop: `sold_count` drifts low → the capacity check passes for more buyers → oversell gets worse.
- "Didn't you just add a sleep to make it break?" → answer with the 0 ms run data.
