"""Analyze nginx<->seller TCP connections from a pcap captured in the lb namespace.
Usage: python diag/analyze_resets.py NAME     (reads results/NAME.pcap and results/NAME.json.gz)
For every RST sent by a seller: the failing connection's history — last data before the
seller's FIN (idle time), seller FIN, nginx's request, the RST — and the matching 502s.
Also: how often sellers close idle connections (and after how long), and how often nginx
sent data on a connection after the seller's FIN had already arrived.
Pure-python pcap parser (Ethernet / IPv4 / TCP only)."""
import gzip
import json
import socket
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10
R = Path(__file__).parent.parent / "results"


def packets(path):
    data = open(path, "rb").read()
    magic = struct.unpack("<I", data[:4])[0]
    scale = {0xA1B2C3D4: 1e-6, 0xA1B23C4D: 1e-9}[magic]
    linktype = struct.unpack("<I", data[20:24])[0]
    assert linktype == 1, f"expected Ethernet, got linktype {linktype}"
    off = 24
    while off + 16 <= len(data):
        sec, frac, incl, _ = struct.unpack("<IIII", data[off:off + 16])
        pkt = data[off + 16: off + 16 + incl]
        off += 16 + incl
        if len(pkt) < 34 or pkt[12:14] != b"\x08\x00":
            continue
        ip = pkt[14:]
        ihl, total = (ip[0] & 0xF) * 4, struct.unpack(">H", ip[2:4])[0]
        if ip[9] != 6:
            continue
        tcp = ip[ihl:]
        sport, dport = struct.unpack(">HH", tcp[:4])
        thl, flags = (tcp[12] >> 4) * 4, tcp[13]
        yield (sec + frac * scale, socket.inet_ntoa(ip[12:16]), sport, socket.inet_ntoa(ip[16:20]), dport,
               flags, total - ihl - thl)


def fl(f):
    return "".join(n for b, n in ((SYN, "S"), (FIN, "F"), (RST, "R"), (PSH, "P"), (ACK, ".")) if f & b)


