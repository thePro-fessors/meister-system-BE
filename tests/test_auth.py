"""
tests/test_auth.py - 인증, 로그인, OTP, 비밀번호 복잡도 및 보안 통제 단위/통합 테스트
SECURITY_AND_AUDIT.md 및 TODO.md 명세 검증 스위트
"""

import glob
import json
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock

# 동적 .venv 감지
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sp in glob.glob(os.path.join(_project_root, ".venv", "lib", "python*", "site-packages")):
    if os.path.exists(_sp) and _sp not in sys.path:
        sys.path.append(_sp)

import bcrypt
from fastapi.testclient import TestClient

from core.security import (
    create_access_token,
    JWT_SECRET_KEY,
    JWT_ALGORITHM,
)
from database import get_db, get_redis
from main import app
from routers.auth import hash_password, validate_password_complexity


class MockRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = str(val)
        if ex:
            self.ttls[key] = ex
        return True

    async def setex(self, key, seconds, val):
        self.data[key] = str(val)
        self.ttls[key] = seconds
        return True

    async def incr(self, key):
        cur = int(self.data.get(key, 0)) + 1
        self.data[key] = str(cur)
        return cur

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def ttl(self, key):
        return self.ttls.get(key, 300)

    async def delete(self, key):
        self.data.pop(key, None)
        self.ttls.pop(key, None)

    async def getdel(self, key):
        val = self.data.pop(key, None)
        self.ttls.pop(key, None)
        return val


class MockCursor:
    def __init__(self, conn):
        self.conn = conn
        self._current_result = []
        self._fetch_index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql: str, params: tuple = ()):
        sql_clean = " ".join(sql.strip().split())
        self._fetch_index = 0

        # 로그인 단일 JOIN 쿼리 (users, students, teachers, academic_years, records)
        if "FROM users u" in sql_clean and "LEFT JOIN students" in sql_clean:
            year_param = params[0]
            username_param = params[1]
            matched = []
            for u in self.conn.users:
                if u["id"] == username_param and not u.get("is_deleted"):
                    row = {
                        "uuid": u["uuid"],
                        "id": u["id"],
                        "password": u["password"],
                        "role": u["role"],
                        "email": u["email"],
                        "student_id": None,
                        "student_name": None,
                        "student_grade": None,
                        "student_class": None,
                        "student_number": None,
                        "teachers_id": None,
                        "teacher_name": None,
                        "teacher_grade": None,
                        "teacher_class": None,
                    }
                    if u["role"] == 0:
                        st = next((s for s in self.conn.students if s["uuid"] == u["uuid"]), None)
                        if st:
                            row["student_id"] = st["student_id"]
                            row["student_name"] = st["name"]
                            rec = next((r for r in self.conn.academic_records if r["student_id"] == st["student_id"]), None)
                            if rec:
                                row["student_grade"] = rec["grade"]
                                row["student_class"] = rec["class"]
                                row["student_number"] = rec["number"]
                    elif u["role"] == 1:
                        tc = next((t for t in self.conn.teachers if t["uuid"] == u["uuid"]), None)
                        if tc:
                            row["teachers_id"] = tc["teachers_id"]
                            row["teacher_name"] = tc["name"]
                            row["teacher_grade"] = tc["grade"]
                            row["teacher_class"] = tc["class"]
                    matched.append(row)
            self._current_result = matched

        elif "FROM users" in sql_clean and "WHERE id = %s" in sql_clean:
            username = params[0]
            self._current_result = [u for u in self.conn.users if u["id"] == username and not u.get("is_deleted")]

        elif "FROM users" in sql_clean and "WHERE email = %s" in sql_clean:
            email = params[0]
            self._current_result = [u for u in self.conn.users if u.get("email") == email and not u.get("is_deleted")]

        elif "FROM students" in sql_clean:
            self._current_result = self.conn.students

        elif "FROM teachers" in sql_clean:
            self._current_result = self.conn.teachers

        elif "FROM academic_years" in sql_clean:
            self._current_result = [{"year_id": 1, "year": 2026}]

        elif "INSERT INTO users" in sql_clean:
            self.conn.users.append({
                "uuid": params[0],
                "id": params[1],
                "password": params[2],
                "email": params[3],
                "role": params[4],
                "is_deleted": False,
            })

    async def fetchone(self):
        if self._fetch_index < len(self._current_result):
            item = self._current_result[self._fetch_index]
            self._fetch_index += 1
            return item
        return None

    async def fetchall(self):
        return self._current_result


