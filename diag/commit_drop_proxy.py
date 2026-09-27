"""TEST-ONLY Postgres-protocol-aware TCP proxy that drops the reply to a COMMIT.

Simulates the committed-but-unacknowledged window: PostgreSQL has made the COMMIT durable
and sent CommandComplete("COMMIT"), but the client never receives it (connection reset).
Postgres sends that message only after the commit record is durable (synchronous_commit=on).

Env: UPSTREAM=db:5432  LISTEN_PORT=6543  DROP_EVERY=N (drop COMMIT replies #N, #2N, ...)
     MAX_DROPS=K (stop dropping after K drops).  Logs one JSON line per COMMIT to stdout.
Requires the client to connect with sslmode=disable (no SSLRequest single-byte reply).
"""
import asyncio
import itertools
import json
import os
import struct
import sys
import time

UP_HOST, UP_PORT = os.environ.get("UPSTREAM", "db:5432").split(":")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "6543"))
DROP_EVERY = int(os.environ.get("DROP_EVERY", "0"))
MAX_DROPS = int(os.environ.get("MAX_DROPS", "0"))
commits = drops = 0
conn_ids = itertools.count(1)


def log(**kw):
    print(json.dumps({"t": round(time.time(), 6), **kw}), flush=True)


def cstrings(b, n):
    out = []
    for _ in range(n):
        i = b.index(b"\x00")
        out.append(b[:i].decode(errors="replace"))
        b = b[i + 1:]
    return out, b


def bind_params(body):
    """Bind: portal\0 stmt\0 int16 nfmt, fmts, int16 nparams, (int32 len, bytes)*"""
    (portal, stmt), rest = cstrings(body, 2)
    nf = struct.unpack("!h", rest[:2])[0]
    fmts = list(struct.unpack(f"!{nf}h", rest[2:2 + 2 * nf])) if nf else []
    rest = rest[2 + 2 * nf:]
    n = struct.unpack("!h", rest[:2])[0]
    rest, params = rest[2:], []
    for i in range(n):
        ln = struct.unpack("!i", rest[:4])[0]
        val, rest = (None, rest[4:]) if ln < 0 else (rest[4:4 + ln], rest[4 + ln:])
        fmt = fmts[i] if len(fmts) > 1 else (fmts[0] if fmts else 0)
        params.append(val if fmt == 1 else (val.decode() if val is not None else None))
    return stmt, params


class Conn:
    def __init__(self):
        self.id, self.stmts, self.insert = next(conn_ids), {}, None


async def handle(creader, cwriter):
    global commits, drops
    try:
        ureader, uwriter = await asyncio.open_connection(UP_HOST, int(UP_PORT))
    except OSError:
        cwriter.transport.abort(); return
    c = Conn()

    async def client_to_server():
        buf, startup = b"", True
        try:
            while data := await creader.read(65536):
                uwriter.write(data)                     # forward unchanged, then inspect
                buf += data
                while True:
                    if startup:                         # untyped: StartupMessage / SSLRequest
                        if len(buf) < 8: break
                        n, code = struct.unpack("!II", buf[:8])
                        if len(buf) < n: break
                        buf = buf[n:]
                        if code == 80877103: log(ev="warning", conn=c.id, msg="SSLRequest seen; use sslmode=disable")
                        if code == 196608: startup = False
                        continue
                    if len(buf) < 5: break
                    t, n = buf[:1], struct.unpack("!I", buf[1:5])[0]
                    if len(buf) < 1 + n: break
                    body, buf = buf[5:1 + n], buf[1 + n:]
                    if t == b"P":                       # Parse: name, query
                        (name, query), _ = cstrings(body, 2)
                        c.stmts[name] = query
                    elif t == b"B":
                        stmt, params = bind_params(body)
                        if c.stmts.get(stmt, "").lstrip().upper().startswith("INSERT INTO TICKETS"):
                            tn = struct.unpack("!i", params[0])[0] if isinstance(params[0], bytes) else int(params[0])
                            c.insert = {"ticket_number": tn, "user_id": params[1] if isinstance(params[1], str) else params[1].decode(),
                                        "request_id": params[2] if isinstance(params[2], str) else params[2].decode()}
        except (ConnectionError, OSError):
            pass
        finally:
            uwriter.close()

    async def server_to_client():
        global commits, drops
        buf = b""
        try:
            while data := await ureader.read(65536):
                buf += data
                out = b""
                while len(buf) >= 5:
                    t, n = buf[:1], struct.unpack("!I", buf[1:5])[0]
                    if len(buf) < 1 + n: break
                    msg, buf = buf[:1 + n], buf[1 + n:]
                    if t == b"C" and msg[5:].startswith(b"COMMIT\x00"):
                        commits += 1
                        drop = DROP_EVERY > 0 and drops < MAX_DROPS and commits % DROP_EVERY == 0
                        log(ev="commit", n=commits, conn=c.id, dropped=drop, insert=c.insert)
                        if drop:
                            drops += 1
                            cwriter.write(out); await cwriter.drain()   # everything BEFORE the COMMIT reply
                            cwriter.transport.abort()                   # reset: the COMMIT reply is never delivered
                            uwriter.close()
                            return
                    if t == b"Z" and msg[5:6] == b"I":   # ReadyForQuery, status Idle: transaction ended
                        c.insert = None                  # ('T' = still in a transaction, e.g. after the INSERT)
                    out += msg
                if out:
                    cwriter.write(out); await cwriter.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            if not cwriter.transport.is_closing():
                cwriter.close()

    await asyncio.gather(client_to_server(), server_to_client())


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT)
    log(ev="listening", port=LISTEN_PORT, upstream=f"{UP_HOST}:{UP_PORT}", drop_every=DROP_EVERY, max_drops=MAX_DROPS)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
