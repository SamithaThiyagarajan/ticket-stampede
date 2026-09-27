"""Per-transaction evidence for the M12 commit-reply-drop run.
Usage: python diag/analyze_commit_drop.py NAME [--url http://localhost:8080]
For every COMMIT whose reply the proxy dropped: the INSERT it committed (from the proxy's
protocol log), every buyer response for that request_id, the ticket row in PostgreSQL, and a
targeted replay (twice) with SQL row counts before and after."""
import argparse
import gzip
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
R = ROOT / "results"


def sql(q):
    out = subprocess.run(["docker", "compose", "exec", "-T", "db", "psql", "-U", "tickets", "-tAc", q],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [l for l in out.splitlines() if l]


def main(name, url):
    d = json.load(gzip.open(R / f"{name}.json.gz", "rt"))
    by_rid = defaultdict(list)
    for r in d["records"]:
        by_rid[r["request_id"]].append(r)
    events = [json.loads(l) for l in (R / f"{name}.proxy.log").read_text().splitlines() if l.startswith("{")]
    commits = [e for e in events if e.get("ev") == "commit"]
    dropped = [e for e in commits if e["dropped"]]
    print(f"proxy: {len(commits)} COMMIT replies seen, {len(dropped)} dropped "
          f"({sum(1 for e in dropped if e['insert'])} of them in a purchase transaction with a ticket INSERT)")
    count0 = int(sql("select count(*) from tickets")[0])
    print(f"SQL before targeted replay: count(*) = {count0}\n")

    class3, dup_saved, no_insert = 0, 0, 0
    with httpx.Client(base_url=url, timeout=30) as client:
        for e in dropped:
            ins = e["insert"]
            if not ins:
                no_insert += 1
                print(f"COMMIT #{e['n']}: dropped reply of a transaction WITHOUT an INSERT (read-only commit)")
                continue
            rid, uid, tn = ins["request_id"], ins["user_id"], ins["ticket_number"]
            copies = sorted(by_rid[rid], key=lambda r: r["i"])
            statuses = [r["status"] for r in copies]
            row = sql(f"select ticket_number, user_id from tickets where request_id = '{rid}'")
            replays = [client.post("/buy", json={"user_id": uid, "request_id": rid}) for _ in range(2)]
            rp = [(r.status_code, r.json().get("ticket_number")) for r in replays]
            got200 = 200 in statuses
            ok = row == [f"{tn}|{uid}"] and all(x == (200, tn) for x in rp)
            if not got200 and ok:
                class3 += 1
                kind = "COMMITTED BUT UNACKNOWLEDGED"
            elif got200:
                dup_saved += 1
                kind = "another copy of this request_id received 200"
            else:
                kind = "UNEXPECTED"
            print(f"COMMIT #{e['n']}: proxy saw INSERT ticket #{tn} for {rid}/{uid}, COMMIT reply dropped")
            print(f"   buyer responses for {rid} (all copies): {statuses}"
                  + (f"; 200 said ticket #{[r['ticket_number'] for r in copies if r['status'] == 200]}" if got200 else ""))
            print(f"   PostgreSQL row for {rid}: {row or 'NONE'}")
            print(f"   replay x2 -> {rp}   => {kind}{'' if ok else '  (MISMATCH)'}")
    count1 = int(sql("select count(*) from tickets")[0])
    print(f"\nSQL after targeted replay: count(*) = {count1} ({'unchanged' if count1 == count0 else 'CHANGED'})")
    print(f"summary: dropped COMMIT replies {len(dropped)} = committed-but-unacknowledged {class3} "
          f"+ covered by another copy's 200 {dup_saved} + no INSERT {no_insert}")
    print(f"buyer outcome totals: {dict(Counter(r['status'] for r in d['records']))}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("name")
    p.add_argument("--url", default="http://localhost:8080")
    a = p.parse_args()
    main(a.name, a.url)
