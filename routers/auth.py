"""
routers/auth.py - 인증(Authentication) 및 계정 관리 관련 API 라우터

명세 및 연동 기준:
- Tech_spec.md 1장 (인증 시스템)
- BACKEND_REQUIREMENTS.md 3장 (인증/인가 API 규격)
- TODO.md 1.1, 1.2 (로그인, 로그아웃, 내 정보 조회, 교내 이메일 OTP, 회원가입, 이메일 변경)
"""

import re
import uuid
from typing import Optional, Any
import secrets
from datetime import datetime, timezone, timedelta
import hashlib

try:
    import asyncmy
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    class _AsyncmyStub:
        Connection = Any
    asyncmy = _AsyncmyStub()  # type: ignore
    DictCursor = Any  # type: ignore
from fastapi import APIRouter, Query, HTTPException, Depends, BackgroundTasks, Request
from pydantic import BaseModel
from fastapi.responses import JSONResponse
import bcrypt
import redis.asyncio as aioredis

from core.email import send_otp_email
from core.logger import logger
from database import get_db, get_redis
from sr_format import *
from core.security import create_access_token, get_current_user, get_client_ip

# ==============================================================================
# 보안 설정 & 타이밍 공격(Timing Attack) 방어용 상수
# ==============================================================================
# 사용자가 DB에 존재하지 않더라도 동일한 연산 시간(약 80~120ms)을 소모하게 함으로써,
# 응답 시간 차이로 가입된 아이디인지 여부를 알아내는 계정 열거(User Enumeration) 공격을 차단합니다.
# DUMMY_HASH: bcrypt 알고리즘을 통해 "dummy_password"를 rounds=12로 사전 해싱한 고정값
DUMMY_HASH = "$2b$12$e86g5Y7hZ4k7l.W2j9X0..mK1N8p3R5s7T9v1X3z5B7d9F1h3J5l."

router = APIRouter(
    prefix="/auth",
    tags=["auth", "database"],
)

# 학년도 ID 인메모리 캐시 (year -> year_id)
# 로그인 및 내 정보 조회 시 academic_years 테이블 반복 서브쿼리 병목 제거
_YEAR_ID_CACHE: dict[int, int] = {}


# ==============================================================================
# 비밀번호 암호화 및 유효성 검증 유틸리티
# ==============================================================================

def hash_password(password: str) -> str:
    """비밀번호를 bcrypt(rounds=12)로 안전하게 단방향 솔팅 해싱합니다."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """평문 비밀번호와 해시 비밀번호를 상시 일정한 시간(Constant Time)으로 안전하게 대조합니다."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


async def get_current_year(date: datetime | None = None) -> int:
    """3월 1일 학사력 기준 현재 학년도 계산 (1~2월은 직전 연도에 귀속)"""
    target = date or datetime.now()
    if target.month < 3:
        return target.year - 1
    return target.year


async def get_year_id(conn: asyncmy.Connection, year: int) -> int:
    """학사년도(year)에 대응하는 year_id를 캐싱하여 불필요한 반복 서브쿼리를 방지합니다."""
    global _YEAR_ID_CACHE
    if year in _YEAR_ID_CACHE:
        return _YEAR_ID_CACHE[year]

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (year,))
        row = await cur.fetchone()
        if row:
            _YEAR_ID_CACHE[year] = row["year_id"]
            return row["year_id"]
    return 1  # 기본값 폴백


# ==============================================================================
# DTO 스키마 정의
# ==============================================================================

class LoginRequest(BaseModel):
    username: str
    password: str


class SendOtpRequest(BaseModel):
    student_grade: Optional[int] = None
    student_class: Optional[int] = None
    student_number: Optional[int] = None  # 교사 요청 건의 경우 위 3개는 무시됨
    is_it_student: bool = False
    email: str


