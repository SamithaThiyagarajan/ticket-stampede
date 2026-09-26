import asyncio
import os
from pathlib import Path

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "10"))
NAIVE_DELAY_MS = int(os.environ.get("NAIVE_DELAY_MS", "50"))
SCHEMA = (Path(__file__).parent / "schema_naive.sql").read_text()

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
        await asyncio.sleep(NAIVE_DELAY_MS / 1000)

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
    async with pool.acquire() as conn:
        sale = await conn.fetchrow("SELECT total_tickets, sold_count FROM sale")
        rows = await conn.fetch("SELECT ticket_number, user_id FROM tickets ORDER BY ticket_number")
    return {
        "total_tickets": sale["total_tickets"] if sale else 0,
        "sold": sale["sold_count"] if sale else 0,
        # A list, not a dict: duplicate ticket numbers must stay visible.
        "tickets": [{"ticket_number": r["ticket_number"], "user_id": r["user_id"]} for r in rows],
    }
