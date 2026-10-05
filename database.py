import os
import sys

# 로컬 개발 환경에서 가상환경 미활성화 상태로 python3 직접 호출 시 로컬 .venv 자동 탐색
if "VIRTUAL_ENV" not in os.environ:
    _base_dir = os.path.dirname(os.path.abspath(__file__))
    _venv_root = os.path.join(_base_dir, ".venv")
    if os.path.isdir(_venv_root):
        import glob
        for _sp in glob.glob(os.path.join(_venv_root, "lib", "python*", "site-packages")):
            if _sp not in sys.path:
                sys.path.append(_sp)

try:
    import asyncmy
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    asyncmy = None  # type: ignore
    DictCursor = None  # type: ignore
import dotenv
import redis.asyncio as aioredis
from typing import AsyncGenerator
import logging

logger = logging.getLogger("meister.database")

dotenv.load_dotenv()

# DB 커넥션 풀 크기 환경 변수화 (SECURITY_AND_AUDIT.md 2.3 & TODO.md 6.2 P2)
DB_POOL_MIN = int(os.getenv("DATABASE_POOL_MIN", "5"))
DB_POOL_MAX = int(os.getenv("DATABASE_POOL_MAX", "30"))
REDIS_POOL_MAX = int(os.getenv("REDIS_POOL_MAX", "30"))

DB_CONFIG = {
    "host": os.getenv("DATABASE_HOST", "localhost"),
    "port": int(os.getenv("DATABASE_PORT", "3306")),
    "user": os.getenv("DATABASE_USER", "root"),
    "password": os.getenv("DATABASE_PASSWORD"),
    "database": os.getenv("DATABASE_NAME", "swMeister"),
    "autocommit": True,
}

pool = None
redis_pool = None
redis_client: aioredis.Redis | None = None

async def init_db_pool():
    global pool
    # 프로덕션 환경 Fail-Fast 검증 (SECURITY_AND_AUDIT.md 1.6)
    is_prod = os.getenv("ENVIRONMENT") == "production" or os.getenv("PROD") == "true"
    if is_prod and not DB_CONFIG.get("password"):
        raise RuntimeError("[FATAL] 프로덕션 환경에서는 DATABASE_PASSWORD 환경변수가 반드시 설정되어야 합니다.")
    pool = await asyncmy.create_pool(**DB_CONFIG, minsize=DB_POOL_MIN, maxsize=DB_POOL_MAX)

async def close_db_pool():
    global pool
    if pool:
        pool.close()
        await pool.wait_closed()
        pool = None

async def get_db():
    async with pool.acquire() as conn:
        try:
            yield conn
        finally:
            try:
                await conn.rollback()
            except Exception:
                pass
            try:
                await conn.autocommit(True)
            except Exception:
                pass

async def init_redis_pool():
    global redis_pool, redis_client
    # 하드코딩된 패스워드 제거 (SECURITY_AND_AUDIT.md 1.6)
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    is_prod = os.getenv("ENVIRONMENT") == "production" or os.getenv("PROD") == "true"
    if is_prod:
        has_auth = "@" in redis_url and ":" in redis_url.split("@")[0]
        if not has_auth and not os.getenv("REDIS_PASSWORD"):
            raise RuntimeError("[FATAL] 프로덕션 환경에서는 REDIS_PASSWORD 또는 인증 정보가 포함된 REDIS_URL이 필수입니다.")
    redis_pool = aioredis.ConnectionPool.from_url(
        redis_url,
        decode_responses=True,
        max_connections=REDIS_POOL_MAX
    )
    redis_client = aioredis.Redis(connection_pool=redis_pool)

async def close_redis_pool():
    global redis_pool, redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None
    if redis_pool:
        await redis_pool.disconnect()
        redis_pool = None

async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """싱글톤 Redis 클라이언트를 재사용하여 연결 오버헤드를 최소화합니다."""
    global redis_client
    if redis_client is None:
        await init_redis_pool()
    yield redis_client
