import uuid
import asyncmy
from asyncmy.cursors import DictCursor
from fastapi import APIRouter, Query, HTTPException, Depends
from datetime import datetime, timezone
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
1