"""Load client v2: stampede the seller, then verify the invariants from raw data.

Closed loop (default): --concurrency workers, each sends its next request when the
previous finishes. Measures throughput at a fixed concurrency; latency timer starts
after the request gets its turn, so client-side queueing is excluded.

Open loop (--rate R): request i is sent at start + i/R regardless of completions
(an arrival rate, like "50k people in 60s"). Latency is measured from the SCHEDULED
time, so server queueing is not hidden (avoids coordinated omission). Send lag =
actual send time - scheduled time; large lag means the client, not the server, is
the bottleneck.
"""
import argparse
import asyncio
import json
import math
import random
import time
from collections import Counter, defaultdict

import httpx


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


async def run(args):
    jobs = build_jobs(args.requests, args.dup_fraction, args.dup_copies, args.seed)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    records = []
    in_flight = max_in_flight = 0

    async with httpx.AsyncClient(base_url=args.url, limits=limits, timeout=args.timeout) as client:
        r = await client.post("/reset", json={"tickets": args.tickets})
        r.raise_for_status()

        async def send(i, user_id, request_id, t0, send_lag):
            nonlocal in_flight, max_in_flight
            rec = {"i": i, "user_id": user_id, "request_id": request_id, "send_lag_ms": send_lag * 1000}
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                r = await client.post("/buy", json={"user_id": user_id, "request_id": request_id})
                rec["status"] = r.status_code
                if r.status_code == 200:
                    rec["ticket_number"] = r.json()["ticket_number"]
            except httpx.HTTPError as e:
                rec["status"] = type(e).__name__
            finally:
                in_flight -= 1
            rec["latency_ms"] = (time.perf_counter() - t0) * 1000
            records.append(rec)

        sem = asyncio.Semaphore(args.concurrency)

        async def closed(i, user_id, request_id):
            async with sem:
                await send(i, user_id, request_id, time.perf_counter(), 0.0)

        async def opened(i, user_id, request_id, start):
            scheduled = start + i / args.rate
            await asyncio.sleep(max(0.0, scheduled - time.perf_counter()))
            await send(i, user_id, request_id, scheduled, time.perf_counter() - scheduled)

        start = time.perf_counter()
        if args.rate:
            await asyncio.gather(*(opened(i, u, q, start) for i, (u, q) in enumerate(jobs)))
        else:
            await asyncio.gather(*(closed(i, u, q) for i, (u, q) in enumerate(jobs)))
        storm_s = time.perf_counter() - start   # excludes /reset and /status

        status = (await client.get("/status")).json()

    return {"args": vars(args), "storm_seconds": storm_s, "max_in_flight": max_in_flight,
            "records": records, "status": status}


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
    ambiguous_rows = [u for u in rows_of_user if u not in users_told_200]

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
        "5 responses = state":  (not lost_or_wrong and not told_soldout_but_holds,
                                 f"{len(lost_or_wrong)} confirmed sales missing/wrong in /status; "
                                 f"{len(told_soldout_but_holds)} told sold-out but hold a ticket; "
                                 f"(info) {len(ambiguous_rows)} rows whose buyer never got a 200"),
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
    print(f"throughput: {len(records) / data['storm_seconds']:.1f} req/s over {data['storm_seconds']:.2f}s "
          f"(storm only)   max in-flight: {data['max_in_flight']} (connection cap {args['concurrency']})")
    if args.get("rate") and data["max_in_flight"] >= args["concurrency"]:
        print("  WARNING: connection cap reached; some latency is client-side queueing")

    print("latency ms           n      p50      p99      max")
    groups = [("all", records)] + [(str(c), [r for r in records if r["status"] == c]) for c in sorted(codes, key=str)]
    for name, rs in groups:
        lat = [r["latency_ms"] for r in rs]
        print(f"  {name:12} {len(lat):6} {pct(lat, .5):8.1f} {pct(lat, .99):8.1f} {max(lat):8.1f}")
    if args.get("rate"):
        lag = [r["send_lag_ms"] for r in records]
        print(f"  send lag     {len(lag):6} {pct(lag, .5):8.1f} {pct(lag, .99):8.1f} {max(lag):8.1f}")

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
    p.add_argument("--dup-fraction", type=float, default=0.2)
    p.add_argument("--dup-copies", type=int, default=2)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", help="write raw results JSON to this path")
    p.add_argument("--recheck", help="re-verify a saved --out file instead of running")
    args = p.parse_args()

    if args.recheck:
        with open(args.recheck) as f:
            data = json.load(f)
    else:
        data = asyncio.run(run(args))
        if args.out:
            # Raw evidence: every verdict and percentile can be re-derived from this file.
            with open(args.out, "w") as f:
                json.dump(data, f)
    raise SystemExit(0 if report(data) else 1)


if __name__ == "__main__":
    main()
