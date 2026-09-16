from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from database import init_db_pool, close_db_pool, init_redis_pool, close_redis_pool
from routers.auth import router as auth_router
from sr_format import Error, SrFormat


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MySQL 및 Redis 커넥션 풀 초기화
    try:
        await init_db_pool()
    except Exception as e:
        print(f"[Warning] Failed to initialize DB pool: {e}")
    try:
        await init_redis_pool()
    except Exception as e:
        print(f"[Warning] Failed to initialize Redis pool: {e}")

    yield

    # 커넥션 풀 종료
    await close_db_pool()
    await close_redis_pool()


app = FastAPI(lifespan=lifespan)

# 라우터 등록
app.include_router(auth_router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "status_code" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=SrFormat(
            status_code=exc.status_code,
            success=False,
            data=None,
            error=Error(code="HTTP_ERROR", message=str(exc.detail)),
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    first_error = exc.errors()[0] if exc.errors() else {}
    field = " -> ".join([str(loc) for loc in first_error.get("loc", [])])
    reason = first_error.get("msg", "입력값이 올바르지 않습니다.")

    detail = f"{field}: {reason}" if field else reason

    return JSONResponse(
        status_code=400,
        content=SrFormat(
            status_code=400,
            success=False,
            error=Error(code="VALIDATION_ERROR", message=detail),
        ).model_dump(),
    )


@app.get("/")
async def root():
    return SrFormat(status_code=200, success=True, data={"What am I": "I am nothing"})
