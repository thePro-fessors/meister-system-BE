import uuid
import asyncmy
from asyncmy.cursors import DictCursor
from fastapi import APIRouter, Query, HTTPException, Depends
from datetime import datetime
import hashlib
from pydantic import BaseModel
from fastapi.responses import JSONResponse
from passlib.context import CryptContext

from database import get_db
from sr_format import *

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
        return target.year-1
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
    if not user:
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data = None,
                error=Error(
                    code="INVALID_CREDENTIAL",
                    message="아이디 혹은 비밀번호가 올바르지 않습니다."
                )
            ).model_dump()
        )

    if not pwd_context.verify(req.password, user["password"]):
        return JSONResponse(
            status_code=401,
            content=SrFormat(
                status_code=401,
                success=False,
                data = None,
                error=Error(
                    code="INVALID_CREDENTIAL",
                    message="아이디 혹은 비밀번호가 올바르지 않습니다."
                )
            ).model_dump()
        )

    if user["role"] == 0:
        async with conn.cursor(cursor=DictCursor) as cur:
            sql_cmd = """
                SELECT name, student_id AS id
                FROM students
                WHERE uuid = %s AND is_deleted = FALSE
            """
            await cur.execute(sql_cmd, (user["uuid"],))
            student_id = await cur.fetchone()
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
    
    return SrFormat(
        status_code=200,
        success=True,
        data={
            "current_year" : await get_current_year(date=datetime.now()),
            "user":{
                "uuid": user["uuid"],
                "id": user["id"],
                "name": students["name"] if user["role"]==0 else teachers["name"] if user["role"]==1 else None,
                "email": user["email"],
                "role": user["role"],
                "student_id": student_id["id"] if student_id else None,
                "grade": students["grade"] if students else None,
                "classNo": students["class"] if students else None,
                "number": students["number"] if students else None,
                "teacherId": teachers["id"] if teachers else None,
                "homeroom": f'{teachers["grade"]}-{teachers["class"]}' if teachers else None,
            }
        }
    ).model_dump()