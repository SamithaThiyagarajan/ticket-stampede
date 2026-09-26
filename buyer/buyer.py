"""Load client v2: stampede the seller, then verify the invariants from raw data.

Closed loop (default): --concurrency workers, each sends its next request when the
previous finishes. Measures throughput at a fixed concurrency; latency timer starts
after the request gets its turn, so client-side queueing is excluded.

Open loop (--rate R): request i arrives at start + i/R regardless of completions
(an arrival rate, like "50k people in 60s"). Latency is measured from the SCHEDULED
time, so server queueing is not hidden (avoids coordinated omission). At most
--concurrency requests are handed to httpx; later arrivals wait in our own queue
(httpcore's pool rescans every queued request per event, which stalled the client at
50k — see DECISIONS_LOG). Per request:
  sched_lag  = how late the client woke for the arrival (client trouble)
  queue_wait = time waiting for an in-flight slot (server backpressure)
  send_lag   = sched_lag + queue_wait
A request that waits longer than --timeout for a slot gives up as QueueTimeout.
"""
import argparse
import asyncio
import gzip
import json
import math
import multiprocessing
import random
import time
from collections import Counter, defaultdict

import httpx

# Transport failures split by whether the server could have received the request.
# Never sent: failed before the request left the client, so the server cannot have acted on it.
NEVER_SENT = {"QueueTimeout", "PoolTimeout", "ConnectError", "ConnectTimeout"}
# Anything else non-HTTP (ReadTimeout, ReadError, RemoteProtocolError, ...) = sent, no
# answer: the server may have committed a sale the buyer never heard about.


def build_jobs(n_requests, dup_fraction, dup_copies, seed):
    """Return a send order of (user_id, request_id). Duplicate copies sit adjacent
    so they are in flight at the same time (the retry-while-original-pending case)."""
    rng = random.Random(seed)
    jobs, i = [], 0
    while len(jobs) < n_requests:
        user_id, request_id = f"user-{i}", f"req-{i}"
        copies = dup_copies if rng.random() < dup_fraction else 1
        jobs.extend([(user_id, request_id)] * copies)
        i += 1
    return jobs[:n_requests]


def storm_worker(args, k, n_workers, start_at):
    """Process entry point: worker k sends arrivals i with i % n_workers == k."""
    return asyncio.run(storm(args, k, n_workers, start_at))


