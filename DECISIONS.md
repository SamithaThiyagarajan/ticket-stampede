# Ticket Stampede — Decisions

Sell exactly 100 tickets while tens of thousands of requests arrive in about a minute. Five checks, recomputed by the buyer from raw responses plus the final `/status`: no oversell, no duplicate ticket numbers, one ticket per `request_id`, every 200 matches the database, `sold_count` equals the rows. **Measured** results are marked separately from *interpretation*; full chronology in `DECISIONS_LOG.md`.

## 1. From a racy seller to a row lock
I first built the obvious seller (read `sold_count`, check, insert `sold_count+1`, write the counter; no transaction or constraints) and pointed the buyer at it. **Measured:** 1,000 concurrent requests for 100 tickets issued 466–570 tickets with a 50 ms check-to-write delay and 160–164 with none; numbers were issued several times; the counter still said 100. Many buyers read the same count before any wrote.

**Decision: one PostgreSQL transaction per purchase, serialized by `SELECT … FOR UPDATE` on the single sale row**, then: existing ticket for this `request_id`? → return it; capacity left? → issue `sold_count+1`, increment the counter, commit. Only one transaction at a time decides about the remaining inventory; the counter and ticket row commit together. **Measured:** same workload, exactly 100 tickets, 0 duplicates, in all runs.
- *App-level lock rejected:* an asyncio lock protects one process only; it cannot cover several worker processes or three sellers.
- *SERIALIZABLE not chosen:* everyone contends on one row, so it would turn contention into serialization failures and retries (reasoning, not benchmarked); the row lock states the critical section directly.
- *Constraints as defence in depth:* primary key on `ticket_number`, `UNIQUE(request_id)`, `CHECK sold_count ≤ total`. **Measured:** the racy code on this schema issued exactly 100 tickets — but 73–78% of buyers got 500s. Constraints protect data; the lock gives correct answers.

## 2. Idempotency and the sold-out path
Networks drop responses and people retry, so a `request_id` must yield at most one ticket; a different `request_id` is a different purchase. **Measured:** 200 request_ids each sent 10 times across three sellers — every copy of every winner got the same ticket.

After sell-out almost every request only needs "409", yet queued for the lock. **Decision: a lock-free fast path** reading, in **one snapshot**, "does this `request_id` own a ticket?" and "is it sold out?". Rows and counter commit together, so a snapshot that says sold out contains every row: "sold out and not yours" is final (tickets are never returned). Two separate reads would reopen a race. **Measured (300 arrivals/s, 3 runs each):** without it the seller fell behind (210–216/s, 409 p50 13–16 s); with it 300.3/s, 409 p50 9–13 ms. *Interpretation:* the lock was a real ceiling, ≈210 purchases/s on one seller.

## 3. Scaling out — and not trusting the load generator
Sellers are stateless, so I ran **three sellers behind nginx (round-robin) on one PostgreSQL**; correctness still lives only in the database. **Measured:** requests spread ≈1/3 each; duplicates routed to different instances returned one ticket.

Several early benchmarks measured the client, not the service: one httpx process topped out near 150 req/s and eight saturated the CPU at 833/s. I found this with profiles and per-process CPU timelines, moved to a **multi-process aiohttp open-loop buyer**, and fixed validity rules before each run (arrival rate ±2%, scheduling-lag p99 < 100 ms, no saturated client, no unexplained errors).

**The 50k workload** is 50,000 **open-loop arrivals over ~60 s (≈833/s)** — not 50,000 simultaneous connections. **Measured:** 5 valid clean runs answered 836.9–838.5/s, 0 errors, 5/5 checks. Two further 833/s runs had valid load generation but were disturbed by nginx 502s (§5); they are not counted as clean.

**Arrival-rate sweep** (60 s per rate, one run each). **Measured:** 1,000/s was sustainable in one valid run (1,004/s answered, post-sale p99 410 ms). At 1,200 and 1,400/s answers levelled off at 1,060–1,070/s; both runs are invalid by the pre-set rules (1,200/s had a genuine server-side timeout). *Interpretation:* an **observed saturation region, not a proven maximum**; each single-process seller hit ~100% of a core while PostgreSQL averaged 55–59% CPU, so seller CPU is the observed limit.

## 4. When PostgreSQL fails
- **Pause 10 s:** requests hung, then completed; no database errors. **Kill + restart:** in-flight work failed with 500s, PostgreSQL recovered from its WAL ~1.2–1.5 s after restarting, sellers reconnected unaided. Both faults landed after sell-out (Docker applies them ~2 s after the command).
- **Kill mid-sale**: 33 tickets confirmed before the kill, one just after; the transaction holding the lock died mid-INSERT, before COMMIT, and was rolled back.
- **Committed but unacknowledged**, created with a test-only proxy that drops PostgreSQL's "COMMIT complete" reply (a probe confirmed the transaction still committed). **Measured:** 10 purchases committed while the buyer saw an error; replaying each `request_id` returned **that exact ticket**; 100 rows before and after. Without idempotency the only safe retry would be "buy again".

After every fault run all five checks passed, replaying every confirmed and uncertain request matched the final state, and no confirmed purchase was lost. These cover the faults described, not every possible failure.

## 5. The nginx 502 issue (partly unresolved)
On a database error Starlette sends a plain 500 and uvicorn then closes the connection; nginx reuses it, gets a reset, and by default ejects that seller for 10 s. At the kill all three were ejected, extending the outage 3.6–5.3 s past PostgreSQL's recovery (*supported by library source and timing, not a packet capture*). Separately, **spontaneous 502s without seller errors** hit a few no-fault runs; five loss-free packet captures caught none, so **their root cause is not proven**.

## 6. Deliberately not built, and limits
- **No PostgreSQL replica/failover.** Asynchronous failover can lose an acknowledged sale and resell its number (a promoted standby at 57 sells #58 again while buyer A holds #58; only the response-vs-database check catches it). Safe failover needs synchronous replication with a quorum, fenced leader election and primary-only routing, and adds a round trip inside the sale lock — too much for this brief, and on one host it removes no real failure.
- **One host, one database, one volume = one failure domain.** Crash tests killed the PostgreSQL *process*; power-loss / host-crash durability was **not** tested. No Redis or Kubernetes: nothing measured required them.
- **Hot row accepted.** Allocation is serialized on purpose; it governs only the ~1.7 s sale phase, and the observed limit was seller CPU.

## 7. In production I would
Synchronous-replica HA with fencing; readiness checks and 503 + Retry-After for database errors instead of a closed connection; one bounded, idempotent retry of connection failures; multiple hosts; metrics, tracing and alerting.
