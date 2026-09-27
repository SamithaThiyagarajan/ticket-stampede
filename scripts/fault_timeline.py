"""Timeline of a fault-injection run, relative to the buyer's shared t=0.
Usage: python scripts/fault_timeline.py NAME
Reads results/NAME.{json.gz,fault.log,fault.log.events,pgsample.jsonl,nginx.log,sellers.log,db.log}."""
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "buyer"))
from buyer import NEVER_SENT, pct  # noqa: E402

R = Path(__file__).parent.parent / "results"


def cls(s):
    if s == 200: return "200"
    if s == 409: return "409"
    if isinstance(s, int): return str(s)
    return "never-sent" if s in NEVER_SENT else "sent-no-answer"


def main(name):
    d = json.load(gzip.open(R / f"{name}.json.gz", "rt"))
    rate, t0, recs = d["args"]["rate"], d["start_at"], d["records"]
    rel = lambda epoch: epoch - t0

    print("=== fault timeline (t relative to buyer start) ===")
    marks = {}
    for line in (R / f"{name}.fault.log").read_text().splitlines():
        ts, msg = line.split(" ", 1)
        print(f"  t={rel(float(ts)):8.3f}s  {msg}")
        for key in ("inject:", "restore:", "postgres accepting"):
            if msg.startswith(key) or (key == "postgres accepting" and key in msg):
                marks.setdefault(key + (" issued" if "issued" in msg else " returned" if "returned" in msg else ""), rel(float(ts)))
    ev = R / f"{name}.fault.log.events"
    if ev.exists():
        for line in ev.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and not parts[2].startswith(("exec_", "health_status")):
                print(f"  t={rel(int(parts[0]) / 1e9):8.3f}s  docker event: {' '.join(parts[2:])}")

    # Outcomes by the time the buyer got its answer (or gave up), per second.
    done = lambda r: r["i"] / rate + r["latency_ms"] / 1000
    per_sec = defaultdict(Counter)
    for r in recs:
        per_sec[int(done(r))][cls(r["status"])] += 1
    kinds = ["200", "409", "500", "502", "503", "504", "sent-no-answer", "never-sent"]
    kinds = [k for k in kinds if any(c[k] for c in per_sec.values())]
    print("\n=== outcomes per second, by answer time ===\n  t(s)  " + "".join(f"{k:>15}" for k in kinds))
    for t in sorted(per_sec):
        if t <= 20 or any(per_sec[t][k] for k in kinds if k not in ("200", "409")):
            print(f"  {t:4}  " + "".join(f"{per_sec[t][k]:15}" for k in kinds))

    print("\n=== outcome totals ===")
    tot = Counter(r["status"] for r in recs)
    print(f"  {dict(tot)}")
    errs = [r for r in recs if r["status"] not in (200, 409)]
    if errs:
        e = sorted(done(r) for r in errs)
        print(f"  non-200/409 outcomes: {len(errs)}, first at t={e[0]:.3f}s, last at t={e[-1]:.3f}s")
        by_up = Counter((r.get("upstream"), r["status"]) for r in errs if isinstance(r["status"], int))
        print(f"  5xx by upstream: {dict(by_up)}")
    ok_after = defaultdict(lambda: None)
    restore_t = marks.get("postgres accepting")
    if restore_t is not None:
        for r in sorted(recs, key=done):
            if r["status"] in (200, 409) and done(r) >= restore_t and ok_after[r.get("upstream")] is None:
                ok_after[r.get("upstream")] = done(r)
        print(f"  first 200/409 answer per upstream after Postgres accepted connections (t={restore_t:.3f}s): "
              + ", ".join(f"{u}: t={t:.3f}s" for u, t in sorted(ok_after.items(), key=lambda x: str(x[0]))))

    print("\n=== latency by arrival window (ms, answered requests) ===")
    win = defaultdict(list)
    for r in recs:
        if isinstance(r["status"], int):
            win[int(r["i"] / rate) // 5 * 5].append(r["latency_ms"])
    for t, v in sorted(win.items()):
        print(f"  arrivals t={t:2}-{t + 5}s: n={len(v):5}  p50 {pct(v, .5):9.1f}  p99 {pct(v, .99):9.1f}  max {max(v):9.1f}")
    qw = [r["queue_wait_ms"] for r in recs if "queue_wait_ms" in r]
    print(f"  queue wait (client-side backpressure): p50 {pct(qw, .5):.1f}  p99 {pct(qw, .99):.1f}  max {max(qw):.1f} ms; "
          f"QueueTimeouts: {tot.get('QueueTimeout', 0)}")
    ok = sorted(done(r) for r in recs if r["status"] == 200)
    print(f"  100th 200-answer at t={ok[99]:.3f}s" if len(ok) >= 100 else f"  only {len(ok)} 200-answers")

    samples = [json.loads(l) for l in open(R / f"{name}.pgsample.jsonl")]
    down = [s for s in samples if s.get("unreachable")]
    print(f"\n=== pg sampler: {len(down)} unreachable samples"
          + (f", t={down[0]['t']:.1f}..{down[-1]['t']:.1f}s (sampler clock), errors {dict(Counter(s['error'] for s in down))}" if down else ""))

    print("\n=== seller logs: exception types ===")
    sl = (R / f"{name}.sellers.log").read_text()
    exc = Counter(re.findall(r"^(?:\S+\s+\|\s+)?(\w+(?:\.\w+)*(?:Error|Exception)): ", sl, re.M))
    print(f"  {dict(exc) if exc else 'none'}")
    ng = (R / f"{name}.nginx.log").read_text().strip()
    print(f"\n=== nginx error log: {len(ng.splitlines()) if ng else 0} line(s) ===")
    for l in ng.splitlines()[:10]:
        print("  " + l[:220])
    print("\n=== Postgres log (this run) ===")
    for l in (R / f"{name}.db.log").read_text().splitlines():
        if not re.search(r"checkpoint (starting|complete)", l):
            print("  " + l[:200])


if __name__ == "__main__":
    main(sys.argv[1])
