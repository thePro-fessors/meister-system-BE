"""
routers/auth.py - 인증(Authentication) 및 계정 관리 관련 API 라우터

명세 및 연동 기준:
- Tech_spec.md 1장 (인증 시스템)
- BACKEND_REQUIREMENTS.md 3장 (인증/인가 API 규격)
- TODO.md 1.1, 1.2 (로그인, 로그아웃, 내 정보 조회, 교내 이메일 OTP, 회원가입, 이메일 변경)
"""

import os
import json
import re
import uuid
from typing import Optional, Any
import secrets
from datetime import datetime, timezone, timedelta
import asyncio
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
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse
import bcrypt
import redis.asyncio as aioredis

from core.academic import (
    _YEAR_ID_CACHE,
    clear_year_cache,
    get_current_year,
    get_year_id,
)
from core.email import send_otp_email, validate_email_format
from core.logger import logger
from database import get_db, get_redis
from sr_format import *
from core.security import error_response, normalize_role, create_access_token, get_current_user, get_client_ip, revoke_all_user_tokens

_INVITATION_FALLBACK_LOCK = asyncio.Lock()

_CONSUME_INVITATION_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then
    return nil
end
local data = cjson.decode(raw)
local rem = tonumber(data['remaining_uses'] or 0)
if rem <= 0 then
    redis.call('DEL', KEYS[1])
    redis.call('SREM', KEYS[2], ARGV[1])
    return nil
end
rem = rem - 1
data['remaining_uses'] = rem
if rem <= 0 then
    redis.call('DEL', KEYS[1])
    redis.call('SREM', KEYS[2], ARGV[1])
else
    local ttl = redis.call('TTL', KEYS[1])
    local encoded = cjson.encode(data)
    if ttl and ttl > 0 then
        redis.call('SETEX', KEYS[1], ttl, encoded)
    else
        redis.call('SET', KEYS[1], encoded)
    end
end
return raw
"""

_REFUND_INVITATION_LUA = """
local raw = redis.call('GET', KEYS[1])
if raw then
    local data = cjson.decode(raw)
    local max_uses = tonumber(data['max_uses'] or 999999)
    local rem = tonumber(data['remaining_uses'] or 0) + 1
    if rem > max_uses then
        rem = max_uses
    end
    data['remaining_uses'] = rem
    local ttl = redis.call('TTL', KEYS[1])
    local encoded = cjson.encode(data)
    if ttl and ttl > 0 then
        redis.call('SETEX', KEYS[1], ttl, encoded)
    else
        redis.call('SET', KEYS[1], encoded)
    end
    redis.call('SADD', KEYS[2], ARGV[1])
    return 1
else
    local fallback_ttl = tonumber(ARGV[3] or 0)
    if fallback_ttl > 0 and ARGV[2] and ARGV[2] ~= '' then
        local data = cjson.decode(ARGV[2])
        data['remaining_uses'] = 1
        local encoded = cjson.encode(data)
        redis.call('SETEX', KEYS[1], fallback_ttl, encoded)
        redis.call('SADD', KEYS[2], ARGV[1])
        return 1
    end
    return 0
