"""Sample what the seller's Postgres sessions are doing, to find the bottleneck from
evidence. Every --interval seconds, record each seller session's state/wait event,
ungranted locks by type, and which pids are blocking the others.

Row locks (SELECT ... FOR UPDATE) are NOT rows in pg_locks: they live in the tuple
header. A waiter shows up as an ungranted lock on the holder's `transactionid`
(queued waiters also on a `tuple` lock). pg_blocking_pids() resolves who blocks whom.
"""
import argparse
import asyncio
import json
import time
from collections import Counter

import asyncpg

SESSIONS = """
SELECT pid, state, wait_event_type, wait_event, pg_blocking_pids(pid) AS blockers
FROM pg_stat_activity
WHERE datname = current_database() AND backend_type = 'client backend' AND pid <> pg_backend_pid()
"""
UNGRANTED = "SELECT locktype, count(*) AS n FROM pg_locks WHERE NOT granted GROUP BY locktype"


def category(s):
    if s["wait_event_type"] == "Lock":
        return "waiting_lock"
    if s["state"] == "active":
        return f"active:{s['wait_event_type'] or 'cpu'}:{s['wait_event'] or '-'}"
    return s["state"]            # 'idle', 'idle in transaction', ...


async def sample(args):
    conn = await asyncpg.connect(args.dsn)
    start = time.time()
    with open(args.out, "w") as f:
        while time.time() - start < args.duration:
            sessions = await conn.fetch(SESSIONS)
            ungranted = {r["locktype"]: r["n"] for r in await conn.fetch(UNGRANTED)}
            blocked = {s["pid"]: list(s["blockers"]) for s in sessions if s["blockers"]}
            # Root blockers: pids that block someone but are not themselves blocked.
            roots = sorted({b for bs in blocked.values() for b in bs} - set(blocked))
            f.write(json.dumps({
                "t": round(time.time() - start, 3),
                "categories": Counter(category(s) for s in sessions),
                "holder_state": [category(s) for s in sessions if s["pid"] in roots],
                "ungranted": ungranted,
                "n_blocked": len(blocked),
                "roots": roots,
            }) + "\n")
            f.flush()   # the run script may stop this container early; keep every sample
            await asyncio.sleep(args.interval)
    await conn.close()


def summarize(path):
    samples = [json.loads(line) for line in open(path)]
    busy = [s for s in samples if any(k != "idle" for k in s["categories"])]
    if not busy:
        print("no busy samples"); return
    cats = Counter()
    for s in busy:
        cats.update(s["categories"])
    lock_hist = Counter(s["categories"].get("waiting_lock", 0) for s in busy)
    single_root = sum(1 for s in busy if len(s["roots"]) == 1)
    holder = Counter(h for s in busy for h in s["holder_state"])
    ungr = Counter()
    for s in busy:
        ungr.update(s["ungranted"])

    print(f"samples={len(samples)}  busy samples={len(busy)} (any seller session not idle)")
    print("mean sessions per busy sample, by category:")
    for k, v in cats.most_common():
        print(f"  {k:40} {v / len(busy):6.2f}")
    print("sessions waiting on Lock (count -> % of busy samples):")
    for k in sorted(lock_hist):
        print(f"  {k:3} -> {100 * lock_hist[k] / len(busy):5.1f}%")
    print(f"busy samples with exactly one root blocker: {100 * single_root / len(busy):.1f}%")
    print(f"root blocker's own state (how the lock holder spends its time): {dict(holder.most_common())}")
    print(f"mean ungranted locks by type: { {k: round(v / len(busy), 2) for k, v in ungr.items()} }")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", default="postgresql://tickets:tickets@db:5432/tickets")
    p.add_argument("--interval", type=float, default=0.1)
    p.add_argument("--duration", type=float, default=150)
    p.add_argument("--out", required=True, help="JSONL, one line per sample")
    p.add_argument("--summarize", action="store_true", help="only summarize an existing --out file")
    args = p.parse_args()
    if not args.summarize:
        asyncio.run(sample(args))
    summarize(args.out)


if __name__ == "__main__":
    main()
