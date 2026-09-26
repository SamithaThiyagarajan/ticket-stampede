"""Summarize a py-spy --idle --format raw profile of the seller's event loop.
Only main-thread stacks (through run_forever) count; executor worker threads are ignored.
Idle = main thread parked in selectors.select. Busy samples are attributed two ways:
  self      : library of the innermost frame (where the CPU instruction was)
  inclusive : share of busy samples whose stack passes through a component
Usage: python scripts/profile_summary.py results/NAME.pyspy.txt"""
import re
import sys
from collections import Counter

CATS = [  # (label, regex on "func (file:line)" frame)
    ("asyncpg driver", r"asyncpg/"),
    ("pydantic validation", r"pydantic"),
    ("fastapi/starlette framework", r"(fastapi|starlette)/"),
    ("uvicorn/h11/httptools HTTP", r"(uvicorn|h11|httptools)/"),
    ("logging (access log)", r"logging/"),
    ("json", r"json/"),
    ("app.py handler", r"app\.py"),
    ("asyncio loop / sockets", r"(asyncio/|selectors\.py|socket\.py|ssl\.py)"),
]


def cat(frame):
    for label, rx in CATS:
        if re.search(rx, frame):
            return label
    return "other: " + frame.split("(")[-1].split(":")[0]


main = Counter()
busy_self, busy_incl, idle, total = Counter(), Counter(), 0, 0
for line in open(sys.argv[1]):
    stack, n = line.rstrip().rsplit(" ", 1)
    n = int(n)
    frames = stack.split(";")
    if not any("run_forever" in f for f in frames):
        continue                                  # executor threads etc.
    total += n
    if frames[-1].startswith("select (selectors.py"):
        idle += n
        continue
    busy_self[cat(frames[-1])] += n
    for label in {cat(f) for f in frames}:
        busy_incl[label] += n

busy = total - idle
print(f"main-thread samples: {total}  idle (in select): {idle} ({100*idle/total:.1f}%)  busy: {busy} ({100*busy/total:.1f}%)")
print("\nbusy time by SELF frame (where the CPU was):")
for k, v in busy_self.most_common(12):
    print(f"  {100*v/busy:5.1f}%  {k}")
print("\nbusy time INCLUSIVE (stack passes through):")
for k, v in busy_incl.most_common(10):
    print(f"  {100*v/busy:5.1f}%  {k}")