async def storm(args, k, n_workers, start_at):
    # Every worker builds the same job list from the same seed and keeps its slice,
    # still scheduled at start + i/rate, so the combined schedule is unchanged.
    jobs = [(i, u, q) for i, (u, q) in enumerate(build_jobs(args.requests, args.dup_fraction,
                                                            args.dup_copies, args.seed))
            if i % n_workers == k]
    cap = math.ceil(args.concurrency / n_workers)          # this worker's share of the connections
    limits = httpx.Limits(max_connections=cap, max_keepalive_connections=cap)
    records, timeline = [], []
    in_flight = max_in_flight = queued = 0
    # start_at is wall-clock (shared across processes); convert once to this process's perf_counter.
    start = time.perf_counter() + (start_at - time.time())

    async def monitor():
        # This worker's own CPU (process_time covers all its threads) per second: proves
        # or disproves "the client is the bottleneck" with a timeline, not an average.
        t_prev, cpu_prev = time.perf_counter(), time.process_time()
        while True:
            await asyncio.sleep(1)
            t, cpu = time.perf_counter(), time.process_time()
            timeline.append({"worker": k, "t": round(t - start, 1),
                             "cpu_pct": round(100 * (cpu - cpu_prev) / (t - t_prev), 1),
                             "in_flight": in_flight, "queued": queued, "done": len(records)})
            t_prev, cpu_prev = t, cpu

    async with httpx.AsyncClient(base_url=args.url, limits=limits, timeout=args.timeout) as client:

        async def send(i, user_id, request_id, t0, sched_lag=0.0, queue_wait=0.0):
            nonlocal in_flight, max_in_flight
            rec = {"worker": k, "i": i, "user_id": user_id, "request_id": request_id,
                   "sched_lag_ms": sched_lag * 1000, "queue_wait_ms": queue_wait * 1000,
                   "send_lag_ms": (sched_lag + queue_wait) * 1000}
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                r = await client.post("/buy", json={"user_id": user_id, "request_id": request_id})
                rec["status"] = r.status_code
                if "x-upstream" in r.headers:          # set by the nginx load balancer
                    rec["upstream"] = r.headers["x-upstream"]
                if r.status_code == 200:
                    rec["ticket_number"] = r.json()["ticket_number"]
            except httpx.HTTPError as e:
                rec["status"] = type(e).__name__
            finally:
                in_flight -= 1
            rec["latency_ms"] = (time.perf_counter() - t0) * 1000
            records.append(rec)

        sem = asyncio.Semaphore(cap)

        async def closed(i, user_id, request_id):
            async with sem:
                await send(i, user_id, request_id, time.perf_counter())

        async def opened(i, user_id, request_id):
            nonlocal queued
            scheduled = start + i / args.rate
            await asyncio.sleep(max(0.0, scheduled - time.perf_counter()))
            woke = time.perf_counter()
            queued += 1
            try:
                await asyncio.wait_for(sem.acquire(), timeout=args.timeout)
            except asyncio.TimeoutError:
                records.append({"worker": k, "i": i, "user_id": user_id, "request_id": request_id,
                                "status": "QueueTimeout",
                                "sched_lag_ms": (woke - scheduled) * 1000,
                                "queue_wait_ms": (time.perf_counter() - woke) * 1000,
                                "send_lag_ms": (time.perf_counter() - scheduled) * 1000,
                                "latency_ms": (time.perf_counter() - scheduled) * 1000})
                return
            finally:
                queued -= 1
            try:
                await send(i, user_id, request_id, scheduled, woke - scheduled, time.perf_counter() - woke)
            finally:
                sem.release()

        await asyncio.sleep(max(0.0, start - time.perf_counter()))   # common start line
        mon = asyncio.create_task(monitor())
        if args.rate:
            await asyncio.gather(*(opened(i, u, q) for i, u, q in jobs))
        else:
            await asyncio.gather(*(closed(i, u, q) for i, u, q in jobs))
        end_at = start_at + (time.perf_counter() - start)
        mon.cancel()

    return {"records": records, "timeline": timeline, "max_in_flight": max_in_flight, "end_at": end_at}


def run(args):
    """Main process: reset once, run the workers from a common start time, merge,
    then read /status once for verification."""
    with httpx.Client(base_url=args.url, timeout=args.timeout) as c:
        c.post("/reset", json={"tickets": args.tickets}).raise_for_status()
    start_at = time.time() + 3.0          # headroom for worker processes to spawn and import
    n = args.processes
    if n == 1:
        parts = [storm_worker(args, 0, 1, start_at)]
    else:
        # spawn, not fork: each worker starts clean, with no inherited event-loop state.
        with multiprocessing.get_context("spawn").Pool(n) as pool:
            parts = pool.starmap(storm_worker, [(args, k, n, start_at) for k in range(n)])
    storm_s = max(p["end_at"] for p in parts) - start_at   # excludes /reset and /status
    with httpx.Client(base_url=args.url, timeout=args.timeout) as c:
        status = c.get("/status").json()

    return {"args": vars(args), "storm_seconds": storm_s,
            "max_in_flight": sum(p["max_in_flight"] for p in parts),   # upper bound when n > 1
            "max_in_flight_per_worker": [p["max_in_flight"] for p in parts],
            "timeline": [s for p in parts for s in p["timeline"]],
            "records": [r for p in parts for r in p["records"]], "status": status}


def pct(xs, p):
    """Nearest-rank percentile."""
    xs = sorted(xs)
    return xs[max(0, math.ceil(p * len(xs)) - 1)]


