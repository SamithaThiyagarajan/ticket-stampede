"""Post-recovery verification for the M11 datastore-failure experiments.
Usage: python scripts/replay_check.py NAME [--url http://localhost:8080]
  S1 = /status now; must match the buyer's final snapshot (else state changed after the run).
  Replay every request_id that got 200 -> must be 200 with the SAME ticket.
  Replay every unknown-outcome request_id (no 200, some non-409) -> must be 200 with its S1
    ticket if S1 has that user's row, else 409. Only when sold out (a replay could buy).
  S2 = /status after replays -> must equal S1 byte-for-byte.
  Direct SQL audit + the 7 constraints still present and validated."""
import argparse
import asyncio
import gzip
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "buyer"))
from buyer import verify  # noqa: E402

ROOT = Path(__file__).parent.parent
EXPECTED_CONSTRAINTS = {
    "sale.sale_check c", "sale.sale_id_check c", "sale.sale_pkey p", "sale.sale_total_tickets_check c",
    "tickets.tickets_pkey p", "tickets.tickets_request_id_key u", "tickets.tickets_ticket_number_check c",
}


def sql(query):
    out = subprocess.run(["docker", "compose", "exec", "-T", "db", "psql", "-U", "tickets", "-tAc", query],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


async def replay(client, items):
    """items: [(request_id, user_id, expected)] with expected = ("200", ticket) or ("409", None)."""
    sem, bad, errors = asyncio.Semaphore(20), [], []

    async def one(rid, uid, expected):
        async with sem:
            try:
                r = await client.post("/buy", json={"user_id": uid, "request_id": rid})
            except httpx.HTTPError as e:
                errors.append((rid, type(e).__name__)); return
            got = (str(r.status_code), r.json().get("ticket_number") if r.status_code == 200 else None)
            if got != expected:
                bad.append((rid, expected, got))
    await asyncio.gather(*(one(*it) for it in items))
    return bad, errors


async def main(args):
    d = json.load(gzip.open(ROOT / "results" / f"{args.name}.json.gz", "rt"))
    records, saved = d["records"], d["status"]
    told, statuses, user_of = defaultdict(set), defaultdict(set), {}
    for r in records:
        user_of[r["request_id"]] = r["user_id"]
        statuses[r["request_id"]].add(r["status"])
        if r["status"] == 200:
            told[r["request_id"]].add(r["ticket_number"])
    ok = True

    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
        s1_raw = (await client.get("/status")).content
        s1 = json.loads(s1_raw)
        same_as_run = s1 == saved
        print(f"S1 == buyer's final snapshot: {same_as_run}"
              + ("" if same_as_run else f"  (rows {len(saved['tickets'])} -> {len(s1['tickets'])}, "
                                        f"sold {saved['sold']} -> {s1['sold']}: state changed AFTER the run)"))
        if not same_as_run:
            res, _ = verify({**d, "status": s1})
            print("  5 checks re-run against S1: " + "  ".join(
                f"{k.split()[0]}:{'PASS' if good else 'FAIL'}" for k, (good, _) in res.items()))
            ok &= all(good for good, _ in res.values())

        row_of = {t["user_id"]: t["ticket_number"] for t in s1["tickets"]}
        sold_out = s1["sold"] >= s1["total_tickets"]

        confirmed = [(rid, user_of[rid], ("200", next(iter(ns)))) for rid, ns in told.items() if len(ns) == 1]
        bad, errs = await replay(client, confirmed)
        print(f"confirmed (200) request_ids replayed: {len(confirmed)}; mismatches: {len(bad)}; replay transport errors: {len(errs)}")
        for b in bad[:10]:
            print(f"  LOST/CHANGED CONFIRMED SALE: {b}")
        ok &= not bad and not errs

        unknown = [rid for rid, st in statuses.items() if 200 not in st and st != {409}]
        if not unknown:
            print("unknown-outcome request_ids: 0")
        elif not sold_out:
            print(f"unknown-outcome request_ids: {len(unknown)} -> SKIPPED (not sold out; a replay could buy a ticket)")
        else:
            items = [(rid, user_of[rid], ("200", row_of[user_of[rid]]) if user_of[rid] in row_of else ("409", None))
                     for rid in unknown]
            committed = sum(1 for _, _, e in items if e[0] == "200")
            bad, errs = await replay(client, items)
            print(f"unknown-outcome request_ids replayed: {len(items)} (of which hold a row in S1 = "
                  f"committed-but-unacknowledged: {committed}); mismatches: {len(bad)}; replay transport errors: {len(errs)}")
            for b in bad[:10]:
                print(f"  MISMATCH: {b}")
            ok &= not bad and not errs

        s2_raw = (await client.get("/status")).content
        print(f"S2 == S1 byte-for-byte (replays changed nothing): {s2_raw == s1_raw}")
        ok &= s2_raw == s1_raw

    n, sold, mx, dn, dr = sql("select count(*), (select sold_count from sale), coalesce(max(ticket_number),0), "
                              "count(distinct ticket_number), count(distinct request_id) from tickets")[0].split("|")
    total = s1["total_tickets"]
    audit = int(n) == int(sold) == len(s1["tickets"]) and int(mx) <= total and int(dn) == int(n) == int(dr)
    print(f"SQL audit: count(*)={n} sold_count={sold} max(ticket_number)={mx} (total {total}) "
          f"distinct ticket_number={dn} distinct request_id={dr} -> {'CONSISTENT' if audit else 'INCONSISTENT'}")
    ok &= audit

    rows = sql("select conrelid::regclass::text||'.'||conname||' '||contype::text||' '||convalidated::text "
               "from pg_constraint where conrelid in ('tickets'::regclass,'sale'::regclass)")
    present = {r.rsplit(" ", 1)[0] for r in rows if r.endswith(" true")}
    cons_ok = present == EXPECTED_CONSTRAINTS
    print(f"constraints present and validated: {len(present)}/7 -> {'OK' if cons_ok else 'MISSING/CHANGED: ' + str(EXPECTED_CONSTRAINTS ^ present)}")
    ok &= cons_ok

    print(f"\nREPLAY VERDICT: {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("name")
    p.add_argument("--url", default="http://localhost:8080")
    sys.exit(0 if asyncio.run(main(p.parse_args())) else 1)
