import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import logging
import traceback

logger = logging.getLogger("meister.main")

from fastapi.staticfiles import StaticFiles
from core.storage import get_upload_base_dir
from database import init_db_pool, close_db_pool, init_redis_pool, close_redis_pool
from routers.auth import router as auth_router
from routers.students import router as students_router
from routers.submissions import router as submissions_router
from sr_format import Error, SrFormat


load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MySQL 및 Redis 커넥션 풀 초기화
    try:
        await init_db_pool()
    except Exception as e:
        logger.critical(f"[FATAL] DB 커넥션 풀 초기화 실패 - 서버를 시작할 수 없습니다: {e}")
        raise
    try:
        await init_redis_pool()
    except Exception as e:
        logger.critical(f"[FATAL] Redis 커넥션 풀 초기화 실패 - 서버를 시작할 수 없습니다: {e}")
        raise

    yield

    # 커넥션 풀 종료
    await close_db_pool()
    await close_redis_pool()


app = FastAPI(
    title="Meister API",
    description=".... --- -- .",
    version="1.0.0",
    lifespan=lifespan
)

# 🌐 CORS(Cross-Origin Resource Sharing) 설정
# 환경 변수에 CORS_ORIGINS가 정의되어 있으면 콤마로 파싱하고, 없으면 기본 개발 주소 허용
env_origins = os.getenv("CORS_ORIGINS")
if env_origins:
    origins = [origin.strip() for origin in env_origins.split(",") if origin.strip()]
else:
    origins = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🔒 보안 응답 헤더 미들웨어 (SECURITY_AND_AUDIT.md 1.7)
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

# 라우터 등록: /auth 및 /api/auth 동시 지원 (FE 호환성 보장)
app.include_router(auth_router)
app.include_router(auth_router, prefix="/api")

# 학생 업무 라우터 등록 (/api/students)
app.include_router(students_router)

# 증빙자료 제출 및 심사 라우터 등록 (/api/submissions)
app.include_router(submissions_router)

# 📁 정적 파일 업로드 경로 마운트 (TODO.md 5장)
UPLOAD_DIR = get_upload_base_dir()
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")



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
    msg = first_error.get("msg", "입력 데이터 유효성 검증 실패")
    field = ".".join(str(x) for x in first_error.get("loc", []))
    return JSONResponse(
        status_code=400,
        content=SrFormat(
            status_code=400,
            success=False,
            data=None,
            error=Error(
                code="VALIDATION_ERROR",
                message=f"[{field}] {msg}"
            )
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """전역 미처리 예외 핸들러: SrFormat 500 응답 보장 및 내부 스택 트레이스 은닉"""
    logger.critical(
        f"[UNHANDLED_EXCEPTION] {request.method} {request.url.path} -> {type(exc).__name__}: {exc}\n"
        f"{traceback.format_exc()}"
    )
    return JSONResponse(
        status_code=500,
        content=SrFormat(
            status_code=500,
            success=False,
            data=None,
            error=Error(
                code="INTERNAL_SERVER_ERROR",
                message="서버 내부 오류가 발생했습니다."
            ),
        ).model_dump(),
    )
