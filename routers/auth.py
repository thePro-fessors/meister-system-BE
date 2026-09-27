import uuid
from typing import Optional
import secrets
from core.email import send_otp_email
import asyncmy
from asyncmy.cursors import DictCursor
from fastapi import APIRouter, Query, HTTPException, Depends
from datetime import datetime, timezone, timedelta
import hashlib
from pydantic import BaseModel
from fastapi.responses import JSONResponse
from passlib.context import CryptContext
import redis.asyncio as aioredis

from database import get_db, get_redis
from sr_format import *
from core.security import create_access_token, get_current_user

# 보안을 위하여 더미 데이터를 통한 보안 검증을 진행합니다.
# DUMMY_HASH는 bcrypt 알고리즘을 통하여 "dummy_password"를 해시한 값입니다.
DUMMY_HASH = "$2b$12$e86g5Y7hZ4k7l.W2j9X0..mK1N8p3R5s7T9v1X3z5B7d9F1h3J5l."

router = APIRouter(
    prefix="/auth",
    tags=["auth", "database"],
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class LoginRequest(BaseModel):
    username: str
    password: str

async def get_current_year(date: datetime | None = None) -> int:
    target = date or datetime.now()

    if target.month < 3:
        return target.year - 1
    return target.year

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
    hash_valid = pwd_context.verify(req.password, hash_data)

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

    if user["role"] == 0:
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
                    WHERE student_id = %s AND year_id = (SELECT year_id FROM academic_years WHERE year = %s)
                """
                await cur.execute(sql_cmd, (student_id["id"], await get_current_year(datetime.now())))
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
            "currentYear": await get_current_year(date=datetime.now()),
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
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                SELECT * FROM students s
                LEFT JOIN student_academic_records sa 
                    ON s.student_id = sa.student_id
                    AND sa.year_id = (SELECT year_id FROM academic_years WHERE year = %s LIMIT 1)
                WHERE s.uuid = %s AND s.is_deleted = FALSE                
                LIMIT 1
            """
            await cur.execute(sql_cmd, (await get_current_year(), user_uuid))
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
    student_grade:Optional[int]
    student_class:Optional[int]
    student_number:Optional[int] # 교사 요청건의 경우 위 3개를 무시하십시오.
    is_it_student:bool = False
    email:str

@router.post('/send-otp')
async def send_otp(req: SendOtpRequest, conn: asyncmy.Connection = Depends(get_db), redis: aioredis.Redis = Depends(get_redis)):
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
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd="""
            SELECT * FROM students s
            JOIN student_academic_records sa
                ON s.student_id = sa.student_id
            WHERE s.email = %s AND s.is_deleted = FALSE
                AND sa.year_id = (SELECT year_id FROM academic_years WHERE year = %s LIMIT 1)
                AND sa.grade = %s AND sa.class = %s AND sa.number = %s
            LIMIT 1
            """
            curr_year = await get_current_year()
            await cur.execute(sql_cmd, (req.email, curr_year, req.student_grade, req.student_class, req.student_number))
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

    # Email 발송
    await send_otp_email(
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