end
"""


async def _consume_invitation_code_atomically(redis: Any, code: str) -> Optional[dict]:
    """Redis Lua 스크립트를 사용하여 초대코드 잔여 횟수 확인 및 차감을 단일 원자적 연산으로 처리합니다."""
    clean_code = (code or "").strip().upper()
    if not clean_code:
        return None
    inv_key = f"invitation_code:{clean_code}"
    active_set_key = "invitation_codes:active"
    if hasattr(redis, "eval"):
        try:
            raw_res = await redis.eval(_CONSUME_INVITATION_LUA, 2, inv_key, active_set_key, clean_code)
            if not raw_res:
                return None
            val_str = raw_res.decode("utf-8") if isinstance(raw_res, bytes) else str(raw_res)
            return json.loads(val_str)
        except (AttributeError, NotImplementedError):
            pass
        except Exception as e:
            logger.warning(f"[INVITATION_LUA_FALLBACK] eval fallback due to: {e}")

    async with _INVITATION_FALLBACK_LOCK:
        raw_inv = await redis.get(inv_key)
        if not raw_inv:
            return None
        try:
            val_str = raw_inv.decode("utf-8") if isinstance(raw_inv, bytes) else str(raw_inv)
            inv_obj = json.loads(val_str)
            rem = int(inv_obj.get("remaining_uses", 0))
            if rem <= 0:
                await redis.delete(inv_key)
                if hasattr(redis, "srem"):
                    await redis.srem(active_set_key, clean_code)
                return None
            original_obj = dict(inv_obj)
            inv_obj["remaining_uses"] = rem - 1
            if inv_obj["remaining_uses"] <= 0:
                await redis.delete(inv_key)
                if hasattr(redis, "srem"):
                    await redis.srem(active_set_key, clean_code)
            else:
                ttl = await redis.ttl(inv_key) if hasattr(redis, "ttl") else 86400
                ttl_sec = ttl if isinstance(ttl, int) and ttl > 0 else 86400
                await redis.setex(inv_key, ttl_sec, json.dumps(inv_obj, ensure_ascii=False))
            return original_obj
        except Exception:
            return None


async def _refund_invitation_code_atomically(
    redis: Any, code: str, consumed_inv: Optional[dict] = None
) -> bool:
    """초대코드 사용 롤백 시 잔여 횟수를 원자적으로 +1 환불하고 TTL 및 활성 목록을 복원합니다."""
    clean_code = (code or "").strip().upper()
    if not clean_code or not consumed_inv:
        return False
    inv_key = f"invitation_code:{clean_code}"
    active_set_key = "invitation_codes:active"

    fallback_ttl = 86400
    try:
        exp_str = consumed_inv.get("expires_at")
        if exp_str:
            exp_dt = datetime.fromisoformat(exp_str)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            diff = int((exp_dt - datetime.now(timezone.utc)).total_seconds())
            if diff > 0:
                fallback_ttl = diff
            else:
                return False  # 이미 만료된 코드는 환불 복원하지 않음
    except Exception:
        fallback_ttl = 86400

    fallback_json = json.dumps(consumed_inv, ensure_ascii=False)

    if hasattr(redis, "eval"):
        try:
            res = await redis.eval(
                _REFUND_INVITATION_LUA, 2, inv_key, active_set_key, clean_code, fallback_json, fallback_ttl
            )
            return bool(res)
        except (AttributeError, NotImplementedError):
            pass
        except Exception as e:
            logger.warning(f"[INVITATION_REFUND_LUA_FALLBACK] eval fallback due to: {e}")

    async with _INVITATION_FALLBACK_LOCK:
        try:
            raw_inv = await redis.get(inv_key)
            if raw_inv:
                val_str = raw_inv.decode("utf-8") if isinstance(raw_inv, bytes) else str(raw_inv)
                inv_obj = json.loads(val_str)
                max_u = int(inv_obj.get("max_uses", 999999))
                inv_obj["remaining_uses"] = min(int(inv_obj.get("remaining_uses", 0)) + 1, max_u)
                ttl = await redis.ttl(inv_key) if hasattr(redis, "ttl") else fallback_ttl
                ttl_sec = ttl if isinstance(ttl, int) and ttl > 0 else fallback_ttl
                await redis.setex(inv_key, ttl_sec, json.dumps(inv_obj, ensure_ascii=False))
            else:
                restored = dict(consumed_inv)
                restored["remaining_uses"] = 1
                await redis.setex(inv_key, fallback_ttl, json.dumps(restored, ensure_ascii=False))
            if hasattr(redis, "sadd"):
                await redis.sadd(active_set_key, clean_code)
            return True
        except Exception:
            return False

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


# ==============================================================================
# 비밀번호 암호화 및 유효성 검증 유틸리티 (AUTH-03: bcrypt 72바이트 상한 및 to_thread 비동기 처리)
# ==============================================================================

def hash_password(password: str) -> str:
    """비밀번호를 bcrypt(rounds=12)로 안전하게 단방향 솔팅 해싱합니다. (최대 72바이트 초과 차단)"""
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > 72:
        raise ValueError("비밀번호는 최대 72바이트를 초과할 수 없습니다.")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def verify_password_sync(plain_password: str, hashed_password: str) -> bool:
    """평문 비밀번호와 해시 비밀번호를 상시 일정한 시간(Constant Time)으로 동기 대조합니다."""
    try:
        pw_bytes = plain_password.encode("utf-8")
        if len(pw_bytes) > 72:
            return False
        return bcrypt.checkpw(pw_bytes, hashed_password.encode("utf-8"))
    except Exception:
        return False


async def verify_password(plain_password: str, hashed_password: str) -> bool:
    """CPU-bound bcrypt 검증 작업을 별도 스레드풀로 오프로딩하여 이벤트 루프 블로킹을 방지합니다."""
    return await asyncio.to_thread(verify_password_sync, plain_password, hashed_password)


# ==============================================================================
# DTO 스키마 정의 (AUTH-01: 목적별 OTP 및 이메일 변경 스키마 확장)
# ==============================================================================

class LoginRequest(BaseModel):
    username: Optional[str] = None
    id: Optional[str] = None
    password: str

    model_config = {"populate_by_name": True}


class SendOtpRequest(BaseModel):
    student_grade: Optional[int] = None
    student_class: Optional[int] = None
    student_number: Optional[int] = None  # 교사 요청 건의 경우 위 3개는 무시됨
    is_it_student: bool = False
    email: str
    purpose: str = "register"  # "register" 또는 "change_email"
    admin_code: Optional[str] = Field(default=None, alias="adminCode")

    model_config = {"populate_by_name": True}


class VerifyOtpRequest(BaseModel):
    email: str
    verify_code: str
    purpose: str = "register"  # "register" 또는 "change_email"


class RegisterRequest(BaseModel):
    email: str
    password: str
    id: str
    register_token: Optional[str] = Field(default=None, alias="registerToken")
    role: Optional[int] = None  # 클라이언트가 임의로 보내더라도 서버 판별값으로 덮어씌워 원천 무시됨
    admin_code: Optional[str] = Field(default=None, alias="adminCode")

    model_config = {"populate_by_name": True}


class PatchEmailRequest(BaseModel):
    email: str
    password: str
    register_token: Optional[str] = None
    email_change_token: Optional[str] = None
    registerToken: Optional[str] = None
    emailChangeToken: Optional[str] = None


# 비밀번호 복잡도 정책: 3종 이상(영문, 숫자, 특수문자) 조합 시 최소 8자, 또는 2종 이상 조합 시 최소 10자, 최대 72바이트 (SECURITY_AND_AUDIT.md 1.5, TODO.md 6.1 P2)
def validate_password_complexity(password: str) -> bool:
    """
    비밀번호 복잡도 및 유효성을 검증합니다.
    - 최소 8자 이상, 최대 72바이트
    - 영문자, 숫자, 특수문자 중:
      * 3종 이상 조합 시: 최소 8자 이상
      * 2종 이상 조합 시: 최소 10자 이상
    """
    if not password:
        return False
    pw_bytes = password.encode("utf-8")
    if len(password) < 8 or len(pw_bytes) > 72:
        return False

    has_letter = bool(re.search(r"[A-Za-z]", password))
    has_digit = bool(re.search(r"\d", password))
    has_special = bool(re.search(r"[!@#$%^&*()_\-+=~`[\]{}|\\:;\"'<>,.?/]", password))

    types_count = sum([has_letter, has_digit, has_special])

    if len(password) >= 10 and types_count >= 2:
        return True
    if len(password) >= 8 and types_count >= 3:
        return True
    return False


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
    - [성능 2.2 / P1] 단일 LEFT JOIN 복합 쿼리로 4회 순차 왕복을 1회 왕복으로 단축
    - [보안] 계정별 5회 연속 실패 시 10분간 로그인 차단 (Brute-Force 방어)
    - [보안] IP별 분당 20회 요청 제한 (DoS 방어, Nginx/프록시 호환)
    """
    client_ip = get_client_ip(request)

    login_id = (req.username or req.id or "").strip()
    if not login_id:
        return error_response(400, "VALIDATION_ERROR", "아이디(username 또는 id)를 입력해주세요.")
    req.username = login_id

    # ── [보안 1.1] IP별 분당 20회 요청 제한 (DoS 방어) ──
    ip_limit_key = f"ip_limit:login:{client_ip}"
    ip_requests = await redis.incr(ip_limit_key)
    if ip_requests == 1:
        await redis.expire(ip_limit_key, 60)
    if ip_requests > 20:
        logger.warning(f"[LOGIN_IP_BLOCKED] IP={client_ip} 분당 20회 초과")
        return error_response(429, "TOO_MANY_REQUESTS", "너무 많은 로그인 요청이 발생했습니다. 잠시 후 다시 시도해주세요.")

    # ── [보안 1.1] 계정+IP 복합 연속 실패 잠금 확인 (타 IP로부터의 계정 잠금 DoS 방어) ──
    account_lock_key = f"login_failures:{req.username}:{client_ip}"
    legacy_lock_key = f"login_failures:{req.username}"
    account_locked = await redis.get(account_lock_key) or await redis.get(legacy_lock_key)
    if account_locked and int(account_locked) >= 5:
        lock_ttl = await redis.ttl(account_lock_key)
        if not isinstance(lock_ttl, int) or lock_ttl < 0:
            lock_ttl = await redis.ttl(legacy_lock_key)
        return JSONResponse(
            status_code=429,
            content=SrFormat(
                status_code=429,
                success=False,
                data={"remainingSeconds": max(0, lock_ttl if isinstance(lock_ttl, int) else 0)},
                error=Error(
                    code="ACCOUNT_LOCKED",
                    message="로그인 실패 횟수를 초과하였습니다. 잠시 후 다시 시도해주세요."
                )
            ).model_dump()
        )

    current_yr = await get_current_year(datetime.now())

    # [성능 2.2 / P1] 단일 JOIN 쿼리로 4회 순차 DB 조회를 1회 왕복으로 통합
    user = None
    try:
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                SELECT 
                    u.uuid, u.id, u.password, u.role, u.email,
                    st.student_id, st.name AS student_name,
                    sar.grade AS student_grade, sar.class AS student_class, sar.number AS student_number,
                    t.teachers_id, t.name AS teacher_name, t.grade AS teacher_grade, t.class AS teacher_class,
                    t.position AS teacher_position, t.department AS teacher_department, t.subject AS teacher_subject
                FROM users u
                LEFT JOIN students st ON st.uuid = u.uuid AND st.is_deleted = FALSE
                LEFT JOIN academic_years ay ON ay.year = %s
                LEFT JOIN student_academic_records sar ON sar.student_id = st.student_id AND sar.year_id = ay.year_id
                LEFT JOIN teachers t ON t.uuid = u.uuid AND t.is_deleted = FALSE
                WHERE u.id = %s AND u.is_deleted = FALSE
                LIMIT 1
            """
            await cur.execute(sql_cmd, (current_yr, req.username))
            user = await cur.fetchone()
    except Exception as db_err:
        logger.warning(f"[LOGIN_COMPLEX_QUERY_FAIL] fallback to simple users query: {db_err}")
        try:
            async with conn.cursor(cursor=DictCursor) as cur:
                sql_cmd = """
                    SELECT uuid, id, password, role, email
                    FROM users
                    WHERE id = %s AND is_deleted = FALSE
                """
                await cur.execute(sql_cmd, (req.username,))
                user = await cur.fetchone()
        except Exception as fatal_err:
            logger.error(f"[LOGIN_DB_FATAL] DB connection error during login: {fatal_err}")
            return error_response(500, "INTERNAL_SERVER_ERROR", "데이터베이스 통신 오류가 발생했습니다.")

    # 계정이 없을 경우 더미 해시 검증을 수행하여 유저 유무에 따른 시간차 공격 차단 (AUTH-03)
    hash_data = user["password"] if user else DUMMY_HASH
    hash_valid = await verify_password(req.password, hash_data)

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

    role_map = {0: "student", 1: "teacher", 2: "admin"}
    role_str = role_map.get(user["role"], "student")
    
    student_id_val = user.get("student_id")
    student_name = user.get("student_name")
    student_grade = user.get("student_grade")
    student_class = user.get("student_class")
    student_number = user.get("student_number")

    teacher_id_val = user.get("teachers_id")
    teacher_name = user.get("teacher_name")
    teacher_grade = user.get("teacher_grade")
    teacher_class = user.get("teacher_class")

    user_name = student_name if user["role"] == 0 and student_name else (
        teacher_name if (user["role"] in (1, 2)) and teacher_name else user["id"]
    )
    homeroom_str = f"{teacher_grade}-{teacher_class}" if (user["role"] in (1, 2)) and teacher_grade and teacher_class else None
    teacher_pos = (user.get("teacher_position") or "").strip() if user.get("teacher_position") else None
    teacher_dept = (user.get("teacher_department") or "").strip() if user.get("teacher_department") else None
    teacher_subj = (user.get("teacher_subject") or "").strip() if user.get("teacher_subject") else None
    teacher_duty = None
    teacher_scope = None
    if user["role"] in (1, 2):
        if teacher_pos == "학년부장":
            teacher_duty = "grade_head"
            teacher_scope = {"grade": teacher_grade} if teacher_grade else None
        elif teacher_pos == "부서부장" and teacher_dept in ("마이스터부", "교무기획부"):
            teacher_duty = "vice_principal"
            teacher_scope = {"schoolWide": True}
        elif teacher_pos in ("담임", "정담임", "임시담임"):
            teacher_duty = "homeroom"
            teacher_scope = {"classrooms": [homeroom_str]} if homeroom_str else None
        elif teacher_pos == "부담임":
            teacher_duty = "assistant_homeroom"
            teacher_scope = {"classrooms": [homeroom_str]} if homeroom_str else None
        elif teacher_pos:
            teacher_duty = "subject_teacher"

    token = create_access_token(
        data={
            "sub": user["uuid"],
            "id": user["id"],
            "role": role_str,
        }
    )

    try:
        from core.audit import record_audit_log
        await record_audit_log(
            conn=conn,
            actor={"uuid": user["uuid"], "id": user["id"], "name": user_name, "role": role_str},
            action_type="LOGIN",
            target_type="AUTH",
            target_id=user["id"],
            target_name=user_name,
            ip_address=client_ip,
            details={"role": role_str},
        )
    except Exception as audit_err:
        logger.warning(f"Failed to record login audit log: {audit_err}")

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
                "studentId": student_id_val if user["role"] == 0 else None,
                "grade": student_grade if user["role"] == 0 else teacher_grade,
                "classNo": student_class if user["role"] == 0 else teacher_class,
                "number": student_number if user["role"] == 0 else None,
                "teacherId": teacher_id_val if (user["role"] in (1, 2)) else None,
                "homeroom": homeroom_str,
                "position": teacher_pos,
                "department": teacher_dept,
                "subject": teacher_subj,
                "teacherDuty": teacher_duty,
                "teacherScope": teacher_scope,
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
        return error_response(400, "INVALID_TOKEN", "토큰 정보가 올바르지 않습니다.")

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
            return error_response(404, "NOT_FOUND", "유저를 찾을 수 없습니다.")

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
    elif user["role"] == 1 or user["role"] == 2:
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                SELECT * FROM teachers
                WHERE uuid = %s AND is_deleted = FALSE
                LIMIT 1
            """
            await cur.execute(sql_cmd, (user_uuid,))
            teachers = await cur.fetchone()

    user_name = students["name"] if students else (teachers["name"] if teachers else user["id"])

    t_pos = (teachers.get("position") or "").strip() if teachers and teachers.get("position") else None
    t_dept = (teachers.get("department") or "").strip() if teachers and teachers.get("department") else None
    t_subj = (teachers.get("subject") or "").strip() if teachers and teachers.get("subject") else None
    t_grade = teachers.get("grade") if teachers else None
    t_class = teachers.get("class") if teachers else None
    t_duty = None
    t_scope = None
    if teachers:
        if t_pos == "학년부장":
            t_duty = "grade_head"
            t_scope = {"grade": t_grade} if t_grade else None
        elif t_pos == "부서부장" and t_dept in ("마이스터부", "교무기획부"):
            t_duty = "vice_principal"
            t_scope = {"schoolWide": True}
        elif t_pos in ("담임", "정담임", "임시담임"):
            t_duty = "homeroom"
            t_scope = {"classrooms": [f"{t_grade}-{t_class}"]} if (t_grade and t_class) else None
        elif t_pos == "부담임":
            t_duty = "assistant_homeroom"
            t_scope = {"classrooms": [f"{t_grade}-{t_class}"]} if (t_grade and t_class) else None
        elif t_pos:
            t_duty = "subject_teacher"

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
                "grade": students["grade"] if students else t_grade,
                "classNo": students["class"] if students else t_class,
                "number": students["number"] if students else None,
                "teacherId": teachers["teachers_id"] if teachers else None,
                "homeroom": f"{teachers['grade']}-{teachers['class']}" if teachers and teachers['grade'] and teachers['class'] else None,
                "position": t_pos,
                "department": t_dept,
                "subject": t_subj,
                "teacherDuty": t_duty,
                "teacherScope": t_scope,
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
    clean_mail = (req.email or "").strip().lower()
    if not clean_mail or not validate_email_format(clean_mail):
        return error_response(400, "VALIDATION_ERROR", "올바른 이메일 형식이 아닙니다.")

    client_ip = get_client_ip(request)

    # ── [보안 1.2] IP별 1시간당 10회 OTP 발송 제한 (Distributed Email Bombing 방어) ──
    ip_otp_key = f"ip_otp_limit:{client_ip}"
    ip_otp_count = await redis.incr(ip_otp_key)
    if ip_otp_count == 1:
        await redis.expire(ip_otp_key, 3600)
    if ip_otp_count > 10:
        logger.warning(f"[OTP_IP_BLOCKED] IP={client_ip} 시간당 10회 초과")
        return error_response(429, "TOO_MANY_REQUESTS", "해당 IP에서 너무 많은 인증 요청이 발생했습니다. 잠시 후 다시 시도해주세요.")

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

    # 목적별 분기 처리 (AUTH-01: 회원가입 vs 이메일 변경 OTP 분리 및 관리자 가입 코드 지원)
    expected_admin_code = (os.getenv("ADMIN_REGISTRATION_CODE") or "").strip()

    if req.admin_code is not None and req.admin_code.strip():
        input_code = req.admin_code.strip()
        is_env_match = bool(expected_admin_code) and secrets.compare_digest(input_code, expected_admin_code)
        invitation_data = None

        if not is_env_match:
            raw_inv = await redis.get(f"invitation_code:{input_code.upper()}")
            if raw_inv:
                try:
                    val_str = raw_inv.decode("utf-8") if isinstance(raw_inv, bytes) else str(raw_inv)
                    parsed = json.loads(val_str)
                    if parsed.get("remaining_uses", 0) > 0:
                        invitation_data = parsed
                except Exception:
                    invitation_data = None

        if not is_env_match and not invitation_data:
            return error_response(403, "FORBIDDEN", "가입 보안 코드 또는 초대 코드가 올바르지 않거나 만료되었습니다.")

        # 이미 users 테이블에 존재하는 이메일인지 중복 검사 (중복 시 409 DUPLICATE_EMAIL)
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("SELECT uuid FROM users WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
            ex_u = await cur.fetchone()

        if ex_u:
            return error_response(409, "DUPLICATE_EMAIL", "이미 가입된 관리자 또는 사용자 이메일입니다.")

        user_name = "관리자" if is_env_match else f"{invitation_data.get('role', '초대')} 가입자"
        otp_key = f"otp:{clean_mail}"
        if is_env_match or (invitation_data and invitation_data.get("target_role") == 2):
            await redis.setex(f"otp:admin_reg:{clean_mail}", 300, "1")
        if invitation_data:
            await redis.setex(f"otp:invite_meta:{clean_mail}", 300, json.dumps(invitation_data, ensure_ascii=False))

    elif req.purpose == "change_email":
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("SELECT uuid FROM users WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
            ex_u = await cur.fetchone()
            if not ex_u:
                await cur.execute("SELECT student_id FROM students WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
                ex_u = await cur.fetchone()
            if not ex_u:
                await cur.execute("SELECT teachers_id FROM teachers WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
                ex_u = await cur.fetchone()

        if ex_u:
            return error_response(409, "DUPLICATE_EMAIL", "이미 사용 중이거나 등록된 이메일 주소입니다.")
        user_name = "사용자"
        otp_key = f"otp:change_email:{clean_mail}"
    else:
        # 일반 신규 가입 (학생 / 교사)
        await redis.delete(f"otp:admin_reg:{clean_mail}")
        # 사전 등록 명단 일치 여부 확인 (신규 가입)
        if not req.is_it_student:
            async with conn.cursor(cursor=DictCursor) as cur:
                sql_cmd = """
                SELECT * FROM teachers t
                WHERE email = %s AND is_deleted = FALSE
                LIMIT 1
                """
                await cur.execute(sql_cmd, (clean_mail,))
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
                await cur.execute(sql_cmd, (clean_mail, year_id, req.student_grade, req.student_class, req.student_number))
                datas = await cur.fetchone()

        if not datas:
            return error_response(404, "NOT_FOUND", "유저를 찾을 수 없거나, 요청이 잘못 되었습니다.")
        if datas.get("uuid") is not None:
            return error_response(403, "FORBIDDEN", "이미 가입한 사용자입니다.")
        user_name = datas.get("name", "사용자")
        otp_key = f"otp:{clean_mail}"

    # TRNG(암호학적 난수 생성기)를 이용한 6자리 균등 난수 생성
    otp_codes = f"{secrets.randbelow(1000000):06d}"
    await redis.setex(otp_key, 300, otp_codes)

    background_tasks.add_task(
        send_otp_email,
        to_email=clean_mail,
        otp_code=otp_codes,
        user_name=user_name,
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
    clean_mail = (req.email or "").strip().lower()
    if not clean_mail or not req.verify_code or not validate_email_format(clean_mail):
        return error_response(400, "VALIDATION_ERROR", "요청이 올바르지 않습니다.")

    clean_code = req.verify_code.strip()

    if req.purpose == "change_email":
        attempt_key = f"otp_attempts:change_email:{clean_mail}"
        otp_key = f"otp:change_email:{clean_mail}"
    else:
        attempt_key = f"otp_attempts:{clean_mail}"
        otp_key = f"otp:{clean_mail}"

    # [보안 1.8] 원자적 시도 횟수 선증가 (Race Window 방어)
    attempts = await redis.incr(attempt_key)
    if attempts == 1:
        await redis.expire(attempt_key, 300)

    # 5회 이상 실패 시 무차별 대입 방어를 위해 즉시 폭파
    if attempts > 5:
        await redis.delete(otp_key)
        await redis.delete(attempt_key)
        return error_response(429, "TOO_MANY_REQUESTS", "인증번호 입력 횟수를 초과하였습니다.")

    stored_otp = await redis.get(otp_key)

    if stored_otp is None:
        return error_response(400, "OTP_EXPIRED", "인증번호가 만료되었거나, 발송되지 않았습니다.")

    if stored_otp != clean_code:
        remain = max(0, 5 - attempts)
        times = await redis.ttl(otp_key)
        if attempts >= 5:
            await redis.delete(otp_key)
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data={
                        "remainingAttempts": 0,
                        "remainingSeconds": 0,
                    },
                    error=Error(
                        code="OTP_ATTEMPTS_EXCEEDED",
                        message="인증번호 입력 가능 횟수(5회)를 초과하였습니다. 새로운 인증번호를 발급받아주세요."
                    )
                ).model_dump()
            )
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

    # 안전한 32바이트 Opaque 토큰 생성 (10분 유효, AUTH-01)
    token_val = secrets.token_urlsafe(32)
    if req.purpose == "change_email":
        await redis.setex(f"email_change_token:{clean_mail}", 600, token_val)
        await redis.setex(f"register_token:{clean_mail}", 600, token_val)
        return SrFormat(
            status_code=200,
            success=True,
            data={
                "message": "Verify OTP codes.",
                "email": clean_mail,
                "emailChangeToken": token_val,
                "registerToken": token_val
            }
        ).model_dump()
    else:
        if await redis.get(f"otp:admin_reg:{clean_mail}"):
            await redis.setex(f"otp:admin_reg:{clean_mail}", 600, "1")
        invite_meta = await redis.get(f"otp:invite_meta:{clean_mail}")
        if invite_meta:
            await redis.setex(f"otp:invite_meta:{clean_mail}", 600, invite_meta)
        await redis.setex(f"register_token:{clean_mail}", 600, token_val)
        return SrFormat(
            status_code=200,
            success=True,
            data={
                "message": "Verify OTP codes.",
                "email": clean_mail,
                "registerToken": token_val
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
    1. 비밀번호 복잡도 검증 (영문, 숫자, 특수문자 조합 8자 이상 또는 2종 10자 이상)
    2. Redis에 보관된 `register_token`과 대조하여 통과 후 즉시 파기 (재사용 차단)
    3. 서버 기반 권한(Role) 확정: 클라이언트의 role 입력을 완전히 무시하고 사전 명단 기반 자동 지정
    4. 아이디 중복 검사 (409 DUPLICATE_ID)
    5. 트랜잭션 원자성(ACID) 보장: users 레코드 생성과 students/teachers uuid 업데이트 실패 시 완전 롤백
    """
    clean_mail = (req.email or "").strip().lower()
    if not clean_mail or not req.password or not req.id or not req.register_token or not validate_email_format(clean_mail):
        return error_response(400, "VALIDATION_ERROR", "올바른 이메일 형식이 아니거나 필수 인자가 누락되었습니다.")

    # 숫자 문자열 로그인 ID 금지 (students.student_id 정수 PK와의 식별자 충돌 원천 방어)
    if req.id.strip().isdigit():
        return error_response(400, "VALIDATION_ERROR", "로그인 아이디는 숫자로만 구성할 수 없습니다. 영문자를 포함해주세요.")

    # [보안 1.5] 비밀번호 복잡도 정규식/정책 검증
    if not validate_password_complexity(req.password):
        return error_response(400, "WEAK_PASSWORD", "비밀번호는 영문자, 숫자, 특수문자 중 3종 이상 조합 시 8자 이상, 2종 이상 조합 시 10자 이상이어야 합니다.")

    stored_token = await redis.getdel(f"register_token:{clean_mail}")
    if stored_token is None or stored_token != req.register_token:
        return error_response(401, "INVALID_REGISTER_TOKEN", "토큰이 만료되었거나, 올바르지 않습니다.")

    # 관리자/초대코드 가입 판별 (보안 코드 직접 제시 또는 Redis OTP 검증 플래그 확인)
    expected_admin_code = (os.getenv("ADMIN_REGISTRATION_CODE") or "").strip()
    is_admin = False
    invitation_data = None
    code_to_consume = None

    raw_invite_meta = await redis.get(f"otp:invite_meta:{clean_mail}")
    if raw_invite_meta:
        try:
            val_str = raw_invite_meta.decode("utf-8") if isinstance(raw_invite_meta, bytes) else str(raw_invite_meta)
            invitation_data = json.loads(val_str)
            code_to_consume = invitation_data.get("code")
        except Exception:
            invitation_data = None

    if req.admin_code is not None and req.admin_code.strip():
        input_code = req.admin_code.strip()
        if bool(expected_admin_code) and secrets.compare_digest(input_code, expected_admin_code):
            is_admin = True
        else:
            raw_inv = await redis.get(f"invitation_code:{input_code.upper()}")
            if raw_inv:
                try:
                    val_str = raw_inv.decode("utf-8") if isinstance(raw_inv, bytes) else str(raw_inv)
                    parsed = json.loads(val_str)
                    if parsed.get("remaining_uses", 0) > 0:
                        invitation_data = parsed
                        code_to_consume = parsed.get("code")
                except Exception:
                    pass
            if not is_admin and not invitation_data:
                return error_response(403, "FORBIDDEN", "관리자 가입 보안 코드 또는 초대 코드가 올바르지 않습니다.")

    if not is_admin:
        admin_flag = await redis.get(f"otp:admin_reg:{clean_mail}")
        if admin_flag:
            is_admin = True

    determined_role = None
    target_table = None

    if is_admin or (invitation_data and invitation_data.get("target_role") == 2):
        determined_role = 2
        async with conn.cursor(cursor=DictCursor) as cur:
            # 이메일 중복 검사
            await cur.execute("SELECT uuid FROM users WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
            if await cur.fetchone():
                return error_response(409, "DUPLICATE_EMAIL", "이미 가입된 이메일 주소입니다.")

            # 아이디 중복 검사
            await cur.execute("SELECT uuid FROM users WHERE id = %s LIMIT 1", (req.id,))
            if await cur.fetchone():
                return error_response(409, "DUPLICATE_ID", "이미 사용 중인 아이디입니다.")

            # 교사 테이블에 동일 이메일이 존재하는지 확인 (있으면 uuid 연동)
            await cur.execute("SELECT teachers_id FROM teachers WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
            teacher_row = await cur.fetchone()
            if teacher_row:
                target_table = "teachers"

    elif invitation_data:
        # 학생 / 교사 동적 초대 코드 기반 가입
        inv_target_role = invitation_data.get("target_role", 0)
        determined_role = inv_target_role

        async with conn.cursor(cursor=DictCursor) as cur:
            # 이메일 중복 검사
            await cur.execute("SELECT uuid FROM users WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
            if await cur.fetchone():
                return error_response(409, "DUPLICATE_EMAIL", "이미 가입된 이메일 주소입니다.")

            # 아이디 중복 검사
            await cur.execute("SELECT uuid FROM users WHERE id = %s LIMIT 1", (req.id,))
            if await cur.fetchone():
                return error_response(409, "DUPLICATE_ID", "이미 사용 중인 아이디입니다.")

            if inv_target_role == 1:
                target_table = "teachers"
                await cur.execute("SELECT teachers_id FROM teachers WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
                teacher_row = await cur.fetchone()
                if not teacher_row:
                    await cur.execute(
                        "INSERT INTO teachers (name, email, is_deleted) VALUES (%s, %s, FALSE)",
                        (req.id, clean_mail)
                    )
            else:
                target_table = "students"
                await cur.execute("SELECT student_id FROM students WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
                student_row = await cur.fetchone()
                if not student_row:
                    await cur.execute(
                        "INSERT INTO students (name, email, is_deleted) VALUES (%s, %s, FALSE)",
                        (req.id, clean_mail)
                    )
    else:
        # 학생 / 교사 서버 기반 권한(Role) 확정 판별 (권한 상승 위조 원천 방어)
        async with conn.cursor(cursor=DictCursor) as cur:
            # 학생 사전 등록 명단 확인
            await cur.execute("SELECT student_id, uuid FROM students WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
            student_row = await cur.fetchone()
            if student_row:
                if student_row.get("uuid") is not None:
                    return error_response(403, "FORBIDDEN", "이미 가입된 사용자입니다.")
                determined_role = 0
                target_table = "students"
            else:
                # 교사 사전 등록 명단 확인
                await cur.execute("SELECT teachers_id, uuid FROM teachers WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
                teacher_row = await cur.fetchone()
                if teacher_row:
                    if teacher_row.get("uuid") is not None:
                        return error_response(403, "FORBIDDEN", "이미 가입된 사용자입니다.")
                    determined_role = 1
                    target_table = "teachers"
                else:
                    return error_response(404, "NOT_FOUND", "사전 등록된 학생 또는 교사 명단에서 이메일을 찾을 수 없습니다.")

            # 이메일 중복 검사
            await cur.execute("SELECT uuid FROM users WHERE email = %s AND is_deleted = FALSE LIMIT 1", (clean_mail,))
            if await cur.fetchone():
                return error_response(409, "DUPLICATE_EMAIL", "이미 가입된 이메일 주소입니다.")

            # 아이디 중복 검사
            await cur.execute("SELECT uuid FROM users WHERE id = %s LIMIT 1", (req.id,))
            if await cur.fetchone():
                return error_response(409, "DUPLICATE_ID", "이미 사용 중인 아이디입니다.")

    user_uuid = str(uuid.uuid4())
    user_pw = hash_password(req.password)

    # 초대 코드가 사용된 경우 DB 커밋 직전 원자적으로 횟수 차감 (동시 가입 TOCTOU 차단)
    consumed_inv = None
    if code_to_consume:
        consumed_inv = await _consume_invitation_code_atomically(redis, code_to_consume)
        if not consumed_inv:
            return error_response(403, "FORBIDDEN", "초대 코드가 만료되었거나 사용 가능 횟수가 모두 소진되었습니다.")

    # DB 트랜잭션 롤백 보장
    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                INSERT INTO users (uuid, id, password, email, role) 
                VALUES (%s, %s, %s, %s, %s)
            """
            await cur.execute(sql_cmd, (user_uuid, req.id, user_pw, clean_mail, determined_role))

            if target_table:
                sql_cmd = f"""
                    UPDATE {target_table} SET uuid = %s
                    WHERE email = %s AND uuid IS NULL AND is_deleted = FALSE
                """
                await cur.execute(sql_cmd, (user_uuid, clean_mail))

                # [보안 1.5] Race Condition 방어: 학생/교사 가입 시 동시 요청으로 이미 UUID가 할당된 경우 롤백
                if not is_admin and cur.rowcount != 1:
                    await conn.rollback()
                    if code_to_consume and consumed_inv:
                        await _refund_invitation_code_atomically(redis, code_to_consume, consumed_inv)
                    return error_response(409, "CONFLICT", "이미 가입 처리가 완료된 계정입니다.")

        await conn.commit()
        await redis.delete(f"otp:admin_reg:{clean_mail}")
        if code_to_consume:
            await redis.delete(f"otp:invite_meta:{clean_mail}")
    except Exception as e:
        await conn.rollback()
        if code_to_consume and consumed_inv:
            await _refund_invitation_code_atomically(redis, code_to_consume, consumed_inv)
        return error_response(500, "DATABASE_ERROR", "회원가입 처리 중 데이터베이스 오류가 발생했습니다.")
    finally:
        await conn.autocommit(True)

    role_str = "admin" if determined_role == 2 else ("teacher" if determined_role == 1 else "student")

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "message": "회원가입이 완료되었습니다.",
            "uuid": user_uuid,
            "id": req.id,
            "email": clean_mail,
            "role": role_str
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

    token_val = req.email_change_token or req.emailChangeToken or req.register_token or req.registerToken
    clean_mail = (req.email or "").strip().lower()
    if not clean_mail or not req.password or not token_val or not validate_email_format(clean_mail):
        return error_response(400, "VALIDATION_ERROR", "올바른 이메일 형식이 아니거나 필수 인자가 누락되었습니다.")

    # 1. 새 이메일 인증 토큰 원자적 검증 및 소비 (AUTH-01, AUTH-02)
    stored_token = await redis.getdel(f"email_change_token:{clean_mail}")
    if not stored_token:
        stored_token = await redis.getdel(f"register_token:{clean_mail}")
    if stored_token is None or stored_token != token_val:
        return error_response(401, "INVALID_REGISTER_TOKEN", "토큰이 만료되었거나, 올바르지 않습니다.")

    # 2. 이메일 중복 및 사용자/비밀번호 검증
    #    [보안 1.4] users뿐 아니라 students, teachers 테이블도 중복 검사
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT uuid FROM users WHERE email = %s AND uuid != %s AND is_deleted = FALSE LIMIT 1", (clean_mail, uuid))
        existing_user = await cur.fetchone()

        if not existing_user:
            await cur.execute("SELECT student_id FROM students WHERE email = %s AND (uuid IS NULL OR uuid != %s) AND is_deleted = FALSE LIMIT 1", (clean_mail, uuid))
            existing_student = await cur.fetchone()
            if not existing_student:
                await cur.execute("SELECT teachers_id FROM teachers WHERE email = %s AND (uuid IS NULL OR uuid != %s) AND is_deleted = FALSE LIMIT 1", (clean_mail, uuid))
                existing_teacher = await cur.fetchone()
                existing_user = existing_teacher
            else:
                existing_user = existing_student

        await cur.execute("SELECT * FROM users WHERE uuid = %s AND is_deleted = FALSE LIMIT 1", (uuid,))
        user_data = await cur.fetchone()

    if existing_user is not None:
        return error_response(409, "DUPLICATE_EMAIL", "이미 사용 중인 이메일입니다.")

    if user_data is None:
        return error_response(404, "NOT_FOUND", "사용자를 찾을 수 없습니다.")

    if not await verify_password(req.password, user_data["password"]):
        return error_response(401, "INVALID_CREDENTIAL", "비밀번호가 일치하지 않습니다.")

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
            if user_role in (0, "0", "student"):  # 학생
                await cur.execute(
                    "UPDATE students SET email = %s WHERE uuid = %s AND is_deleted = FALSE",
                    (clean_mail, uuid)
                )
            elif user_role in (1, "1", "teacher"):  # 교사
                await cur.execute(
                    "UPDATE teachers SET email = %s WHERE uuid = %s AND is_deleted = FALSE",
                    (clean_mail, uuid)
                )

        await conn.commit()
    except Exception as e:
        await conn.rollback()
        return error_response(500, "DATABASE_ERROR", "이메일 업데이트 중 데이터베이스 오류가 발생했습니다.")
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


class PatchPasswordRequest(BaseModel):
    current_password: str = Field(..., alias="currentPassword")
    new_password: str = Field(..., alias="newPassword")

    model_config = {"populate_by_name": True}


@router.patch("/password")
async def patch_password(
    req: PatchPasswordRequest,
    current_user: dict = Depends(get_current_user),
    conn: asyncmy.Connection = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    uuid_val = current_user["uuid"]
    if not req.current_password or not req.new_password:
        return error_response(400, "VALIDATION_ERROR", "현재 비밀번호와 새 비밀번호를 모두 입력해주세요.")
    if not validate_password_complexity(req.new_password):
        return error_response(400, "WEAK_PASSWORD", "새 비밀번호는 영문·숫자·특수문자 3종 조합 8자 이상(또는 2종 조합 10자 이상)이어야 합니다.")
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT password FROM users WHERE uuid = %s AND is_deleted = FALSE LIMIT 1", (uuid_val,))
        row = await cur.fetchone()
    if not row or not await verify_password(req.current_password, row["password"]):
        return error_response(401, "INVALID_CREDENTIAL", "현재 비밀번호가 일치하지 않습니다.")
    hashed_new = await asyncio.to_thread(hash_password, req.new_password)
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("UPDATE users SET password = %s WHERE uuid = %s AND is_deleted = FALSE", (hashed_new, uuid_val))
    await conn.commit()

    new_jti = str(uuid.uuid4())
    new_token = create_access_token(
        data={
            "sub": uuid_val,
            "id": current_user.get("id"),
            "role": current_user.get("role"),
            "jti": new_jti,
        }
    )
    await revoke_all_user_tokens(
        redis=redis,
        user_uuid=uuid_val,
        current_jti=current_user.get("jti"),
        exempt_jti=new_jti,
    )

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "message": "비밀번호가 성공적으로 변경되었습니다.",
            "token": new_token,
        },
    ).model_dump()