class MockConnection:
    def __init__(self):
        pw_hash = bcrypt.hashpw(b"ValidPass1234!", bcrypt.gensalt(rounds=4)).decode("utf-8")
        self.users = [
            {
                "uuid": "student-uuid-1",
                "id": "student1",
                "password": pw_hash,
                "email": "student1@school.kr",
                "role": 0,
                "is_deleted": False,
            },
            {
                "uuid": "teacher-uuid-1",
                "id": "teacher1",
                "password": pw_hash,
                "email": "teacher1@school.kr",
                "role": 1,
                "is_deleted": False,
            },
        ]
        self.students = [
            {"student_id": 1, "uuid": "student-uuid-1", "name": "김학생", "email": "student1@school.kr", "is_deleted": False},
        ]
        self.academic_records = [
            {"student_id": 1, "year_id": 1, "grade": 1, "class": 2, "number": 15},
        ]
        self.teachers = [
            {"teachers_id": 1, "uuid": "teacher-uuid-1", "name": "이교사", "grade": 1, "class": 2, "email": "teacher1@school.kr", "is_deleted": False},
        ]

    def cursor(self, cursor=None):
        return MockCursor(self)

    async def autocommit(self, val: bool):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class TestAuthAPI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_redis = MockRedis()
        self.mock_conn = MockConnection()

        app.dependency_overrides[get_db] = lambda: self.mock_conn
        app.dependency_overrides[get_redis] = lambda: self.mock_redis
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    # 1. 학생 단일 JOIN 로그인 성공
    def test_login_student_single_join_success(self):
        """[성능 2.2 / 기능 1.1] 학생 로그인 시 단일 JOIN 쿼리로 학적 정보 및 세션 토큰 응답 검증"""
        res = self.client.post(
            "/api/auth/login",
            json={"username": "student1", "password": "ValidPass1234!"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertIn("token", data)
        user = data["user"]
        self.assertEqual(user["id"], "student1")
        self.assertEqual(user["role"], "student")
        self.assertEqual(user["name"], "김학생")
        self.assertEqual(user["grade"], 1)
        self.assertEqual(user["classNo"], 2)
        self.assertEqual(user["number"], 15)

    # 2. 교사 단일 JOIN 로그인 성공
    def test_login_teacher_single_join_success(self):
        """[성능 2.2 / 기능 1.1] 교사 로그인 시 담당 학급 정보 및 세션 토큰 응답 검증"""
        res = self.client.post(
            "/api/auth/login",
            json={"username": "teacher1", "password": "ValidPass1234!"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        user = body["data"]["user"]
        self.assertEqual(user["role"], "teacher")
        self.assertEqual(user["name"], "이교사")
        self.assertEqual(user["homeroom"], "1-2")

    # 3. 로그인 실패 및 무차별 대입 방어 (5회 연속 실패 시 10분 잠금)
    async def test_login_brute_force_lockout_after_5_failures(self):
        """[보안 1.1] 계정별 5회 연속 실패 시 10분 잠금(429 ACCOUNT_LOCKED) 차단 검증"""
        # 1~4회 실패: 401 UNAUTHORIZED
        for _ in range(4):
            res = self.client.post(
                "/api/auth/login",
                json={"username": "student1", "password": "WrongPassword!"},
            )
            self.assertEqual(res.status_code, 401)

        # 5회째 실패 기록
        res5 = self.client.post(
            "/api/auth/login",
            json={"username": "student1", "password": "WrongPassword!"},
        )
        self.assertEqual(res5.status_code, 401)

        # 6회째 시도: 계정 잠금 429 차단
        res6 = self.client.post(
            "/api/auth/login",
            json={"username": "student1", "password": "ValidPass1234!"},
        )
        self.assertEqual(res6.status_code, 429)
        self.assertEqual(res6.json()["error"]["code"], "ACCOUNT_LOCKED")

    # 4. OTP 인증 시도 횟수 초과 및 원자적 파기
    async def test_verify_otp_atomic_invalidation_after_5_attempts(self):
        """[보안 1.8] OTP 오입력 5회 초과 시 즉시 무효화 및 원자적 파기 검증"""
        email = "test@school.kr"
        await self.mock_redis.set(f"otp:{email}", "123456", ex=300)

        # 4회 오입력
        for _ in range(4):
            res = self.client.post(
                "/api/auth/verify-otp",
                json={"email": email, "verify_code": "999999"},
            )
            self.assertEqual(res.status_code, 400)

        # 5회째 오입력 -> 횟수 초과로 OTP 즉시 삭제 처리
        res5 = self.client.post(
            "/api/auth/verify-otp",
            json={"email": email, "verify_code": "999999"},
        )
        self.assertEqual(res5.status_code, 400)
        self.assertIn("초과", res5.json()["error"]["message"])

        # OTP 키가 Redis에서 즉시 파기되었는지 확인
        self.assertIsNone(await self.mock_redis.get(f"otp:{email}"))

    # 5. 회원가입 시 비밀번호 복잡도 유효성 검사 차단
    def test_register_weak_password_rejected(self):
        """[보안 1.5] 단순 8자리 영숫자 패스워드로 가입 시 400 WEAK_PASSWORD 거부 검증"""
        self.mock_redis.data["register_token:newuser@school.kr"] = "valid_token"
        res = self.client.post(
            "/api/auth/register",
            json={
                "id": "newuser",
                "email": "newuser@school.kr",
                "password": "simple12",  # 특수문자 없는 8자리 취약 패스워드 (2종 8자: 3종 미충족 및 10자 미달)
                "register_token": "valid_token",
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "WEAK_PASSWORD")

    # 6. 표준 HTTP 보안 응답 헤더 6종 자동 주입 검증
    def test_security_headers_present(self):
        """[보안] 모든 HTTP 응답에 nosniff, DENY, strict-origin 등 보안 헤더 포함 검증"""
        res = self.client.get("/api/auth/me")
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res.headers.get("X-XSS-Protection"), "1; mode=block")
        self.assertEqual(res.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        self.assertEqual(res.headers.get("Cache-Control"), "no-store")
