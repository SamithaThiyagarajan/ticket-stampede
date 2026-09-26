"""True-positive tests for the verifier: tamper with a real saved safe run and check
that verify() fails for exactly the expected reason. Run: python buyer/test_verify.py"""
import copy
import json
import sys
from pathlib import Path

from buyer import verify

FIXTURE = Path(__file__).parent.parent / "results" / "m6_safe_50ms_run1.json"


def failing(data):
    results, unexpected = verify(data)
    return {name for name, (ok, _) in results.items() if not ok}, unexpected, results


def main():
    base = json.loads(FIXTURE.read_text())
    base["records"] = [r for r in base["records"] if r["status"] in (200, 409)]  # clean baseline
    winners = [r for r in base["records"] if r["status"] == 200]
    loser = next(r for r in base["records"] if r["status"] == 409)

    # Control: untouched data passes everything.
    failed, unexpected, _ = failing(base)
    assert failed == set() and unexpected == 0, failed
    print("control                     : all checks pass")

    # A. A confirmed sale vanishes from durable state (sold adjusted so check 4 stays green).
    a = copy.deepcopy(base)
    victim = winners[0]
    a["status"]["tickets"] = [t for t in a["status"]["tickets"] if t["ticket_number"] != victim["ticket_number"]]
    a["status"]["sold"] -= 1
    failed, _, res = failing(a)
    assert failed == {"5 responses = state"}, failed
    print(f"A lost confirmed sale       : only check 5 fails -> {res['5 responses = state'][1]}")

    # B. A confirmed ticket ends up owned by a buyer who was told sold-out.
    b = copy.deepcopy(base)
    for t in b["status"]["tickets"]:
        if t["ticket_number"] == victim["ticket_number"]:
            t["user_id"] = loser["user_id"]
    failed, _, res = failing(b)
    assert failed == {"5 responses = state"}, failed
    print(f"B ticket given to other user: only check 5 fails -> {res['5 responses = state'][1]}")

    # C. The server committed but the buyer's response was lost in transit.
    c = copy.deepcopy(base)
    ambiguous_user = victim["user_id"]
    for r in c["records"]:
        if r["user_id"] == ambiguous_user:
            r["status"] = "ReadError"
            r.pop("ticket_number", None)
    failed, unexpected, res = failing(c)
    assert failed == set() and unexpected >= 1, (failed, unexpected)
    assert "(info) 1 late-confirmed" in res["5 responses = state"][1]
    print(f"C response lost in transit  : checks pass, {unexpected} unexpected -> INCONCLUSIVE; "
          f"{res['5 responses = state'][1].split('(info) ')[1]}")

    # D. A row exists for a buyer whose every copy failed before leaving the client.
    #    The server never saw the request, so this is impossible -> must FAIL.
    d = copy.deepcopy(base)
    for r in d["records"]:
        if r["user_id"] == ambiguous_user:
            r["status"] = "PoolTimeout"
            r.pop("ticket_number", None)
    failed, _, res = failing(d)
    assert failed == {"5 responses = state"} and "1 rows for never-sent" in res["5 responses = state"][1], failed
    print(f"D row for never-sent request: only check 5 fails -> {res['5 responses = state'][1]}")

    print("\nverifier true-positive tests: ALL OK")


if __name__ == "__main__":
    sys.exit(main())