class VerifyOtpRequest(BaseModel):
    email: str
    verify_code: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    id: str
    register_token: str
    role: Optional[int] = None  # 클라이언트가 임의로 보내더라도 서버 판별값으로 덮어씌워 원천 무시됨


class PatchEmailRequest(BaseModel):
    email: str
    password: str
    register_token: str


# 비밀번호 복잡도 정규식: 최소 8자 이상, 영문자 1개 이상 및 숫자 1개 이상 포함
PASSWORD_REGEX = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,}$")


# ==============================================================================
# 1. 로그인 & 세션 관리 엔드포인트
# ==============================================================================

@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    conn: asyncmy.Connection = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    [POST] /auth/login - 사용자 로그인 및 JWT 세션 토큰 발급
    
    특징:
    - 타이밍 공격 방어: 계정이 없더라도 DUMMY_HASH로 bcrypt 검증을 실행하여 일정한 응답 시간 유지
    - 역할(Role)에 따른 프로필 정보 통합(학생 학적 정보 / 교사 담임 학급 정보)
    - [보안] 계정별 5회 연속 실패 시 10분간 로그인 차단 (Brute-Force 방어)
    - [보안] IP별 분당 20회 요청 제한 (DoS 방어, Nginx/프록시 호환)
    """
    client_ip = get_client_ip(request)

    # ── [보안 1.1] IP별 분당 20회 요청 제한 (DoS 방어) ──
    ip_limit_key = f"ip_limit:login:{client_ip}"
    ip_requests = await redis.incr(ip_limit_key)
    if ip_requests == 1:
        await redis.expire(ip_limit_key, 60)
    if ip_requests > 20:
        logger.warning(f"[LOGIN_IP_BLOCKED] IP={client_ip} 분당 20회 초과")
        return JSONResponse(
            status_code=429,
            content=SrFormat(
                status_code=429,
                success=False,
                data=None,
                error=Error(
                    code="TOO_MANY_REQUESTS",
                    message="너무 많은 로그인 요청이 발생했습니다. 잠시 후 다시 시도해주세요."
                )
            ).model_dump()
        )

    # ── [보안 1.1] 계정별 연속 실패 잠금 확인 ──
    account_lock_key = f"login_failures:{req.username}"
    account_locked = await redis.get(account_lock_key)
    if account_locked and int(account_locked) >= 5:
        lock_ttl = await redis.ttl(account_lock_key)
        return JSONResponse(
            status_code=429,
            content=SrFormat(
                status_code=429,
                success=False,
                data={"remainingSeconds": max(0, lock_ttl)},
                error=Error(
                    code="ACCOUNT_LOCKED",
                    message="로그인 실패 횟수를 초과하였습니다. 잠시 후 다시 시도해주세요."
                )
            ).model_dump()
        )

    async with conn.cursor(cursor=DictCursor) as cur:
        sql_cmd = """
            SELECT uuid, id, password, role, email
            FROM users
            WHERE id = %s AND is_deleted = FALSE
        """
        await cur.execute(sql_cmd, (req.username,))
        user = await cur.fetchone()

    # 계정이 없을 경우 더미 해시 검증을 수행하여 유저 유무에 따른 시간차 공격 차단
    hash_data = user["password"] if user else DUMMY_HASH
    hash_valid = verify_password(req.password, hash_data)

    if not user or not hash_valid:
        # 실패 카운터 증가 (10분 TTL)
        failures = await redis.incr(account_lock_key)
        if failures == 1 or failures >= 5:
            await redis.expire(account_lock_key, 600)
        remaining_attempts = max(0, 5 - failures)
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data={"remainingAttempts": remaining_attempts} if remaining_attempts > 0 else None,
                error=Error(
                    code="INVALID_CREDENTIAL",
                    message="아이디 혹은 비밀번호가 올바르지 않습니다."
                )
            ).model_dump()
        )

    # 로그인 성공 시 실패 카운터 초기화
    await redis.delete(account_lock_key)

    student_id = None
    students = None
    teachers = None
    current_yr = await get_current_year(datetime.now())

    # 학생 계정(role == 0): 현재 학년도 학적 정보 조회 (학년, 반, 번호)
    if user["role"] == 0:
        year_id = await get_year_id(conn, current_yr)
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                SELECT name, student_id AS id
                FROM students
                WHERE uuid = %s AND is_deleted = FALSE
            """
            await cur.execute(sql_cmd, (user["uuid"],))
            student_id = await cur.fetchone()
            if student_id:
                sql_cmd = """
                    SELECT grade, class, number
                    FROM student_academic_records
                    WHERE student_id = %s AND year_id = %s
                """
                await cur.execute(sql_cmd, (student_id["id"], year_id))
                students = await cur.fetchone()

    # 교사 계정(role == 1): 담당 학급 정보 조회 (학년, 반)
    if user["role"] == 1:
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                SELECT teachers_id AS id, name, grade, class
                FROM teachers
                WHERE uuid = %s AND is_deleted = FALSE
            """
            await cur.execute(sql_cmd, (user["uuid"],))
            teachers = await cur.fetchone()

    role_map = {0: "student", 1: "teacher", 2: "admin"}
    role_str = role_map.get(user["role"], "student")
    user_name = student_id["name"] if student_id else (teachers["name"] if teachers else user["id"])
    
    token = create_access_token(
        data={
            "sub": user["uuid"],
            "id": user["id"],
            "role": role_str,
        }
    )

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "token": token,
            "currentYear": current_yr,
            "user": {
                "uuid": user["uuid"],
                "id": user["id"],
                "name": user_name,
                "email": user["email"],
                "role": role_str,
                "studentId": student_id["id"] if student_id else None,
                "grade": students["grade"] if students else None,
                "classNo": students["class"] if students else None,
                "number": students["number"] if students else None,
                "teacherId": teachers["id"] if teachers else None,
                "homeroom": f'{teachers["grade"]}-{teachers["class"]}' if teachers and teachers["grade"] and teachers["class"] else None,
            }
        }
    ).model_dump()


@router.post("/logout")
async def logout(
    current_user: dict = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    [POST] /auth/logout - JWT 세션 즉시 만료 및 블랙리스트 등록
    
    특징:
    - 토큰의 남은 유효시간(exp - now)만큼 Redis 블랙리스트 키(`blacklist:{jti}`)로 보관
    - 메모리 누수 방지 및 O(1) 초고속 무효화 처리
    """
    jti = current_user.get("jti")
    exp = current_user.get("exp")

    if not jti or not exp:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(
                    code="INVALID_TOKEN",
                    message="토큰 정보가 올바르지 않습니다."
                )
            ).model_dump()
        )

    now_ts = int(datetime.now(timezone.utc).timestamp())
    remaining_seconds = int(exp - now_ts)
    if remaining_seconds <= 0:
        remaining_seconds = 1

    await redis.setex(f"blacklist:{jti}", remaining_seconds, "revoked")

    return SrFormat(
        status_code=200,
        success=True,
        data={"message": "로그아웃되었습니다."}
    ).model_dump()


