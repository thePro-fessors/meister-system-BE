import os
from contextlib import asynccontextmanager
from typing import Any, Optional, Dict
import jwt
from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import logging
import traceback

logger = logging.getLogger("meister.main")

from core.storage import get_upload_base_dir, find_and_clean_orphan_files
from core.security import (
    JWT_SECRET_KEY,
    JWT_ALGORITHM,
    get_current_user,
    create_download_ticket,
    verify_and_consume_download_ticket,
    create_signed_download_token,
    verify_signed_download_token,
)
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
from routers.teachers import router as teachers_router
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

# [보안 1.7] 와일드카드 CORS 충돌 방어
# allow_credentials=True 상태에서 origins에 '*'가 포함되면 브라우저 CORS 정책 위반으로 차단되므로 가드 적용
allow_creds = True
if "*" in origins:
    allow_creds = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=allow_creds,
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

# 교사 업무 라우터 등록 (/api/teacher)
app.include_router(teachers_router)

# 📁 [보안 3.1] 증빙자료 파일 안전 조회 및 다운로드 (RBAC 및 학생 소유권 인가 검증)
# 기존 단순 StaticFiles 마운트의 무인가 개인정보 탈취(IDOR) 취약점을 해소
@app.get(
    "/uploads/{file_path:path}",
    summary="증빙자료 파일 안전 조회 및 다운로드 (RBAC 및 소유권 인가 검증)",
)
async def get_uploaded_file(
    file_path: str,
    request: Request,
    token: Optional[str] = Query(None, description="다운로드 서명 토큰 또는 레거시 쿼리 토큰"),
    ticket: Optional[str] = Query(None, description="일회용 다운로드 티켓(Single-use Ticket)"),
    conn: Any = Depends(get_db),
    redis: Any = Depends(get_redis),
):
    upload_root = os.path.abspath(get_upload_base_dir())
    clean_rel_path = os.path.normpath(file_path.strip().lstrip("/\\"))
    abs_file_path = os.path.abspath(os.path.join(upload_root, clean_rel_path))

    # 1. 인증 정보 추출 및 검증 (비인가자의 파일 존재 유무 정찰 차단: 401 선검증)
    user_uuid = None
    user_role = None

    # (A) [보안 1.2] 일회용 티켓 검증 (Single-use Ticket 우선 소비)
    if ticket and redis is not None:
        ticket_user = await verify_and_consume_download_ticket(ticket, clean_rel_path, redis)
        if ticket_user:
            user_uuid = ticket_user.get("uuid")
            user_role = ticket_user.get("role")
        else:
            return JSONResponse(
                status_code=401,
                content=SrFormat(
                    status_code=401,
                    success=False,
                    data=None,
                    error=Error(code="UNAUTHORIZED", message="유효하지 않거나 이미 만료/소비된 일회용 다운로드 티켓입니다."),
                ).model_dump(),
            )

    # (B) Authorization Bearer 헤더 검증
    if not user_uuid:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            jwt_token = auth_header[7:].strip()
            try:
                payload = jwt.decode(jwt_token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
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
                user_uuid = payload.get("sub")
                user_role = payload.get("role")
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

    # (C) token 쿼리 파라미터 (단기 서명 토큰 검증 또는 하위 호환용 JWT)
    if not user_uuid and token:
        # 단기 다운로드 서명 토큰 검증 시도
        signed_user = verify_signed_download_token(token, clean_rel_path)
        if signed_user:
            user_uuid = signed_user.get("uuid")
            user_role = signed_user.get("role")
        else:
            # 하위 호환용 JWT 디코딩
            try:
                payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
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
                user_uuid = payload.get("sub")
                user_role = payload.get("role")
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

    if not user_uuid:
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(code="UNAUTHORIZED", message="파일 열람 권한이 없습니다. 인증 토큰 또는 일회용 티켓이 필요합니다."),
            ).model_dump(),
        )

    # 3. 경로 탐색(Path Traversal) 공격 차단 및 디렉터리 경계 엄격화 (SECURITY_AND_AUDIT.md 1.4)
    try:
        if os.path.commonpath([upload_root, abs_file_path]) != upload_root or not os.path.isfile(abs_file_path):
            return JSONResponse(
                status_code=404,
                content=SrFormat(
                    status_code=404,
                    success=False,
                    data=None,
                    error=Error(code="FILE_NOT_FOUND", message="요청한 파일을 찾을 수 없습니다."),
                ).model_dump(),
            )
    except (ValueError, Exception):
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
    if user_role in ("admin", "teacher", 1, 2, "1", "2"):
        return FileResponse(abs_file_path)

    # 학생은 본인이 제출한 증빙자료만 열람 가능
    db_web_path = f"/uploads/{clean_rel_path.replace(os.sep, '/')}"
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT s.submission_id, s.student_id, st.uuid
            FROM submissions s
            JOIN students st ON s.student_id = st.student_id
            WHERE (s.file_path = %s OR s.file_path = %s) AND s.is_deleted = FALSE
            LIMIT 1
            """,
            (db_web_path, clean_rel_path),
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


# 🎫 [보안 1.2] 일회용 미디어 열람/다운로드 티켓 발급 API
@app.post(
    "/api/uploads/ticket",
    summary="일회용 미디어 열람/다운로드 티켓 발급 API (SECURITY_AND_AUDIT.md 1.2)",
)
async def issue_download_ticket(
    file_path: str = Query(..., description="열람 대상 파일 경로"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    redis: Any = Depends(get_redis),
    conn: Any = Depends(get_db),
):
    """
    브라우저 직접 링크 및 <img> 태그 열람 시 JWT 평문 노출을 방지하기 위해
    60초간 유효한 일회용 다운로드 티켓(ticket)을 발급합니다.
    """
    raw_p = file_path.strip().lstrip("/\\")
    if raw_p.startswith("uploads/"):
        raw_p = raw_p[8:]
    clean_rel = os.path.normpath(raw_p).lstrip("/\\")

    if clean_rel.startswith("..") or os.path.isabs(clean_rel):
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="INVALID_PATH", message="유효하지 않은 파일 경로입니다."),
            ).model_dump(),
        )

    user_role = current_user.get("role")
    user_uuid = current_user.get("uuid")

    # 학생 권한인 경우 본인 증빙 파일인지 DB 소유권 검증
    if user_role in ("student", "0", 0):
        db_web_path = f"/uploads/{clean_rel.replace(os.sep, '/')}"
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute(
                """
                SELECT s.submission_id
                FROM submissions s
                JOIN students st ON s.student_id = st.student_id
                WHERE (s.file_path = %s OR s.file_path = %s) AND st.uuid = %s AND s.is_deleted = FALSE
                LIMIT 1
                """,
                (db_web_path, clean_rel, user_uuid),
            )
            row = await cur.fetchone()
        if not row:
            return JSONResponse(
                status_code=403,
                content=SrFormat(
                    status_code=403,
                    success=False,
                    data=None,
                    error=Error(code="FORBIDDEN", message="본인의 증빙 파일에 대해서만 다운로드 티켓을 발급받을 수 있습니다."),
                ).model_dump(),
            )

    ticket_id = await create_download_ticket(
        user_uuid=user_uuid,
        role=str(user_role),
        file_path=clean_rel,
        redis=redis,
        ttl_seconds=60,
    )
    from core.security import create_signed_download_token
    signed_token = create_signed_download_token(
        user_uuid=user_uuid,
        role=str(user_role),
        file_path=clean_rel,
        ttl_seconds=60,
    )
    url_path = f"/uploads/{clean_rel.replace(os.sep, '/')}"
    return SrFormat(
        status_code=200,
        success=True,
        data={
            "ticket": ticket_id,
            "signedToken": signed_token,
            "expiresIn": 60,
            "downloadUrl": f"{url_path}?ticket={ticket_id}",
            "fileUrl": f"{url_path}?ticket={ticket_id}",
            "signedUrl": f"{url_path}?token={signed_token}",
        },
    ).model_dump()


# 🧹 [유지보수] 고아 파일 정리 가비지 컬렉터(GC) 실행 API
@app.post(
    "/api/admin/storage/gc",
    summary="고아 파일 정리 가비지 컬렉터(GC) 실행 API",
)
async def run_storage_garbage_collection(
    dry_run: bool = Query(False, description="테스트 실행 여부"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    conn: Any = Depends(get_db),
):
    """
    데이터베이스와 디스크를 대조하여 고아 파일을 탐색 및 정리합니다 (관리자 전용).
    """
    if current_user.get("role") not in ("admin", 2, "2"):
        return JSONResponse(
            status_code=403,
            content=SrFormat(
                status_code=403,
                success=False,
                data=None,
                error=Error(code="FORBIDDEN", message="관리자만 저장소 GC를 실행할 수 있습니다."),
            ).model_dump(),
        )
    result = await find_and_clean_orphan_files(conn, dry_run=dry_run)
    return SrFormat(status_code=200, success=True, data=result).model_dump()


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
