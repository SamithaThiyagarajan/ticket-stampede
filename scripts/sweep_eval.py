"""Evaluate pool-sweep runs against criteria fixed before the runs (DECISIONS_LOG).
Usage: python scripts/sweep_eval.py POOL [POOL ...]   (reads results/m7_sweepv2_poolP.*)"""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "buyer"))
from buyer import NEVER_SENT, pct  # noqa: E402

R = Path(__file__).parent.parent / "results"


def evaluate(pool):
    base = R / f"m7_sweepv2_pool{pool}"
    d = json.load(gzip.open(f"{base}.json.gz", "rt"))
    env = (R / f"m7_sweepv2_pool{pool}.env").read_text()
    rate, recs = d["args"]["rate"], d["records"]

    arr = sorted(r["i"] / rate + r["sched_lag_ms"] / 1000 for r in recs)
    delivered = (len(arr) - 1) / (arr[-1] - arr[0])
    sched_p99 = pct([r["sched_lag_ms"] for r in recs], .99)
    hot = defaultdict(int)
    for s in d["timeline"]:
        hot[s["worker"]] += s["cpu_pct"] >= 90
    sent_no_answer = sum(1 for r in recs if not isinstance(r["status"], int) and r["status"] not in NEVER_SENT)
    served = [r for r in recs if isinstance(r["status"], int)]
    t0 = min(r["i"] / rate + r["send_lag_ms"] / 1000 for r in served)
    t1 = max(r["i"] / rate + r["latency_ms"] / 1000 for r in served)

    samples = [json.loads(l) for l in open(f"{base}.pgsample.jsonl")]
    busy = [s for s in samples if any(k != "idle" for k in s["categories"])]
    waiters = sum(s["categories"].get("waiting_lock", 0) for s in busy) / len(busy)
    one_root = sum(len(s["roots"]) == 1 for s in busy) / len(busy)

    checks = {
        f"pool={pool} recorded": f"safe 0 {pool} " in env,
        "seller unchanged": "seller container unchanged" in env,
        "arrivals +-2%": abs(delivered - rate) / rate <= 0.02,
        "sched p99 <100ms": sched_p99 < 100,
        "worker >=90% <=3s": max(hot.values()) <= 3,
        "0 sent-no-answer": sent_no_answer == 0,
    }
    return {"pool": pool, "served_per_s": len(served) / (t1 - t0), "served": len(served),
            "delivered": delivered, "sched_p99": sched_p99, "max_hot_s": max(hot.values()),
            "sent_no_answer": sent_no_answer, "waiters": waiters, "one_root": one_root,
            "valid": all(checks.values()), "failed": [k for k, ok in checks.items() if not ok]}


rows = [evaluate(int(p)) for p in sys.argv[1:]]
ref = next((r for r in rows if r["pool"] == 10 and r["valid"]), None)
print("pool  served/s  vs pool10   served  arrivals/s  sched p99  max worker s>=90%  sent-no-ans  lock waiters  one root  valid")
for r in rows:
    vs = f"{100 * (r['served_per_s'] / ref['served_per_s'] - 1):+6.1f}%" if ref else "   n/a "
    print(f"{r['pool']:4}  {r['served_per_s']:8.1f}  {vs:>9}  {r['served']:7}  {r['delivered']:10.1f}  {r['sched_p99']:7.1f}ms"
          f"  {r['max_hot_s']:17}  {r['sent_no_answer']:11}  {r['waiters']:12.2f}  {100 * r['one_root']:7.1f}%  "
          f"{'VALID' if r['valid'] else 'INVALID ' + str(r['failed'])}")
if ref:
    material = [r["pool"] for r in rows if r["valid"] and r["pool"] != 10 and r["served_per_s"] > 1.15 * ref["served_per_s"]]
    print(f"\nthreshold: >15% over pool 10 ({1.15 * ref['served_per_s']:.1f}/s). "
          f"Valid pools exceeding it: {material or 'none'}")
