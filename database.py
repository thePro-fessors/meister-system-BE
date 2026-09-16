import asyncmy
from asyncmy.cursors import DictCursor
import dotenv
import os

dotenv.load_dotenv()

DB_CONFIG={
    "host": "localhost",
    "port": 3306,
    "user": "wantogohome",
    "password": os.getenv("DATABASE_PASSWORD"),
    "database": "swMeister",
    "autocommit": True,
    "cursorclass": DictCursor
}

pool = None

async def init_db_pool():
    global pool
    pool = await asyncmy.create_pool(**DB_CONFIG,minsize=1, maxsize=10)

async def close_db_pool():
    global pool
    if pool:
        pool.close()
        await pool.wait_closed()

async def get_db():
    async with pool.acquire() as conn:
        yield conn