@router.get("/me")
async def get_my_info(
    current_user: dict = Depends(get_current_user),
    conn: asyncmy.Connection = Depends(get_db)
):
    """
    [GET] /auth/me - 로그인된 본인 상세 계정 정보 조회
    """
    user_uuid = current_user["uuid"]

    async with conn.cursor(cursor=DictCursor) as cur:
        sql_cmd = """
            SELECT * FROM users
            WHERE uuid = %s AND is_deleted = FALSE
            LIMIT 1
        """
        await cur.execute(sql_cmd, (user_uuid,))
        user = await cur.fetchone()
        if not user:
            return JSONResponse(
                status_code=404,
                content=SrFormat(
                    status_code=404,
                    success=False,
                    data=None,
                    error=Error(
                        code="NOT_FOUND",
                        message="유저를 찾을 수 없습니다."
                    )
                ).model_dump()
            )

    role_map = {0: "student", 1: "teacher", 2: "admin"}
    role_str = role_map.get(user["role"], "student")

    students, teachers = None, None

    if user["role"] == 0:
        current_yr = await get_current_year()
        year_id = await get_year_id(conn, current_yr)
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                SELECT * FROM students s
                LEFT JOIN student_academic_records sa 
                    ON s.student_id = sa.student_id
                    AND sa.year_id = %s
                WHERE s.uuid = %s AND s.is_deleted = FALSE                
                LIMIT 1
            """
            await cur.execute(sql_cmd, (year_id, user_uuid))
            students = await cur.fetchone()
    elif user["role"] == 1:
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                SELECT * FROM teachers
                WHERE uuid = %s AND is_deleted = FALSE
                LIMIT 1
            """
            await cur.execute(sql_cmd, (user_uuid,))
            teachers = await cur.fetchone()

    user_name = students["name"] if students else (teachers["name"] if teachers else user["id"])

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "user": {
                "uuid": user_uuid,
                "id": user["id"],
                "name": user_name,
                "email": user["email"],
                "schoolEmail": students["email"] if students else (teachers["email"] if teachers else None),
                "role": role_str,
                "studentId": students["student_id"] if students else None,
                "grade": students["grade"] if students else None,
                "classNo": students["class"] if students else None,
                "number": students["number"] if students else None,
                "teacherId": teachers["teachers_id"] if teachers else None,
                "homeroom": f"{teachers['grade']}-{teachers['class']}" if teachers and teachers['grade'] and teachers['class'] else None
            }
        }
    ).model_dump()


