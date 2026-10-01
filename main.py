import os
from contextlib import asynccontextmanager
from typing import Any, Optional
import jwt
from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import logging
import traceback

logger = logging.getLogger("meister.main")

from core.storage import get_upload_base_dir
from core.security import JWT_SECRET_KEY, JWT_ALGORITHM
from database import init_db_pool, close_db_pool, init_redis_pool, close_redis_pool, get_db, get_redis
try:
    import asyncmy
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    class _AsyncmyStub:
        Connection = Any
    asyncmy = _AsyncmyStub()  # type: ignore
    DictCursor = Any  # type: ignore
from routers.auth import router as auth_router
from routers.students import router as students_router
from routers.submissions import router as submissions_router
from routers.points import router as points_router
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

# 상벌점 내역 조회 라우터 등록 (/api/points)
app.include_router(points_router)

# 📁 [보안 3.1] 증빙자료 파일 안전 조회 및 다운로드 (RBAC 및 학생 소유권 인가 검증)
# 기존 단순 StaticFiles 마운트의 무인가 개인정보 탈취(IDOR) 취약점을 해소
@app.get(
    "/uploads/{file_path:path}",
    summary="증빙자료 파일 안전 조회 및 다운로드 (RBAC 및 소유권 인가 검증)",
)
async def get_uploaded_file(
    file_path: str,
    request: Request,
    token: Optional[str] = Query(None, description="브라우저 직접 다운로드 및 미디어 열람용 쿼리 토큰"),
    conn: Any = Depends(get_db),
    redis: Any = Depends(get_redis),
):
    upload_root = os.path.abspath(get_upload_base_dir())
    clean_rel_path = os.path.normpath(file_path.strip().lstrip("/\\"))
    abs_file_path = os.path.abspath(os.path.join(upload_root, clean_rel_path))

    # 1. 인증 토큰 추출 및 검증 (비인가자의 파일 존재 유무 정찰 차단: 401 선검증)
    auth_header = request.headers.get("Authorization", "")
    jwt_token = None
    if auth_header.startswith("Bearer "):
        jwt_token = auth_header[7:].strip()
    elif token:
        jwt_token = token.strip()

    if not jwt_token:
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(code="UNAUTHORIZED", message="파일 열람 권한이 없습니다. 인증 토큰이 필요합니다."),
            ).model_dump(),
        )

    # 2. 토큰 서명 및 만료/블랙리스트 검증
    try:
        payload = jwt.decode(jwt_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(code="UNAUTHORIZED", message="인증 토큰이 만료되었습니다."),
            ).model_dump(),
        )
    except Exception:
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(code="UNAUTHORIZED", message="유효하지 않은 인증 토큰입니다."),
            ).model_dump(),
        )

    jti = payload.get("jti")
    if jti and redis is not None:
        try:
            if await redis.get(f"blacklist:{jti}"):
                return JSONResponse(
                    status_code=401,
                    content=SrFormat(
                        status_code=401,
                        success=False,
                        data=None,
                        error=Error(code="UNAUTHORIZED", message="로그아웃되었거나 폐기된 토큰입니다."),
                    ).model_dump(),
                )
        except Exception:
            pass

    # 3. 경로 탐색(Path Traversal) 공격 차단 및 파일 실존 확인
    if not abs_file_path.startswith(upload_root) or not os.path.isfile(abs_file_path):
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="FILE_NOT_FOUND", message="요청한 파일을 찾을 수 없습니다."),
            ).model_dump(),
        )

    # 4. 역할별 인가(RBAC) 및 학생 본인 증빙 소유권 검증 (IDOR 방어)
    user_uuid = payload.get("sub")
    user_role = payload.get("role")

    # 관리자 및 교사는 모든 학생의 증빙자료 심사/열람 허용
    if user_role in ("admin", "teacher", 1, 2):
        return FileResponse(abs_file_path)

    # 학생은 본인이 제출한 증빙자료만 열람 가능
    db_web_path = f"/uploads/{clean_rel_path.replace(os.sep, '/')}"
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT s.submission_id, s.student_id, st.uuid
            FROM submissions s
            JOIN students st ON s.student_id = st.student_id
            WHERE s.file_path = %s AND s.is_deleted = FALSE
            LIMIT 1
            """,
            (db_web_path,),
        )
        row = await cur.fetchone()

    if not row or row["uuid"] != user_uuid:
        return JSONResponse(
            status_code=403,
            content=SrFormat(
                status_code=403,
                success=False,
                data=None,
                error=Error(code="FORBIDDEN", message="본인이 제출한 증빙자료만 열람할 수 있습니다."),
            ).model_dump(),
        )

    return FileResponse(abs_file_path)



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
