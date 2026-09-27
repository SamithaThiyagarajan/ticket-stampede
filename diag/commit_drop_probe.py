"""Proof that commit_drop_proxy drops the COMMIT reply AFTER the transaction committed.
Run with the proxy at DROP_EVERY=2, MAX_DROPS=1: through the proxy, tx#1 COMMIT (forwarded),
tx#2 COMMIT (reply dropped), tx#3 ROLLBACK; then a DIRECT connection reads pg_xact_status.
No tables are created or touched (transaction IDs only)."""
import asyncio

import asyncpg

PROXY = "postgresql://tickets:tickets@commitproxy:6543/tickets?sslmode=disable"
DIRECT = "postgresql://tickets:tickets@db:5432/tickets"


async def tx(finish):
    conn = await asyncpg.connect(PROXY)
    try:
        await conn.execute("BEGIN")
        xid = await conn.fetchval("SELECT pg_current_xact_id()::text")
        try:
            await conn.execute(finish)
            return xid, f"{finish}: client got a normal reply"
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as e:
            return xid, f"{finish}: client got {type(e).__name__}: {str(e)[:70]}"
    finally:
        conn.terminate()


async def main():
    results = [await tx("COMMIT"), await tx("COMMIT"), await tx("ROLLBACK")]
    direct = await asyncpg.connect(DIRECT)
    for i, (xid, client) in enumerate(results, 1):
        status = await direct.fetchval("SELECT pg_xact_status($1::xid8)", int(xid))
        print(f"tx#{i} xid={xid}  {client}  ->  pg_xact_status (direct) = {status}")
    await direct.close()


asyncio.run(main())