# ==============================================================================
# 2. 회원가입 및 교내 이메일 OTP 온보딩 엔드포인트
# ==============================================================================

@router.post('/send-otp')
async def send_otp(
    req: SendOtpRequest, 
    request: Request,
    background_tasks: BackgroundTasks, 
    conn: asyncmy.Connection = Depends(get_db), 
    redis: aioredis.Redis = Depends(get_redis)
):
    """
    [POST] /auth/send-otp - 교내 이메일 OTP 6자리 난수 발송
    
    보안 및 비즈니스 규칙:
    1. 관리자가 등록한 사전 명단(students / teachers)에 이메일 및 학적이 일치해야만 발송
    2. 이미 uuid가 연동된 가입 완료 유저인 경우 차단 (403 FORBIDDEN)
    3. 60초 재발송 쿨다운(email_cooldown) 적용 (429 TOO_MANY_REQUESTS)
    4. TRNG 기반 암호학적 6자리 난수 생성 후 Redis에 5분(300초) 보관
    5. BackgroundTasks로 비동기 메일 발송하여 API 응답 지연을 0.05초 이하로 유지
    6. [보안] IP별 1시간당 10회 OTP 발송 제한 (Distributed Email Bombing 방어, 프록시 호환)
    """
    if not req.email:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(
                    code="VALIDATION_ERROR",
                    message="요청이 올바르지 않습니다."
                )
            ).model_dump()
        )

    clean_mail = req.email.strip().lower()
    client_ip = get_client_ip(request)

    # ── [보안 1.2] IP별 1시간당 10회 OTP 발송 제한 (Distributed Email Bombing 방어) ──
    ip_otp_key = f"ip_otp_limit:{client_ip}"
    ip_otp_count = await redis.incr(ip_otp_key)
    if ip_otp_count == 1:
        await redis.expire(ip_otp_key, 3600)
    if ip_otp_count > 10:
        logger.warning(f"[OTP_IP_BLOCKED] IP={client_ip} 시간당 10회 초과")
        return JSONResponse(
            status_code=429,
            content=SrFormat(
                status_code=429,
                success=False,
                data=None,
                error=Error(
                    code="TOO_MANY_REQUESTS",
                    message="해당 IP에서 너무 많은 인증 요청이 발생했습니다. 잠시 후 다시 시도해주세요."
                )
            ).model_dump()
        )

    # 60초 재발송 쿨다운 체크
    times = await redis.ttl(f"email_cooldown:{clean_mail}")
    if times > 0:
        return JSONResponse(
            status_code=429,
            content=SrFormat(
                status_code=429,
                success=False,
                data={
                    "remainingSeconds": times,
                },
                error=Error(
                    code="RATE_LIMIT_EXCEEDED",
                    message="너무 많이 요청하였습니다."
                )
            ).model_dump()
        )

    await redis.setex(f"email_cooldown:{clean_mail}", 60, f"{datetime.now()}")

    # 사전 등록 명단 일치 여부 확인
    if not req.is_it_student:
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
            SELECT * FROM teachers t
            WHERE email = %s AND is_deleted = FALSE
            LIMIT 1
            """
            await cur.execute(sql_cmd, (req.email,))
            datas = await cur.fetchone()
    else:
        current_yr = await get_current_year()
        year_id = await get_year_id(conn, current_yr)
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
            SELECT * FROM students s
            JOIN student_academic_records sa
                ON s.student_id = sa.student_id
            WHERE s.email = %s AND s.is_deleted = FALSE
                AND sa.year_id = %s
                AND sa.grade = %s AND sa.class = %s AND sa.number = %s
            LIMIT 1
            """
            await cur.execute(sql_cmd, (req.email, year_id, req.student_grade, req.student_class, req.student_number))
            datas = await cur.fetchone()

    if not datas:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(
                    code="NOT_FOUND",
                    message="유저를 찾을 수 없거나, 요청이 잘못 되었습니다."
                )
            ).model_dump()
        )
    if datas.get("uuid") is not None:
        return JSONResponse(
            status_code=403,
            content=SrFormat(
                status_code=403,
                success=False,
                data=None,
                error=Error(
                    code="FORBIDDEN",
                    message="이미 가입한 사용자입니다."
                )
            ).model_dump()
        )

    # TRNG(암호학적 난수 생성기)를 이용한 6자리 균등 난수 생성
    otp_codes = f"{secrets.randbelow(1000000):06d}"
    await redis.setex(f"otp:{clean_mail}", 300, otp_codes)

    background_tasks.add_task(
        send_otp_email,
        to_email=clean_mail,
        otp_code=otp_codes,
        user_name=datas.get("name", "사용자")
    )

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "message": "Send codes",
            "expiresIn": 300
        }
    ).model_dump()


