"""
tests/test_auth.py - 인증, 로그인, OTP, 비밀번호 복잡도 및 보안 통제 단위/통합 테스트
SECURITY_AND_AUDIT.md 및 TODO.md 명세 검증 스위트
"""

import os
import sys

# 테스트 환경 내 실제 SMTP 발송 방지 (로컬 프로세스 메모리 한정)
os.environ["EMAIL_PROVIDER"] = "console"

import glob
import json
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
        if key not in self.data and key not in self.ttls:
            return -2
        return self.ttls.get(key, -1)

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
        self.rowcount = 1

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
                    elif u["role"] == 1 or u["role"] == 2:
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

        elif "FROM users" in sql_clean and ("WHERE uuid = %s" in sql_clean or "WHERE u.uuid = %s" in sql_clean):
            u_uuid = params[0]
            self._current_result = [u for u in self.conn.users if u.get("uuid") == u_uuid and not u.get("is_deleted")]

        elif "FROM students" in sql_clean:
            if "WHERE s.email = %s" in sql_clean or "WHERE email = %s" in sql_clean:
                email = params[0]
                self._current_result = [s for s in self.conn.students if s.get("email") == email and not s.get("is_deleted")]
            elif "WHERE s.uuid = %s" in sql_clean or "WHERE uuid = %s" in sql_clean:
                user_uuid = params[0] if "year_id" not in sql_clean else params[1]
                self._current_result = [s for s in self.conn.students if s.get("uuid") == user_uuid and not s.get("is_deleted")]
            else:
                self._current_result = self.conn.students

        elif "FROM teachers" in sql_clean:
            if "WHERE email = %s" in sql_clean:
                email = params[0]
                self._current_result = [t for t in self.conn.teachers if t.get("email") == email and not t.get("is_deleted")]
            elif "WHERE uuid = %s" in sql_clean:
                user_uuid = params[0]
                self._current_result = [t for t in self.conn.teachers if t.get("uuid") == user_uuid and not t.get("is_deleted")]
            else:
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

        elif "UPDATE teachers" in sql_clean:
            self.rowcount = 1
            if len(params) >= 2:
                u_uuid, u_email = params[0], params[1]
                for t in self.conn.teachers:
                    if t.get("email") == u_email:
                        t["uuid"] = u_uuid

        elif "UPDATE students" in sql_clean:
            self.rowcount = 1
            if len(params) >= 2:
                u_uuid, u_email = params[0], params[1]
                for s in self.conn.students:
                    if s.get("email") == u_email:
                        s["uuid"] = u_uuid

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
            {"teachers_id": 2, "uuid": None, "name": "박교사", "grade": 2, "class": 1, "email": "newteacher@school.kr", "is_deleted": False},
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
    TEST_ADMIN_CODE = "TEST_ADMIN_SECRET_CODE!"

    def setUp(self):
        os.environ["ADMIN_REGISTRATION_CODE"] = self.TEST_ADMIN_CODE
        self.mock_redis = MockRedis()
        self.mock_conn = MockConnection()

        app.dependency_overrides[get_db] = lambda: self.mock_conn
        app.dependency_overrides[get_redis] = lambda: self.mock_redis
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("ADMIN_REGISTRATION_CODE", None)
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
        # HTTP 요청: 로컬/IP 차단 방지를 위해 HSTS 미포함
        self.assertNotIn("Strict-Transport-Security", res.headers)
        self.assertEqual(res.headers.get("Content-Security-Policy"), "default-src 'self'; frame-ancestors 'none'; object-src 'none';")

        # HTTPS 요청: X-Forwarded-Proto 또는 HTTPS scheme 시 HSTS 정상 주입
        res_https = self.client.get("/api/auth/me", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(res_https.headers.get("Strict-Transport-Security"), "max-age=31536000; includeSubDomains")

        # 다중 리버스 프록시 경유 체인 (https, http): 클라이언트 최초 프로토콜이 https일 때 HSTS 주입
        res_multi_https = self.client.get("/api/auth/me", headers={"X-Forwarded-Proto": "https, http"})
        self.assertEqual(res_multi_https.headers.get("Strict-Transport-Security"), "max-age=31536000; includeSubDomains")

        # 다중 리버스 프록시 경유 체인 (http, https): 클라이언트 최초 프로토콜이 http일 때 HSTS 미주입
        res_multi_http = self.client.get("/api/auth/me", headers={"X-Forwarded-Proto": "http, https"})
        self.assertNotIn("Strict-Transport-Security", res_multi_http.headers)

    # 7. 교사 회원가입 OTP 발송 성공/실패 검증
    def test_send_otp_teacher_success(self):
        """사전 등록된 교사 이메일로 OTP 발송 요청 시 200 반환 및 Redis OTP 등록 검증"""
        res = self.client.post(
            "/api/auth/send-otp",
            json={"email": "newteacher@school.kr", "is_it_student": False},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        self.assertIn("otp:newteacher@school.kr", self.mock_redis.data)

    def test_send_otp_teacher_already_registered(self):
        """이미 가입된 교사 이메일로 OTP 발송 요청 시 403 FORBIDDEN 차단 검증"""
        res = self.client.post(
            "/api/auth/send-otp",
            json={"email": "teacher1@school.kr", "is_it_student": False},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_send_otp_teacher_not_found(self):
        """사전 등록 명단에 없는 교사 이메일로 OTP 발송 요청 시 404 NOT_FOUND 차단 검증"""
        res = self.client.post(
            "/api/auth/send-otp",
            json={"email": "unknownteacher@school.kr", "is_it_student": False},
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "NOT_FOUND")

    # 8. 교사 회원가입 통합 플로우 (OTP 발송 -> 검증 -> 가입 완료 -> 권한 role=1)
    async def test_teacher_registration_full_flow(self):
        """교사 OTP 발송, 검증, 최종 회원가입 및 teachers 테이블 uuid 연동 검증"""
        email = "newteacher@school.kr"
        # 1) OTP 발송
        res_otp = self.client.post("/api/auth/send-otp", json={"email": email, "is_it_student": False})
        self.assertEqual(res_otp.status_code, 200)
        otp_code = self.mock_redis.data[f"otp:{email}"]

        # 2) OTP 검증
        res_verify = self.client.post(
            "/api/auth/verify-otp",
            json={"email": email, "verify_code": otp_code},
        )
        self.assertEqual(res_verify.status_code, 200)
        register_token = res_verify.json()["data"]["registerToken"]
        self.assertTrue(register_token)

        # 3) 회원가입
        res_reg = self.client.post(
            "/api/auth/register",
            json={
                "id": "newteacher_id",
                "email": email,
                "password": "TeacherPassword123!",
                "register_token": register_token,
            },
        )
        self.assertEqual(res_reg.status_code, 200)
        body = res_reg.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["role"], "teacher")

        # DB 검증: users 테이블에 role=1로 등록되고 teachers 테이블의 uuid가 업데이트되었는지 확인
        created_user = next((u for u in self.mock_conn.users if u["id"] == "newteacher_id"), None)
        self.assertIsNotNone(created_user)
        self.assertEqual(created_user["role"], 1)
        self.assertEqual(created_user["email"], email)

        teacher_record = next((t for t in self.mock_conn.teachers if t["email"] == email), None)
        self.assertIsNotNone(teacher_record)
        self.assertEqual(teacher_record["uuid"], created_user["uuid"])

    # 9. 관리자 코드 기반 OTP 발송 및 보안 검증
    def test_send_otp_admin_wrong_code(self):
        """유효하지 않은 관리자 가입 보안 코드로 OTP 발송 시 403 FORBIDDEN 차단 검증"""
        res = self.client.post(
            "/api/auth/send-otp",
            json={
                "email": "admin_candidate@school.kr",
                "admin_code": "INVALID_ADMIN_CODE_999",
            },
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_send_otp_admin_duplicate_email(self):
        """이미 users에 가입된 이메일로 관리자 OTP 발송 시 409 DUPLICATE_EMAIL 거부 검증"""
        res = self.client.post(
            "/api/auth/send-otp",
            json={
                "email": "teacher1@school.kr",  # 이미 users에 존재하는 계정
                "admin_code": self.TEST_ADMIN_CODE,
            },
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["error"]["code"], "DUPLICATE_EMAIL")

    def test_send_otp_admin_success_without_roster(self):
        """정상 관리자 코드로 학생/교사 사전 명단에 없는 신규 이메일 OTP 발송 성공 및 플래그 검증"""
        email = "brand_new_admin@school.kr"
        res = self.client.post(
            "/api/auth/send-otp",
            json={
                "email": email,
                "admin_code": self.TEST_ADMIN_CODE,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(f"otp:{email}", self.mock_redis.data)
        self.assertEqual(self.mock_redis.data.get(f"otp:admin_reg:{email}"), "1")

    def test_send_otp_admin_camel_case_alias(self):
        """관리자 가입 코드 전달 시 adminCode(camelCase) alias 지원 검증"""
        email = "alias_admin@school.kr"
        res = self.client.post(
            "/api/auth/send-otp",
            json={
                "email": email,
                "adminCode": self.TEST_ADMIN_CODE,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.mock_redis.data.get(f"otp:admin_reg:{email}"), "1")

    # 10. 관리자 최종 회원가입 및 권한(role=2) 부여 검증
    def test_admin_register_wrong_admin_code(self):
        """회원가입 시 잘못된 admin_code 제출 시 403 FORBIDDEN 차단 검증"""
        email = "wrong_admin@school.kr"
        self.mock_redis.data[f"register_token:{email}"] = "tok_test_123"
        res = self.client.post(
            "/api/auth/register",
            json={
                "id": "admin_wrong",
                "email": email,
                "password": "AdminPassword123!",
                "register_token": "tok_test_123",
                "admin_code": "WRONG_SECRET_CODE",
            },
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_admin_register_with_valid_admin_code_directly(self):
        """회원가입 시 유효한 admin_code를 직접 제출하여 관리자(role=2) 가입 성공 검증"""
        email = "direct_admin@school.kr"
        self.mock_redis.data[f"register_token:{email}"] = "tok_admin_direct"
        res = self.client.post(
            "/api/auth/register",
            json={
                "id": "direct_admin_id",
                "email": email,
                "password": "AdminPassword123!",
                "register_token": "tok_admin_direct",
                "admin_code": self.TEST_ADMIN_CODE,
            },
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["role"], "admin")

        user = next((u for u in self.mock_conn.users if u["id"] == "direct_admin_id"), None)
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], 2)

    async def test_admin_registration_via_otp_and_redis_flag(self):
        """OTP 발송 단계에서 관리자 코드를 인증한 후, 가입 시 코드 생략해도 관리자(role=2)로 가입 성공 검증"""
        email = "flag_admin@school.kr"
        # 1) OTP 발송 (admin_code 제공)
        res_otp = self.client.post(
            "/api/auth/send-otp",
            json={"email": email, "admin_code": self.TEST_ADMIN_CODE},
        )
        self.assertEqual(res_otp.status_code, 200)
        otp_code = self.mock_redis.data[f"otp:{email}"]
        self.assertEqual(self.mock_redis.data.get(f"otp:admin_reg:{email}"), "1")

        # 2) OTP 검증
        res_verify = self.client.post(
            "/api/auth/verify-otp",
            json={"email": email, "verify_code": otp_code},
        )
        self.assertEqual(res_verify.status_code, 200)
        register_token = res_verify.json()["data"]["registerToken"]
        # Redis 관리자 가입 플래그가 유지되는지 확인
        self.assertEqual(self.mock_redis.data.get(f"otp:admin_reg:{email}"), "1")

        # 3) 최종 회원가입 (admin_code 없이 요청해도 Redis 플래그로 role=2 부여)
        res_reg = self.client.post(
            "/api/auth/register",
            json={
                "id": "flag_admin_id",
                "email": email,
                "password": "AdminPassword123!",
                "register_token": register_token,
            },
        )
        self.assertEqual(res_reg.status_code, 200)
        self.assertEqual(res_reg.json()["data"]["role"], "admin")

        user = next((u for u in self.mock_conn.users if u["id"] == "flag_admin_id"), None)
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], 2)
        # 가입 완료 후 플래그 소멸 확인
        self.assertIsNone(await self.mock_redis.get(f"otp:admin_reg:{email}"))

    def test_admin_register_linked_with_existing_teacher_row(self):
        """교사 명단에 등록된 이메일로 관리자 가입 시, teachers 테이블의 uuid도 연동 업데이트되는지 검증"""
        email = "newteacher@school.kr"
        self.mock_redis.data[f"register_token:{email}"] = "tok_teacher_admin"
        res = self.client.post(
            "/api/auth/register",
            json={
                "id": "admin_teacher_id",
                "email": email,
                "password": "AdminPassword123!",
                "register_token": "tok_teacher_admin",
                "admin_code": self.TEST_ADMIN_CODE,
            },
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["data"]["role"], "admin")

        # users 테이블에 role=2로 등록
        user = next((u for u in self.mock_conn.users if u["id"] == "admin_teacher_id"), None)
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], 2)

        # teachers 테이블의 uuid도 해당 관리자의 uuid로 연동 완료
        teacher = next((t for t in self.mock_conn.teachers if t["email"] == email), None)
        self.assertIsNotNone(teacher)
        self.assertEqual(teacher["uuid"], user["uuid"])

    def test_admin_register_duplicate_id_and_email(self):
        """관리자 가입 시 이미 사용 중인 ID 또는 이메일 제출 시 409 CONFLICT/DUPLICATE 차단 검증"""
        # 1) 중복 ID 테스트
        email1 = "unique_admin1@school.kr"
        self.mock_redis.data[f"register_token:{email1}"] = "tok_dup_id"
        res_dup_id = self.client.post(
            "/api/auth/register",
            json={
                "id": "teacher1",  # 이미 존재하는 ID
                "email": email1,
                "password": "AdminPassword123!",
                "register_token": "tok_dup_id",
                "admin_code": self.TEST_ADMIN_CODE,
            },
        )
        self.assertEqual(res_dup_id.status_code, 409)
        self.assertEqual(res_dup_id.json()["error"]["code"], "DUPLICATE_ID")

        # 2) 중복 이메일 테스트
        self.mock_redis.data["register_token:student1@school.kr"] = "tok_dup_email"
        res_dup_email = self.client.post(
            "/api/auth/register",
            json={
                "id": "new_admin_unique_id",
                "email": "student1@school.kr",  # 이미 users에 등록된 이메일
                "password": "AdminPassword123!",
                "register_token": "tok_dup_email",
                "admin_code": self.TEST_ADMIN_CODE,
            },
        )
        self.assertEqual(res_dup_email.status_code, 409)
        self.assertEqual(res_dup_email.json()["error"]["code"], "DUPLICATE_EMAIL")

    # 11. 빈 문자열 adminCode 처리 및 회귀 검증
    def test_send_otp_with_empty_admin_code_succeeds_for_teacher(self):
        """빈 문자열 adminCode('') 전달 시 403 오류 없이 일반 교사 OTP 발송 정상 처리 검증"""
        res = self.client.post(
            "/api/auth/send-otp",
            json={
                "email": "newteacher@school.kr",
                "is_it_student": False,
                "adminCode": "",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        self.assertIn("otp:newteacher@school.kr", self.mock_redis.data)

    def test_admin_registration_with_empty_admin_code_string_via_otp(self):
        """OTP 단계에서 관리자 인증 후 최종 가입 폼에서 adminCode='' 전달 시에도 정상 관리자(role=2) 가입 검증"""
        email = "flag_empty_code_admin@school.kr"
        self.mock_redis.data[f"otp:admin_reg:{email}"] = "1"
        self.mock_redis.data[f"register_token:{email}"] = "tok_admin_empty_flag"
        res = self.client.post(
            "/api/auth/register",
            json={
                "id": "empty_code_admin",
                "email": email,
                "password": "AdminPassword123!",
                "register_token": "tok_admin_empty_flag",
                "adminCode": "",
            },
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["role"], "admin")
        created_user = next((u for u in self.mock_conn.users if u["id"] == "empty_code_admin"), None)
        self.assertIsNotNone(created_user)
        self.assertEqual(created_user["role"], 2)

    def test_admin_register_with_camel_case_admin_code(self):
        """회원가입 시 adminCode(camelCase) 직접 제출 시 정상 관리자 가입 검증"""
        email = "camel_admin@school.kr"
        self.mock_redis.data[f"register_token:{email}"] = "tok_camel"
        res = self.client.post(
            "/api/auth/register",
            json={
                "id": "camel_admin_id",
                "email": email,
                "password": "AdminPassword123!",
                "registerToken": "tok_camel",
                "adminCode": self.TEST_ADMIN_CODE,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["role"], "admin")

    # 12. 관리자 로그인 및 /auth/me 프로필 일관성 검증
    def test_login_admin_linked_with_teacher(self):
        """교사와 연동된 관리자 로그인 시 teacher_name, teacherId, homeroom 및 role='admin' 반환 검증"""
        # 관리자 계정 생성 (교사 연동)
        self.mock_conn.users.append({
            "uuid": "admin-teacher-uuid-99",
            "id": "admin_linked_teacher",
            "password": self.mock_conn.users[1]["password"],
            "email": "linked_admin@school.kr",
            "role": 2,
            "is_deleted": False,
        })
        self.mock_conn.teachers.append({
            "teachers_id": 99,
            "uuid": "admin-teacher-uuid-99",
            "name": "관리자겸교사",
            "grade": 3,
            "class": 1,
            "email": "linked_admin@school.kr",
            "is_deleted": False,
        })

        res = self.client.post(
            "/api/auth/login",
            json={"username": "admin_linked_teacher", "password": "ValidPass1234!"},
        )
        self.assertEqual(res.status_code, 200)
        user = res.json()["data"]["user"]
        self.assertEqual(user["role"], "admin")
        self.assertEqual(user["name"], "관리자겸교사")
        self.assertEqual(user["teacherId"], 99)
        self.assertEqual(user["homeroom"], "3-1")

        # /auth/me 검증
        token = res.json()["data"]["token"]
        res_me = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res_me.status_code, 200)
        user_me = res_me.json()["data"]["user"]
        self.assertEqual(user_me["role"], "admin")
        self.assertEqual(user_me["name"], "관리자겸교사")
        self.assertEqual(user_me["teacherId"], 99)
        self.assertEqual(user_me["homeroom"], "3-1")

    def test_login_admin_standalone(self):
        """단독 관리자(교사 연동 없음) 로그인 및 /auth/me 시 정상 admin 응답 검증"""
        self.mock_conn.users.append({
            "uuid": "standalone-admin-uuid",
            "id": "standalone_admin",
            "password": self.mock_conn.users[1]["password"],
            "email": "standalone@school.kr",
            "role": 2,
            "is_deleted": False,
        })

        res = self.client.post(
            "/api/auth/login",
            json={"username": "standalone_admin", "password": "ValidPass1234!"},
        )
        self.assertEqual(res.status_code, 200)
        user = res.json()["data"]["user"]
        self.assertEqual(user["role"], "admin")
        self.assertEqual(user["name"], "standalone_admin")
        self.assertIsNone(user["teacherId"])

        token = res.json()["data"]["token"]
        res_me = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res_me.status_code, 200)
        user_me = res_me.json()["data"]["user"]
        self.assertEqual(user_me["role"], "admin")
        self.assertEqual(user_me["name"], "standalone_admin")
        self.assertIsNone(user_me["teacherId"])

    # 13. 교사 가입 실패 케이스 검증
    def test_teacher_registration_already_registered(self):
        """teachers 테이블에 이미 uuid가 부여된 교사가 재가입 시도 시 403 FORBIDDEN 차단 검증"""
        email = "teacher1@school.kr"  # teachers 테이블에 uuid가 이미 존재하는 교사
        self.mock_redis.data[f"register_token:{email}"] = "tok_dup"
        res = self.client.post(
            "/api/auth/register",
            json={
                "id": "new_teacher_id",
                "email": email,
                "password": "TeacherPassword123!",
                "register_token": "tok_dup",
            },
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_teacher_registration_duplicate_email_in_users(self):
        """teachers 테이블에는 미가입(uuid=None) 상태이나 users 테이블에 이미 존재하는 이메일일 때 409 DUPLICATE_EMAIL 차단 검증"""
        email = "newteacher@school.kr"  # teachers에는 uuid=None
        self.mock_conn.users.append({
            "uuid": "prior-user-uuid",
            "id": "prior_account",
            "password": self.mock_conn.users[1]["password"],
            "email": email,
            "role": 0,
            "is_deleted": False,
        })
        self.mock_redis.data[f"register_token:{email}"] = "tok_dup_email_teacher"
        res = self.client.post(
            "/api/auth/register",
            json={
                "id": "teacher_new_id",
                "email": email,
                "password": "TeacherPassword123!",
                "register_token": "tok_dup_email_teacher",
            },
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["error"]["code"], "DUPLICATE_EMAIL")

    def test_teacher_registration_invalid_token(self):
        """유효하지 않은 register_token으로 가입 시 401 INVALID_REGISTER_TOKEN 차단 검증"""
        res = self.client.post(
            "/api/auth/register",
            json={
                "id": "new_teacher_id",
                "email": "newteacher@school.kr",
                "password": "TeacherPassword123!",
                "register_token": "invalid_or_expired_token",
            },
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "INVALID_REGISTER_TOKEN")


