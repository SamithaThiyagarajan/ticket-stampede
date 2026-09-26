"""Evaluate open-loop runs against the pre-registered client-clean and keeps-up criteria.
Usage: python scripts/run_eval.py NAME [NAME ...]   (reads results/NAME.*)"""
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "buyer"))
from buyer import NEVER_SENT, pct, verify  # noqa: E402

R = Path(__file__).parent.parent / "results"

for name in sys.argv[1:]:
    d = json.load(gzip.open(R / f"{name}.json.gz", "rt"))
    env = (R / f"{name}.env").read_text().splitlines()[0]
    rate, recs = d["args"]["rate"], d["records"]
    arr = sorted(r["i"] / rate + r["sched_lag_ms"] / 1000 for r in recs)
    delivered = (len(arr) - 1) / (arr[-1] - arr[0])
    hot = defaultdict(int)
    for s in d["timeline"]:
        hot[s["worker"]] += s["cpu_pct"] >= 90
    sna = sum(1 for r in recs if not isinstance(r["status"], int) and r["status"] not in NEVER_SENT)
    qto = sum(1 for r in recs if r["status"] == "QueueTimeout")
    served = [r for r in recs if isinstance(r["status"], int)]
    t0 = min(r["i"] / rate + r["send_lag_ms"] / 1000 for r in served)
    t1 = max(r["i"] / rate + r["latency_ms"] / 1000 for r in served)
    lat = lambda rs: f"p50 {pct([r['latency_ms'] for r in rs], .5):8.1f}  p99 {pct([r['latency_ms'] for r in rs], .99):8.1f}"
    results, _ = verify(d)

    samples = [json.loads(l) for l in open(R / f"{name}.pgsample.jsonl")]
    busy = [s for s in samples if any(k != "idle" for k in s["categories"])]
    waiters = sum(s["categories"].get("waiting_lock", 0) for s in busy) / max(1, len(busy))
    cpu = defaultdict(list)
    for line in list(open(R / f"{name}.dockerstats.csv"))[1:]:
        _, cname, c, _ = line.strip().split(",", 3)
        cpu[cname].append(float(c.rstrip("%")))
    sellers = {k: v for k, v in sorted(cpu.items()) if "seller" in k or "lb-1" in k}   # all instances + nginx
    db = next(v for k, v in cpu.items() if "db-1" in k)

    clean = {"arrivals +-2%": abs(delivered - rate) / rate <= .02,
             "sched p99 <100ms": pct([r["sched_lag_ms"] for r in recs], .99) < 100,
             "worker >=90% <=3s": max(hot.values()) <= 3,
             "0 sent-no-answer": sna == 0,
             # bench.sh writes "seller container unchanged", bench_cluster.sh "all containers unchanged"
             "seller unchanged": "container unchanged" in (R / f"{name}.env").read_text()
                                 or "containers unchanged" in (R / f"{name}.env").read_text()}
    served_rate = len(served) / (t1 - t0)
    keeps_up = qto == 0 and served_rate >= 0.97 * rate

    print(f"\n=== {name}   [{env}]")
    print(f"  arrivals delivered {delivered:.1f}/s   sched lag p99 {pct([r['sched_lag_ms'] for r in recs], .99):.1f}ms   "
          f"max worker s>=90%: {max(hot.values())}   sent-no-answer: {sna}")
    print(f"  CLIENT: {'CLEAN' if all(clean.values()) else 'NOT CLEAN ' + str([k for k, v in clean.items() if not v])}")
    print(f"  served {len(served)} of {len(recs)} = {served_rate:.1f}/s   QueueTimeouts {qto}   "
          f"-> {'KEEPS UP' if keeps_up else 'FALLS BEHIND'} (needs 0 QueueTimeouts and >= {0.97 * rate:.0f}/s)")
    for code in (200, 409):
        rs = [r for r in recs if r["status"] == code]
        print(f"  latency ms {code}: n={len(rs):6}  {lat(rs)}")
    print(f"  latency ms all served: {lat(served)}")
    for k, v in sellers.items():
        print(f"  {k.replace('ticket_stampede-', ''):10} CPU mean {sum(v)/len(v):4.0f}% max {max(v):4.0f}%")
    print(f"  db         CPU mean {sum(db)/len(db):4.0f}% max {max(db):4.0f}%")
    buyer_cpu = [c for k, v in cpu.items() if "buyer-run" in k for c in v]
    if buyer_cpu:
        print(f"  buyer container CPU mean {sum(buyer_cpu)/len(buyer_cpu):.0f}% max {max(buyer_cpu):.0f}%")
    routed = [r for r in recs if "upstream" in r]
    if routed:
        # Per-request routing from nginx's X-Upstream header: distribution, and whether
        # copies of one request_id were served by different instances yet got one answer.
        print("  routing (X-Upstream): " + "  ".join(f"{u}={n}" for u, n in sorted(Counter(r["upstream"] for r in routed).items())))
        by_rid = defaultdict(list)
        for r in routed:
            by_rid[r["request_id"]].append(r)
        multi = [rs for rs in by_rid.values() if len({r["upstream"] for r in rs}) > 1]
        multi_200 = [rs for rs in multi if any(r["status"] == 200 for r in rs)]
        same = sum(1 for rs in multi_200 if len({r.get("ticket_number") for r in rs if r["status"] == 200}) == 1
                   and all(r["status"] == 200 for r in rs))
        print(f"  request_ids whose copies hit >1 instance: {len(multi)}; of those holding a ticket: {len(multi_200)}, "
              f"all copies answered 200 with the same ticket: {same}")
    print(f"  pg: busy samples {len(busy)}/{len(samples)}, mean lock waiters (busy) {waiters:.2f}")
    print("  correctness: " + "  ".join(f"{k.split()[0]}:{'PASS' if ok else 'FAIL'}" for k, (ok, _) in results.items()))