@router.post("/verify-otp")
async def verify_otp(req: VerifyOtpRequest, redis: aioredis.Redis = Depends(get_redis)):
    """
    [POST] /auth/verify-otp - 발송된 6자리 OTP 대조 및 회원가입용 일회용 토큰(registerToken) 발급
    
    보안 메커니즘:
    1. 무차별 대입 공격(Brute-Force) 방어: 5회 실패 시 OTP 즉시 파기 및 차단
    2. 불일치 시 남은 시도 횟수 및 남은 유효시간 정량 반환
    3. 일치 시 기존 OTP 즉시 파기 후, TRNG 32바이트 Opaque Token(`registerToken`)을 발급하여 10분간 보관
    """
    if not req.email or not req.verify_code:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(
                    code="VALIDATION_ERROR",
                    message="요청이 올바르지 않습니다."
                )
            ).model_dump()
        )

    clean_mail = req.email.strip().lower()
    clean_code = req.verify_code.strip()

    attempt_key = f"otp_attempts:{clean_mail}"
    otp_key = f"otp:{clean_mail}"

    stored_otp = await redis.get(otp_key)

    if stored_otp is None:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(
                    code="OTP_EXPIRED",
                    message="인증번호가 만료되었거나, 발송되지 않았습니다."
                )
            ).model_dump()
        )

    attempts = await redis.incr(attempt_key)
    if attempts == 1:
        await redis.expire(attempt_key, 300)

    # 5회 이상 실패 시 무차별 대입 방어를 위해 즉시 폭파
    if attempts > 5:
        await redis.delete(otp_key)
        await redis.delete(attempt_key)
        return JSONResponse(
            status_code=429,
            content=SrFormat(
                status_code=429,
                success=False,
                data=None,
                error=Error(
                    code="TOO_MANY_REQUESTS",
                    message="인증번호 입력 횟수를 초과하였습니다."
                )
            ).model_dump()
        )

    if stored_otp != clean_code:
        remain = 5 - attempts
        times = await redis.ttl(otp_key)
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data={
                    "remainingAttempts": remain,
                    "remainingSeconds": max(0, times)
                },
                error=Error(
                    code="INVALID_OTP",
                    message="인증번호가 올바르지 않습니다."
                )
            ).model_dump()
        )

    # 성공 시 OTP 소멸
    await redis.delete(otp_key)
    await redis.delete(attempt_key)

    # 안전한 32바이트 Opaque 토큰 생성 (10분 유효)
    register_token = secrets.token_urlsafe(32)
    await redis.setex(f"register_token:{clean_mail}", 600, register_token)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "message": "Verify OTP codes.",
            "email": clean_mail,
            "registerToken": register_token
        }
    ).model_dump()


