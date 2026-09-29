import re
import uuid
from typing import Optional
import secrets

from core.email import send_otp_email
import asyncmy
from asyncmy.cursors import DictCursor
from fastapi import APIRouter, Query, HTTPException, Depends, BackgroundTasks
from datetime import datetime, timezone, timedelta
import hashlib
from pydantic import BaseModel
from fastapi.responses import JSONResponse
import bcrypt
import redis.asyncio as aioredis

from database import get_db, get_redis
from sr_format import *
from core.security import create_access_token, get_current_user

# 보안을 위하여 더미 데이터를 통한 보안 검증을 진행합니다.
# DUMMY_HASH는 bcrypt 알고리즘을 통하여 "dummy_password"를 rounds=12로 해시한 값입니다.
DUMMY_HASH = "$2b$12$e86g5Y7hZ4k7l.W2j9X0..mK1N8p3R5s7T9v1X3z5B7d9F1h3J5l."

router = APIRouter(
    prefix="/auth",
    tags=["auth", "database"],
)

# 학년도 ID 메모리 캐시 (year -> year_id)
_YEAR_ID_CACHE: dict[int, int] = {}

def hash_password(password: str) -> str:
    """비밀번호를 bcrypt(rounds=12)로 안전하게 단방향 해싱합니다."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """평문 비밀번호와 해시 비밀번호를 상시 일정한 시간으로 안전하게 대조합니다."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False

class LoginRequest(BaseModel):
    username: str
    password: str

async def get_current_year(date: datetime | None = None) -> int:
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

@router.post("/login")
async def login(req: LoginRequest, conn: asyncmy.Connection = Depends(get_db)):
    async with conn.cursor(cursor=DictCursor) as cur:
        sql_cmd = """
            SELECT uuid, id, password, role, email
            FROM users
            WHERE id = %s AND is_deleted = FALSE
        """
        await cur.execute(sql_cmd, (req.username,))
        user = await cur.fetchone()

    hash_data = user["password"] if user else DUMMY_HASH
    # 왜 비밀번호 검증을 여기서 할까? : bcrypt 알고리즘의 연산 시간을 기반으로 계정이 있는지 확인하는 공격법이 있다더라
    hash_valid = verify_password(req.password, hash_data)

    if not user or not hash_valid:
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="INVALID_CREDENTIAL",
                    message="아이디 혹은 비밀번호가 올바르지 않습니다."
                )
            ).model_dump()
        )

    student_id = None
    students = None
    teachers = None
    current_yr = await get_current_year(datetime.now())

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
            "user": {"uuid": user_uuid,
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
                     "homeroom": f"{teachers['grade']}-{teachers['class']}" if teachers and teachers["grade"] and teachers["class"] else None
            }
        }
    ).model_dump()

class SendOtpRequest(BaseModel):
    student_grade: Optional[int] = None
    student_class: Optional[int] = None
    student_number: Optional[int] = None # 교사 요청건의 경우 위 3개를 무시하십시오.
    is_it_student: bool = False
    email: str

@router.post('/send-otp')
async def send_otp(req: SendOtpRequest, background_tasks: BackgroundTasks, conn: asyncmy.Connection = Depends(get_db), redis: aioredis.Redis = Depends(get_redis)):
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

    # TRNG를 이용한 난수 생성
    otp_codes = f"{secrets.randbelow(1000000):06d}"
    # 더 복잡한 암호화 (각 자리마다 0 ~ 9 중 택 1하여 선택. 10^6 확률)
    # otp_codes = "".join(secrets.choice("0123456789") for _ in range(8))
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
            "message" : "Send codes",
            "expiresIn": 300
        }
    ).model_dump()

class VerifyOtpRequest(BaseModel):
    email:str
    verify_code:str

@router.post("/verify-otp")
async def verify_otp(req: VerifyOtpRequest, redis: aioredis.Redis = Depends(get_redis)):
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
        remain = 5-attempts
        times = await redis.ttl(otp_key)
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data={
                    "remainingAttempts": remain,
                    "remainingSeconds": max(0,times)
                },
                error=Error(
                    code="INVALID_OTP",
                    message="인증번호가 올바르지 않습니다."
                )
            ).model_dump()
        )

    await redis.delete(otp_key)
    await redis.delete(attempt_key)

    register_token = secrets.token_urlsafe(32)
    await redis.setex(f"register_token:{clean_mail}", 600, register_token)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "message" : "Verify OTP codes.",
            "email" : clean_mail,
            "registerToken" : register_token
        }
    ).model_dump()

class RegisterRequest(BaseModel):
    email: str
    password: str
    id: str
    register_token: str
    role: Optional[int] = None  # 클라이언트가 임의로 보내더라도 서버 판별값으로 덮어씌워 무시됨

# 비밀번호 정규식: 최소 8자 이상, 영문자 1개 이상 및 숫자 1개 이상 포함
PASSWORD_REGEX = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,}$")

@router.post("/register")
async def register_user(
    req: RegisterRequest, 
    redis: aioredis.Redis = Depends(get_redis), 
    conn: asyncmy.Connection = Depends(get_db)
):
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

    # 비밀번호 복잡도 정규식 검증 (최소 8자, 영문 + 숫자 조합)
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

    # 1. 서버 기반 권한(Role) 확정 판별 (클라이언트의 권한 상승 위조 원천 방어)
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

        # 2. 아이디 중복 검사
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

    await redis.delete(f"register_token:{clean_mail}")

    user_uuid = str(uuid.uuid4())
    user_pw = hash_password(req.password)

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
                WHERE email = %s AND is_deleted = FALSE
            """
            await cur.execute(sql_cmd, (user_uuid, clean_mail))

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
