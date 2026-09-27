"""Evaluate arrival-rate sweep runs against the pre-registered criteria (DECISIONS_LOG, M12 sweep).
Usage: python scripts/sweep_rate_eval.py NAME [NAME ...]"""
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "buyer"))
from buyer import NEVER_SENT, pct, verify  # noqa: E402

R = Path(__file__).parent.parent / "results"


def cpu_by_container(name):
    by, host = defaultdict(list), defaultdict(float)
    for line in list(open(R / f"{name}.dockerstats.csv"))[1:]:
        t, cname, c, _ = line.strip().split(",", 3)
        v = float(c.rstrip("%"))
        by[cname.replace("ticket_stampede-", "").split("-run-")[0]].append(v)
        host[t] += v
    return by, list(host.values())


def compute(name):
    """The pre-registered validity/sustainability evaluation, returned as data (also used by
    scripts/build_viewer.py, so the viewer shows exactly the verdicts this script reports)."""
    d = json.load(gzip.open(R / f"{name}.json.gz", "rt"))
    rate, recs = d["args"]["rate"], d["records"]
    arr = sorted(r["i"] / rate + r["sched_lag_ms"] / 1000 for r in recs)
    delivered = (len(arr) - 1) / (arr[-1] - arr[0])
    sched99 = pct([r["sched_lag_ms"] for r in recs], .99)
    hot = defaultdict(int)
    for s in d["timeline"]:
        hot[s["worker"]] += s["cpu_pct"] >= 90
    st = Counter(r["status"] for r in recs)
    sna = sum(v for k, v in st.items() if not isinstance(k, int) and k not in NEVER_SENT)
    qto = st.get("QueueTimeout", 0)
    e5 = sum(v for k, v in st.items() if isinstance(k, int) and k >= 500)
    served = [r for r in recs if isinstance(r["status"], int)]
    t0 = min(r["i"] / rate + r["send_lag_ms"] / 1000 for r in served)
    t1 = max(r["i"] / rate + r["latency_ms"] / 1000 for r in served)
    srate = len(served) / (t1 - t0)
    post = [r["latency_ms"] for r in served if r["i"] / rate >= 5]
    by, host = cpu_by_container(name)
    env = (R / f"{name}.env").read_text()
    samples = [json.loads(l) for l in open(R / f"{name}.pgsample.jsonl")]
    busy = [s for s in samples if not s.get("unreachable") and any(k != "idle" for k in s["categories"])]
    waiters = [s["categories"].get("waiting_lock", 0) for s in busy]
    valid = {"arrivals +-2%": abs(delivered - rate) / rate <= .02, "sched p99<100ms": sched99 < 100,
             "worker>=90% <=3s": max(hot.values()) <= 3, "0 sent-no-answer": sna == 0,
             # bench_cluster.sh writes "all containers unchanged", bench.sh "seller container unchanged"
             "containers unchanged": "containers unchanged" in env or "container unchanged" in env,
             "host < 1400%": max(host) < 1400}
    sustain = {"0 QueueTimeouts": qto == 0, "0 x 5xx": e5 == 0, "served >= 97%": srate >= .97 * rate,
               "post-sale p99 <= 1000ms": pct(post, .99) <= 1000}
    res, _ = verify(d)
    cpu = {k: (round(sum(v) / len(v)), round(max(v))) for k, v in by.items()}
    lat409 = [r["latency_ms"] for r in served if r["status"] == 409]
    return dict(name=name, d=d, rate=rate, recs=recs, delivered=delivered, sched99=sched99, hot=hot, st=st, sna=sna,
                qto=qto, e5=e5, served=served, srate=srate, post=post, by=by, host=host, samples=samples, busy=busy,
                waiters=waiters, valid=valid, sustain=sustain, res=res, cpu=cpu,
                lat_all=(pct([r["latency_ms"] for r in served], .5), pct([r["latency_ms"] for r in served], .99)),
                lat_post=(pct(post, .5), pct(post, .99)),
                lat409=(pct(lat409, .5), pct(lat409, .99)) if lat409 else (None, None))


def evaluate(name):
    c = compute(name)
    (d, rate, recs, delivered, sched99, hot, st, sna, qto, e5, served, srate, post, by, host, samples, busy,
     waiters, valid, sustain, res) = (c[k] for k in ("d", "rate", "recs", "delivered", "sched99", "hot", "st", "sna", "qto",
                                                    "e5", "served", "srate", "post", "by", "host", "samples", "busy",
                                                    "waiters", "valid", "sustain", "res"))
    m = lambda k: f"{sum(by[k]) / len(by[k]):.0f}/{max(by[k]):.0f}%" if by.get(k) else "-"
    print(f"\n=== {name}: target {rate}/s, {len(recs)} arrivals, buyer processes {d['args']['processes']}")
    print(f"  delivered {delivered:.1f}/s  sched p99 {sched99:.1f}ms  max worker hot s {max(hot.values())}  "
          f"sent-no-answer {sna}  host CPU max {max(host):.0f}% (mean {sum(host) / len(host):.0f}%)")
    print(f"  VALID: {all(valid.values())}" + ("" if all(valid.values()) else f"  failed {[k for k, v in valid.items() if not v]}"))
    print(f"  served {len(served)} = {srate:.1f}/s  QueueTimeouts {qto}  5xx {e5}  outcomes {dict(st)}")
    print(f"  latency all p50/p99 {pct([r['latency_ms'] for r in served], .5):.1f}/{pct([r['latency_ms'] for r in served], .99):.1f}ms; "
          f"arrivals after t=5s p50/p99 {pct(post, .5):.1f}/{pct(post, .99):.1f}ms")
    print(f"  SUSTAINABLE: {all(valid.values()) and all(sustain.values())}" +
          ("" if all(sustain.values()) else f"  failed {[k for k, v in sustain.items() if not v]}"))
    print(f"  CPU mean/max: buyer {m('buyer')}  nginx {m('lb-1')}  seller1 {m('seller1-1')}  seller2 {m('seller2-1')}  "
          f"seller3 {m('seller3-1')}  postgres {m('db-1')}")
    print(f"  pg: busy samples {len(busy)}/{len(samples)}, lock waiters mean {sum(waiters) / max(1, len(waiters)):.2f} max {max(waiters, default=0)}")
    print("  correctness: " + "  ".join(f"{k.split()[0]}:{'PASS' if ok else 'FAIL'}" for k, (ok, _) in res.items()))


if __name__ == "__main__":
    for n in sys.argv[1:]:
        evaluate(n)