def main(name):
    # Key = (nginx ip, nginx port, seller ip, generation). nginx REUSES source ports for new
    # connections, so a SYN from nginx starts a new generation (otherwise separate
    # connections merge — a first version of this script showed "requests 46s after FIN").
    conns, gen = defaultdict(list), defaultdict(int)
    for t, src, sp, dst, dp, f, n in packets(R / f"{name}.pcap"):
        if dp == 8000:
            if f & SYN and not f & ACK:
                gen[(src, sp, dst)] += 1
            k = (src, sp, dst)
            conns[k + (gen[k],)].append((t, "N>S", f, n))
        elif sp == 8000:
            k = (dst, dp, src)
            conns[k + (gen[k],)].append((t, "S>N", f, n))
    # Capture-quality checks (a second, independent loss detector besides tcpdump's counter):
    orphan = sum(len(v) for k, v in conns.items() if k[3] == 0)   # packets whose connection's SYN was never captured
    post_close = 0                                                 # data after a full FIN/FIN close, no new SYN
    for v in conns.values():
        fins = set()
        for t, dr, f, n in v:
            if n > 0 and len(fins) == 2:
                post_close += 1
            if f & FIN:
                fins.add(dr)
    stats = (R / f"{name}.capstats").read_text() if (R / f"{name}.capstats").exists() else ""
    kdrop = next((int(l.split()[0]) for l in stats.splitlines() if "dropped by kernel" in l), None)
    valid = kdrop == 0 and orphan == 0 and post_close == 0
    print("tcpdump: " + "; ".join(l.strip() for l in stats.splitlines()))
    print(f"consistency: packets without a captured SYN = {orphan}; data after full close = {post_close}")
    print(f"CAPTURE {'VALID' if valid else 'INVALID'} (requires kernel drops = 0 and both consistency counts = 0)")
    conns = {k: v for k, v in conns.items() if k[3] > 0}

    t_first = min(ev[0][0] for ev in conns.values())
    d = json.load(gzip.open(R / f"{name}.json.gz", "rt"))
    rate, start_at = d["args"]["rate"], d["start_at"]
    rel = lambda t: t - start_at          # seconds relative to the buyer's shared t=0

    seller_fin_idle, reuse_after_fin, nginx_fins, syns = [], [], 0, Counter()
    resets = []
    for key, ev in conns.items():
        syns[key[2]] += sum(1 for e in ev if e[1] == "N>S" and e[2] & SYN and not e[2] & ACK)
        last_data, sfin = None, None
        for t, dr, f, n in ev:
            if dr == "S>N" and f & FIN and sfin is None:
                sfin = t
                if last_data is not None:
                    seller_fin_idle.append(t - last_data)
            if dr == "N>S" and f & FIN:
                nginx_fins += 1
            if dr == "N>S" and n > 0 and sfin is not None:
                reuse_after_fin.append((key, t - sfin))
            if dr == "S>N" and f & RST:
                resets.append(key)
            if n > 0 and sfin is None:
                last_data = t
    print(f"capture: {sum(len(v) for v in conns.values())} packets, {len(conns)} upstream TCP connections, "
          f"t={rel(t_first):.2f}s .. (relative to buyer start)")
    print(f"new upstream connections (SYN) per seller: {dict(syns)}")
    # Who closed: the side whose FIN came FIRST (the other side's FIN is just the reply).
    initiator, idle_by = Counter(), defaultdict(list)
    for ev in conns.values():
        first = next(((t, dr) for t, dr, f, n in ev if f & FIN), None)
        if first is None:
            initiator["still open at capture end"] += 1
            continue
        side = "seller" if first[1] == "S>N" else "nginx"
        initiator[side] += 1
        data = [t for t, dr, f, n in ev if n > 0 and t < first[0]]
        if data:
            idle_by[side].append(first[0] - data[-1])
    print(f"who closed each connection (first FIN): {dict(initiator)}")
    for side, xs in sorted(idle_by.items()):
        s = sorted(xs)
        print(f"  {side}-initiated closes: idle before FIN min {s[0]:.3f}s  p50 {s[len(s)//2]:.3f}s  max {s[-1]:.3f}s; "
              f"within 4.9-5.3s: {sum(4.9 <= x <= 5.3 for x in s)} of {len(s)}")
    print(f"nginx sent request data AFTER the seller's FIN had arrived: {len(reuse_after_fin)} time(s)")
    for key, gap in reuse_after_fin[:10]:
        print(f"  {key[0]}:{key[1]} -> {key[2]}  request sent {gap*1000:.2f} ms after the seller's FIN arrived")

    print(f"\nRST sent by a seller: {len({k for k in resets})} connection(s)")
    bad = [r for r in d["records"] if r["status"] == 502]
    print(f"502s in buyer records: {len(bad)}" + (" at t=" + ", ".join(
        f"{r['i']/rate + r['latency_ms']/1000:.3f}s ({r.get('upstream')})" for r in bad) if bad else ""))
    for key in dict.fromkeys(resets):
        ev = conns[key]
        print(f"\n--- failing connection {key[0]}:{key[1]} <-> {key[2]}:8000 (last 10 packets; t rel. to buyer start) ---")
        for t, dr, f, n in ev[-10:]:
            print(f"  t={rel(t):9.4f}s  {dr}  flags={fl(f):5} payload={n}")
        sfin = next((t for t, dr, f, n in ev if dr == "S>N" and f & FIN), None)
        data_before = [t for t, dr, f, n in ev if n > 0 and (sfin is None or t < sfin)]
        req_after = [t for t, dr, f, n in ev if dr == "N>S" and n > 0 and sfin is not None and t >= sfin]
        rst = next(t for t, dr, f, n in ev if dr == "S>N" and f & RST)
        if sfin is None:
            print("  seller FIN before the RST: NONE  -> not an idle close")
        else:
            print(f"  last data before seller FIN: t={rel(data_before[-1]):.4f}s; seller FIN: t={rel(sfin):.4f}s "
                  f"-> idle {sfin - data_before[-1]:.3f}s")
            print(f"  nginx request after the FIN: " + (f"t={rel(req_after[0]):.4f}s (+{(req_after[0]-sfin)*1000:.2f} ms)"
                                                        if req_after else "none"))
        print(f"  seller RST: t={rel(rst):.4f}s  (epoch {rst:.4f})")
        near = [r for r in bad if abs(r["i"] / rate + r["latency_ms"] / 1000 - rel(rst)) < 0.5]
        for r in near:
            print(f"  matching buyer 502: answered t={r['i']/rate + r['latency_ms']/1000:.4f}s, upstream {r.get('upstream')}, "
                  f"{(r['i']/rate + r['latency_ms']/1000 - rel(rst))*1000:+.1f} ms from the RST")
    nginx_log = R / f"{name}.nginx.log"
    if nginx_log.exists() and nginx_log.read_text().strip():
        print("\nnginx error log:\n" + nginx_log.read_text().rstrip())


if __name__ == "__main__":
    main(sys.argv[1])