def verify(data):
    """Recompute every invariant from raw records + /status. Returns (results, unexpected)."""
    records, status = data["records"], data["status"]
    total, sold, tickets = status["total_tickets"], status["sold"], status["tickets"]

    # Checks 1-4: unchanged from v1.
    numbers = [t["ticket_number"] for t in tickets]
    dup_numbers = {n: c for n, c in Counter(numbers).items() if c > 1}
    out_of_range = [n for n in numbers if not 1 <= n <= total]
    users_multi = {u: c for u, c in Counter(t["user_id"] for t in tickets).items() if c > 1}
    told = defaultdict(set)                       # request_id -> ticket numbers told (200s)
    for r in records:
        if r["status"] == 200:
            told[r["request_id"]].add(r["ticket_number"])
    inconsistent = {q for q, ns in told.items() if len(ns) > 1}

    # Check 5: client responses vs final durable state.
    # /status has no request_id; the mapping relies on the load generator using
    # exactly one user_id per request_id.
    user_of = {r["request_id"]: r["user_id"] for r in records}
    holders = defaultdict(set)                    # ticket_number -> user_ids holding it
    rows_of_user = defaultdict(list)
    for t in tickets:
        holders[t["ticket_number"]].add(t["user_id"])
        rows_of_user[t["user_id"]].append(t["ticket_number"])
    lost_or_wrong = [(q, n) for q, ns in told.items() for n in ns if user_of[q] not in holders.get(n, ())]
    got_409 = {r["request_id"] for r in records if r["status"] == 409}
    told_soldout_but_holds = [q for q in got_409 if rows_of_user.get(user_of[q])]
    users_told_200 = {user_of[q] for q in told}
    outcomes = defaultdict(set)                   # user_id -> statuses of all its copies
    for r in records:
        outcomes[r["user_id"]].add(r["status"])
    # Rows whose buyer never got a 200: either the server committed after the buyer
    # gave up (late-confirmed, info), or every copy was never sent (impossible -> FAIL).
    unconfirmed = [u for u in rows_of_user if u not in users_told_200 and 409 not in outcomes[u]]
    never_sent_rows = [u for u in unconfirmed if outcomes[u] <= NEVER_SENT]
    late_confirmed = [u for u in unconfirmed if not outcomes[u] <= NEVER_SENT]

    results = {
        "1 no oversell":        (len(tickets) <= total,
                                 f"{len(tickets)} tickets issued for {total} available"),
        "2 no duplicate ticket": (not dup_numbers and not out_of_range,
                                 f"{len(dup_numbers)} ticket numbers issued >1 time "
                                 f"({sum(dup_numbers.values())} rows); {len(out_of_range)} out of range"),
        "3 idempotency":        (not users_multi and not inconsistent,
                                 f"{len(users_multi)} request_ids hold >1 ticket (server rows); "
                                 f"{len(inconsistent)} request_ids told different numbers (responses)"),
        "4 no drift":           (sold == len(tickets),
                                 f"/status sold={sold}, actual rows={len(tickets)}"),
        "5 responses = state":  (not lost_or_wrong and not told_soldout_but_holds and not never_sent_rows,
                                 f"{len(lost_or_wrong)} confirmed sales missing/wrong in /status; "
                                 f"{len(told_soldout_but_holds)} told sold-out but hold a ticket; "
                                 f"{len(never_sent_rows)} rows for never-sent requests; "
                                 f"(info) {len(late_confirmed)} late-confirmed sales (sent, no answer, but committed)"),
    }
    unexpected = sum(1 for r in records if r["status"] not in (200, 409))
    return results, unexpected


