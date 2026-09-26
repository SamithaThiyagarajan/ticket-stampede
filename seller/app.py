import asyncio
import os
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "10"))
# Simulated work between the capacity check and the insert, applied in BOTH modes
# at the same point so naive-vs-safe is a fair comparison.
WORK_DELAY_MS = int(os.environ.get("WORK_DELAY_MS", "50"))
SELLER_MODE = os.environ.get("SELLER_MODE", "safe")
if SELLER_MODE not in ("naive", "safe"):
    raise ValueError(f"SELLER_MODE must be naive or safe, got {SELLER_MODE!r}")
# Schema defaults to the mode. Override only for the constraint-backstop experiment
# (SELLER_MODE=naive SELLER_SCHEMA=safe: racy code, constrained tables).
SELLER_SCHEMA = os.environ.get("SELLER_SCHEMA") or SELLER_MODE
if SELLER_SCHEMA not in ("naive", "safe"):
    raise ValueError(f"SELLER_SCHEMA must be naive or safe, got {SELLER_SCHEMA!r}")
SCHEMA = (Path(__file__).parent / f"schema_{SELLER_SCHEMA}.sql").read_text()

app = FastAPI()
pool: asyncpg.Pool


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=DB_POOL_SIZE, max_size=DB_POOL_SIZE)


class ResetRequest(BaseModel):
    tickets: int


class BuyRequest(BaseModel):
    user_id: str
    request_id: str


@app.post("/reset")
async def reset(body: ResetRequest):
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
        await conn.execute("INSERT INTO sale (total_tickets, sold_count) VALUES ($1, 0)", body.tickets)
    return {"total_tickets": body.tickets}


@app.post("/buy")
async def buy(body: BuyRequest):
    return await (buy_safe(body) if SELLER_MODE == "safe" else buy_naive(body))


async def buy_safe(body: BuyRequest):
    # One transaction (READ COMMITTED). Statement ORDER matters: the idempotency
    # check must come after the lock, so it sees every previously committed sale.
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. Lock the sale row. Every other /buy blocks here until we commit.
            #    On acquiring the lock, Postgres re-reads the latest committed row.
            sale = await conn.fetchrow(
                "SELECT total_tickets, sold_count FROM sale WHERE id = 1 FOR UPDATE"
            )

            # 2. Idempotency: under the lock, so no concurrent copy can slip past.
            existing = await conn.fetchrow(
                "SELECT ticket_number, user_id FROM tickets WHERE request_id = $1", body.request_id
            )
            if existing is not None:
                if existing["user_id"] != body.user_id:
                    return JSONResponse(status_code=422, content={
                        "error": "request_id was already used by a different user_id"})
                return {"ticket_number": existing["ticket_number"]}

            # 3. Capacity.
            if sale["sold_count"] >= sale["total_tickets"]:
                return JSONResponse(status_code=409, content={"sold_out": True})

            # Same simulated work as naive mode, but here it is done while holding the lock.
            await asyncio.sleep(WORK_DELAY_MS / 1000)

            # 4 + 5. Insert and count commit together or not at all (invariant 4).
            ticket_number = sale["sold_count"] + 1
            await conn.execute(
                "INSERT INTO tickets (ticket_number, user_id, request_id) VALUES ($1, $2, $3)",
                ticket_number, body.user_id, body.request_id,
            )
            await conn.execute("UPDATE sale SET sold_count = sold_count + 1 WHERE id = 1")
    return {"ticket_number": ticket_number}


async def buy_naive(body: BuyRequest):
    # NAIVE: no transaction, no lock. Each statement autocommits on its own,
    # so other requests can interleave between every step below.
    async with pool.acquire() as conn:
        # 1. Idempotency check (check-then-act: two copies can both see "not found").
        existing = await conn.fetchval(
            "SELECT ticket_number FROM tickets WHERE request_id = $1", body.request_id
        )
        if existing is not None:
            return {"ticket_number": existing}

        # 2. Capacity check (check-then-act: many readers can see the same sold_count).
        sale = await conn.fetchrow("SELECT total_tickets, sold_count FROM sale")
        sold = sale["sold_count"]
        if sold >= sale["total_tickets"]:
            return JSONResponse(status_code=409, content={"sold_out": True})

        # 3. Simulated work between check and act; widens the race window.
        await asyncio.sleep(WORK_DELAY_MS / 1000)

        # 4. Ticket number derived from the stale read -> duplicate numbers.
        ticket_number = sold + 1
        await conn.execute(
            "INSERT INTO tickets (ticket_number, user_id, request_id) VALUES ($1, $2, $3)",
            ticket_number, body.user_id, body.request_id,
        )

        # 5. Read-modify-write computed in Python -> lost updates -> sold_count drifts.
        await conn.execute("UPDATE sale SET sold_count = $1", sold + 1)

    return {"ticket_number": ticket_number}


@app.get("/status")
async def status():
    # One REPEATABLE READ snapshot: both reads see the same set of commits, so a
    # mid-sale /status cannot show sold and rows from different moments.
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            sale = await conn.fetchrow("SELECT total_tickets, sold_count FROM sale")
            rows = await conn.fetch("SELECT ticket_number, user_id FROM tickets ORDER BY ticket_number")
    return {
        "total_tickets": sale["total_tickets"] if sale else 0,
        "sold": sale["sold_count"] if sale else 0,
        # A list, not a dict: duplicate ticket numbers must stay visible.
        "tickets": [{"ticket_number": r["ticket_number"], "user_id": r["user_id"]} for r in rows],
    }