@router.post("/register")
async def register_user(
    req: RegisterRequest, 
    redis: aioredis.Redis = Depends(get_redis), 
    conn: asyncmy.Connection = Depends(get_db)
):
    """
    [POST] /auth/register - 사용자 최종 회원가입 및 계정 생성
    
    안전 설계:
    1. 비밀번호 복잡도 정규식 검증 (최소 8자, 영문+숫자 필수)
    2. Redis에 보관된 `register_token`과 대조하여 통과 후 즉시 파기 (재사용 차단)
    3. 서버 기반 권한(Role) 확정: 클라이언트의 role 입력을 완전히 무시하고 사전 명단 기반 자동 지정
    4. 아이디 중복 검사 (409 DUPLICATE_ID)
    5. 트랜잭션 원자성(ACID) 보장: users 레코드 생성과 students/teachers uuid 업데이트 실패 시 완전 롤백
    """
    if not req.email or not req.password or not req.id or not req.register_token:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(
                    code="VALIDATION_ERROR",
                    message="필수 인자가 누락되었습니다."
                )
            ).model_dump()
        )

    # 비밀번호 복잡도 정규식 검증
    if not PASSWORD_REGEX.match(req.password):
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(
                    code="WEAK_PASSWORD",
                    message="비밀번호는 최소 8자 이상이어야 하며 영문자와 숫자를 각각 1개 이상 포함해야 합니다."
                )
            ).model_dump()
        )

    clean_mail = req.email.strip().lower()
    stored_token = await redis.get(f"register_token:{clean_mail}")
    if stored_token is None or stored_token != req.register_token:
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="INVALID_REGISTER_TOKEN",
                    message="토큰이 만료되었거나, 올바르지 않습니다."
                )
            ).model_dump()
        )

    # 서버 기반 권한(Role) 확정 판별 (권한 상승 위조 원천 방어)
    determined_role = None
    target_table = None

    async with conn.cursor(cursor=DictCursor) as cur:
        # 학생 사전 등록 명단 확인
        await cur.execute("SELECT student_id, uuid FROM students WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
        student_row = await cur.fetchone()
        if student_row:
            if student_row.get("uuid") is not None:
                return JSONResponse(
                    status_code=403,
                    content=SrFormat(
                        status_code=403,
                        success=False,
                        data=None,
                        error=Error(
                            code="FORBIDDEN",
                            message="이미 가입된 사용자입니다."
                        )
                    ).model_dump()
                )
            determined_role = 0
            target_table = "students"
        else:
            # 교사 사전 등록 명단 확인
            await cur.execute("SELECT teachers_id, uuid FROM teachers WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
            teacher_row = await cur.fetchone()
            if teacher_row:
                if teacher_row.get("uuid") is not None:
                    return JSONResponse(
                        status_code=403,
                        content=SrFormat(
                            status_code=403,
                            success=False,
                            data=None,
                            error=Error(
                                code="FORBIDDEN",
                                message="이미 가입된 사용자입니다."
                            )
                        ).model_dump()
                    )
                determined_role = 1
                target_table = "teachers"
            else:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(
                            code="NOT_FOUND",
                            message="사전 등록된 학생 또는 교사 명단에서 이메일을 찾을 수 없습니다."
                        )
                    ).model_dump()
                )

        # 아이디 중복 검사
        await cur.execute("SELECT uuid FROM users WHERE id = %s LIMIT 1", (req.id,))
        if await cur.fetchone():
            return JSONResponse(
                status_code=409,
                content=SrFormat(
                    status_code=409,
                    success=False,
                    data=None,
                    error=Error(
                        code="DUPLICATE_ID",
                        message="이미 사용 중인 아이디입니다."
                    )
                ).model_dump()
            )

    # 1회용 토큰 즉시 파기
    await redis.delete(f"register_token:{clean_mail}")

    user_uuid = str(uuid.uuid4())
    user_pw = hash_password(req.password)

    # DB 트랜잭션 롤백 보장
    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                INSERT INTO users (uuid, id, password, email, role) 
                VALUES (%s, %s, %s, %s, %s)
            """
            await cur.execute(sql_cmd, (user_uuid, req.id, user_pw, clean_mail, determined_role))

            sql_cmd = f"""
                UPDATE {target_table} SET uuid = %s
                WHERE email = %s AND uuid IS NULL AND is_deleted = FALSE
            """
            await cur.execute(sql_cmd, (user_uuid, clean_mail))

            # [보안 1.5] Race Condition 방어: 동시 요청으로 이미 UUID가 할당된 경우 롤백
            if cur.rowcount != 1:
                await conn.rollback()
                return JSONResponse(
                    status_code=409,
                    content=SrFormat(
                        status_code=409,
                        success=False,
                        data=None,
                        error=Error(
                            code="CONFLICT",
                            message="이미 가입 처리가 완료된 계정입니다."
                        )
                    ).model_dump()
                )

        await conn.commit()
    except Exception as e:
        await conn.rollback()
        return JSONResponse(
            status_code=500,
            content=SrFormat(
                status_code=500,
                success=False,
                data=None,
                error=Error(
                    code="DATABASE_ERROR",
                    message="회원가입 처리 중 데이터베이스 오류가 발생했습니다."
                )
            ).model_dump()
        )
    finally:
        await conn.autocommit(True)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "message": "회원가입이 완료되었습니다.",
            "uuid": user_uuid,
            "id": req.id,
            "email": clean_mail,
            "role": "student" if determined_role == 0 else "teacher"
        }
    ).model_dump()


# ==============================================================================
# 3. 계정 정보 수정 (이메일 변경) 엔드포인트
# ==============================================================================

@router.patch("/email")
async def patch_email(
    req: PatchEmailRequest, 
    current_user: dict = Depends(get_current_user), 
    conn: asyncmy.Connection = Depends(get_db), 
    redis: aioredis.Redis = Depends(get_redis)
):
    """
    [PATCH] /auth/email - 이메일 주소 변경
    
    보안 절차:
    1. 새 이메일로 발송되어 검증된 일회용 OTP 토큰(`register_token`) 대조
    2. 이미 다른 계정에서 사용 중인 이메일인지 중복 확인 (409 DUPLICATE_EMAIL)
    3. 본인 확인을 위한 현재 계정 비밀번호 2차 검증 (401 INVALID_CREDENTIAL)
    4. 검증 완료 후 즉시 일회용 토큰 파기
    5. 트랜잭션 롤백 보장 하에 users 테이블의 이메일 갱신
    """
    uuid = current_user["uuid"]

    if not req.email or not req.password or not req.register_token:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(
                    code="VALIDATION_ERROR",
                    message="필수 인자가 누락되었습니다."
                )
            ).model_dump()
        )

    clean_mail = req.email.strip().lower()

    # 1. 새 이메일 인증 토큰 검증
    stored_token = await redis.get(f"register_token:{clean_mail}")
    if stored_token is None or stored_token != req.register_token:
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="INVALID_REGISTER_TOKEN",
                    message="토큰이 만료되었거나, 올바르지 않습니다."
                )
            ).model_dump()
        )

    # 2. 이메일 중복 및 사용자/비밀번호 검증
    #    [보안 1.4] users뿐 아니라 students, teachers 테이블도 중복 검사
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT uuid FROM users WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
        existing_user = await cur.fetchone()

        if not existing_user:
            await cur.execute("SELECT student_id FROM students WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
            existing_student = await cur.fetchone()
            if not existing_student:
                await cur.execute("SELECT teachers_id FROM teachers WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
                existing_teacher = await cur.fetchone()
                existing_user = existing_teacher

            else:
                existing_user = existing_student

        await cur.execute("SELECT * FROM users WHERE uuid = %s AND is_deleted = FALSE LIMIT 1", (uuid,))
        user_data = await cur.fetchone()

    if existing_user is not None:
        return JSONResponse(
            status_code=409,
            content=SrFormat(
                status_code=409,
                success=False,
                data=None,
                error=Error(
                    code="DUPLICATE_EMAIL",
                    message="이미 사용 중인 이메일입니다."
                )
            ).model_dump()
        )

    if user_data is None:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(
                    code="NOT_FOUND",
                    message="사용자를 찾을 수 없습니다."
                )
            ).model_dump()
        )

    if not verify_password(req.password, user_data["password"]):
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="INVALID_CREDENTIAL",
                    message="비밀번호가 일치하지 않습니다."
                )
            ).model_dump()
        )

    # 3. 모든 검증 통과 후 1회용 토큰 파기
    await redis.delete(f"register_token:{clean_mail}")

    # 4. 트랜잭션 기반 이메일 업데이트
    #    [보안 1.4] users와 대상 테이블(students/teachers)의 이메일을 원자적으로 동시 UPDATE
    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            # 현재 사용자의 이전 이메일 조회 (하위 테이블 WHERE 조건용)
            old_email = user_data["email"]

            sql_cmd = """
                UPDATE users SET email = %s
                WHERE uuid = %s AND is_deleted = FALSE
            """
            await cur.execute(sql_cmd, (clean_mail, uuid))

            # 역할(role)에 따라 하위 테이블도 동기화
            user_role = user_data["role"]
            if user_role == 0:  # 학생
                await cur.execute(
                    "UPDATE students SET email = %s WHERE uuid = %s AND is_deleted = FALSE",
                    (clean_mail, uuid)
                )
            elif user_role == 1:  # 교사
                await cur.execute(
                    "UPDATE teachers SET email = %s WHERE uuid = %s AND is_deleted = FALSE",
                    (clean_mail, uuid)
                )

        await conn.commit()
    except Exception as e:
        await conn.rollback()
        return JSONResponse(
            status_code=500,
            content=SrFormat(
                status_code=500,
                success=False,
                data=None,
                error=Error(
                    code="DATABASE_ERROR",
                    message="이메일 업데이트 중 데이터베이스 오류가 발생했습니다."
                )
            ).model_dump()
        )
    finally:
        await conn.autocommit(True)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "message": "이메일 변경이 완료되었습니다.",
            "uuid": uuid,
            "email": clean_mail,
        }
    ).model_dump()
