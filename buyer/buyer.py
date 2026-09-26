"""Load client v1: stampede the seller, then check the four invariants."""
import argparse
import asyncio
import random
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
    responses = defaultdict(list)          # request_id -> [ticket_number, ...] from 200s
    codes, errors = Counter(), Counter()
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(base_url=args.url, limits=limits, timeout=args.timeout) as client:
        r = await client.post("/reset", json={"tickets": args.tickets})
        r.raise_for_status()

        async def buy(user_id, request_id):
            async with sem:
                try:
                    r = await client.post("/buy", json={"user_id": user_id, "request_id": request_id})
                except httpx.HTTPError as e:
                    errors[type(e).__name__] += 1
                    return
                codes[r.status_code] += 1
                if r.status_code == 200:
                    responses[request_id].append(r.json()["ticket_number"])

        await asyncio.gather(*(buy(u, q) for u, q in jobs))
        status = (await client.get("/status")).json()

    return jobs, responses, codes, errors, status


def check(args, jobs, responses, codes, errors, status):
    total, sold, tickets = status["total_tickets"], status["sold"], status["tickets"]
    numbers = [t["ticket_number"] for t in tickets]
    dup_numbers = {n: c for n, c in Counter(numbers).items() if c > 1}
    out_of_range = [n for n in numbers if not 1 <= n <= total]
    users_multi = {u: c for u, c in Counter(t["user_id"] for t in tickets).items() if c > 1}
    inconsistent = {q: ns for q, ns in responses.items() if len(set(ns)) > 1}

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
    }

    n_dup_ids = sum(1 for c in Counter(q for _, q in jobs).values() if c > 1)
    print(f"\nrequests={len(jobs)}  concurrency={args.concurrency}  tickets={total}  "
          f"request_ids sent >1 time={n_dup_ids}")
    print(f"HTTP codes: {dict(codes)}   transport errors: {dict(errors) or 0}")
    unexpected = sum(c for code, c in codes.items() if code not in (200, 409)) + sum(errors.values())
    print()
    for name, (ok, detail) in results.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:24} {detail}")

    if unexpected:
        verdict = "INCONCLUSIVE (unexpected errors)"
    else:
        verdict = "PASS" if all(ok for ok, _ in results.values()) else "FAIL"
    print(f"\nOVERALL: {verdict}")
    if args.verbose and dup_numbers:
        print("duplicate ticket numbers (number: rows):", dict(sorted(dup_numbers.items())[:20]))
    return verdict == "PASS"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--tickets", type=int, default=100)
    p.add_argument("--requests", type=int, default=1000)
    p.add_argument("--concurrency", type=int, default=200)
    p.add_argument("--dup-fraction", type=float, default=0.2)
    p.add_argument("--dup-copies", type=int, default=2)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    ok = check(args, *asyncio.run(run(args)))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