def report(data):
    args, records = data["args"], data["records"]
    results, unexpected = verify(data)
    codes = Counter(r["status"] for r in records)
    n_dup_ids = sum(1 for c in Counter(r["request_id"] for r in records).values() if c > 1)
    mode = f"open loop @ {args['rate']}/s" if args.get("rate") else f"closed loop, concurrency={args['concurrency']}"

    print(f"\nrequests={len(records)}  {mode}  tickets={data['status']['total_tickets']}  "
          f"request_ids sent >1 time={n_dup_ids}")
    print(f"responses: {dict(codes)}")
    transport = [r["status"] for r in records if not isinstance(r["status"], int)]
    if transport:
        never = sum(1 for s in transport if s in NEVER_SENT)
        print(f"transport failures: never sent={never}  sent, no answer={len(transport) - never}")
    per_worker = data.get("max_in_flight_per_worker")
    inflight = (f"{data['max_in_flight']} (sum of per-worker maxima {per_worker})" if per_worker and len(per_worker) > 1
                else str(data["max_in_flight"]))
    print(f"throughput: {len(records) / data['storm_seconds']:.1f} req/s over {data['storm_seconds']:.2f}s "
          f"(storm only)   max in-flight: {inflight} (connection cap {args['concurrency']})")
    if args.get("rate"):
        # Actual arrival time = scheduled + sched_lag: the rate the client really delivered.
        arr = sorted(r["i"] / args["rate"] + r["sched_lag_ms"] / 1000 for r in records if "sched_lag_ms" in r)
        if len(arr) > 1:
            print(f"arrivals delivered: {(len(arr) - 1) / (arr[-1] - arr[0]):.1f}/s over {arr[-1] - arr[0]:.2f}s "
                  f"(target {args['rate']}/s over {(len(arr) - 1) / args['rate']:.2f}s)")
        # Throughput above counts client-side timeouts as completions; this counts only
        # requests the server answered, over first send -> last answer.
        served = [r for r in records if isinstance(r["status"], int)]
        if len(served) > 1:
            sent = min(r["i"] / args["rate"] + r["send_lag_ms"] / 1000 for r in served)
            done = max(r["i"] / args["rate"] + r["latency_ms"] / 1000 for r in served)
            print(f"served by server: {len(served)} responses, {len(served) / (done - sent):.1f}/s "
                  f"over {done - sent:.2f}s (first send -> last answer)")
        if data["max_in_flight"] >= args["concurrency"]:
            print("  NOTE: in-flight cap reached; arrivals queued client-side (see queue_wait) — server backpressure")
    tl = data.get("timeline") or []
    if tl:
        by_worker = defaultdict(list)
        for s in tl:
            by_worker[s.get("worker", 0)].append(s["cpu_pct"])
        print(f"buyer CPU per 1s sample, per worker process (max queued in any worker: {max(s['queued'] for s in tl)}):")
        for w, cpu in sorted(by_worker.items()):
            print(f"  worker {w}: p50={pct(cpu, .5):4.0f}%  max={max(cpu):4.0f}%  seconds >= 90%: {sum(c >= 90 for c in cpu)} of {len(cpu)}")

    print("latency ms           n      p50      p99      max")
    groups = [("all", records)] + [(str(c), [r for r in records if r["status"] == c]) for c in sorted(codes, key=str)]
    for name, rs in groups:
        lat = [r["latency_ms"] for r in rs]
        print(f"  {name:12} {len(lat):6} {pct(lat, .5):8.1f} {pct(lat, .99):8.1f} {max(lat):8.1f}")
    if args.get("rate"):
        for key, label in (("sched_lag_ms", "sched lag"), ("queue_wait_ms", "queue wait"), ("send_lag_ms", "send lag")):
            lag = [r[key] for r in records if key in r]
            if lag:
                print(f"  {label:12} {len(lag):6} {pct(lag, .5):8.1f} {pct(lag, .99):8.1f} {max(lag):8.1f}")

    print()
    for name, (ok, detail) in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:24} {detail}")

    # Errors can hide a violation but cannot create one (violations are measured from
    # server rows), so any failed invariant is FAIL; errors only block a PASS.
    if not all(ok for ok, _ in results.values()):
        verdict = "FAIL"
    elif unexpected:
        verdict = "INCONCLUSIVE (invariants held, but unexpected errors)"
    else:
        verdict = "PASS"
    print(f"\nOVERALL: {verdict}")
    return verdict == "PASS"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--tickets", type=int, default=100)
    p.add_argument("--requests", type=int, default=1000)
    p.add_argument("--concurrency", type=int, default=200,
                   help="closed loop: worker count; both modes: HTTP connection cap")
    p.add_argument("--rate", type=float, help="open loop: arrivals per second")
    p.add_argument("--processes", type=int, default=1,
                   help="worker processes; arrivals split by i %% processes, connections split evenly")
    p.add_argument("--dup-fraction", type=float, default=0.2)
    p.add_argument("--dup-copies", type=int, default=2)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", help="write raw results JSON to this path")
    p.add_argument("--recheck", help="re-verify a saved --out file instead of running")
    args = p.parse_args()

    opener = lambda path, mode: (gzip.open if path.endswith(".gz") else open)(path, mode + "t")
    if args.recheck:
        with opener(args.recheck, "r") as f:
            data = json.load(f)
    else:
        data = run(args)
        if args.out:
            # Raw evidence: every verdict and percentile can be re-derived from this file.
            with opener(args.out, "w") as f:
                json.dump(data, f)
    raise SystemExit(0 if report(data) else 1)


if __name__ == "__main__":
    main()
