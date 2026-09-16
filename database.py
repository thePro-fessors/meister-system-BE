import asyncmy
from asyncmy.cursors import DictCursor
import dotenv
import os
import redis.asyncio as aioredis
from typing import AsyncGenerator

dotenv.load_dotenv()

DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "wantogohome",
    "password": os.getenv("DATABASE_PASSWORD"),
    "database": "swMeister",
    "autocommit": True,
    "cursorclass": DictCursor
}

pool = None
redis_pool = None

async def init_db_pool():
    global pool
    pool = await asyncmy.create_pool(**DB_CONFIG, minsize=1, maxsize=10)

async def close_db_pool():
    global pool
    if pool:
        pool.close()
        await pool.wait_closed()
        pool = None

async def get_db():
    async with pool.acquire() as conn:
        yield conn

async def init_redis_pool():
    global redis_pool
    redis_url = os.getenv("REDIS_URL", "redis://:meister_redis_pw!@localhost:6379/0")
    redis_pool = aioredis.ConnectionPool.from_url(
        redis_url,
        decode_responses=True,
        max_connections=20
    )

async def close_redis_pool():
    global redis_pool
    if redis_pool:
        await redis_pool.disconnect()
        redis_pool = None

async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    global redis_pool
    if redis_pool is None:
        await init_redis_pool()
    client = aioredis.Redis(connection_pool=redis_pool)
    try:
        yield client
    finally:
        await client.aclose()
