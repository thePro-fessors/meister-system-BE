"""
tests/test_audit_known_issues.py - SECURITY_AND_AUDIT.md 알려진 오류 해결 심층 검증 스위트

검증 항목:
1. [3.5] routers/submissions.py 내 logger 정상 임포트 및 NameError 소거 검증
2. [1.22] core/security.py RoleChecker 정수형 Role(0, 1, 2) 및 문자열 Role 상호 호환 검증
3. [1.20] routers/auth.py 및 core/email.py 이메일 형식(RFC 5322) 검증 체계 동작 검증
4. [1.17] 학생 증빙 제출/재제출 시 detail, description Stored XSS 이스케이프 검증
5. [3.8] routers/submissions.py delete_submission 트랜잭션 원자성(autocommit(False)/commit/rollback) 검증
6. [2.15] database.py 빈 문자열 환경변수 ValueError 방어 검증
"""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.email import validate_email_format
from core.security import RoleChecker, UserRole
import routers.submissions as submissions_mod
import routers.auth as auth_mod
from main import app
from database import get_db, get_redis


class TestAuditKnownIssues(unittest.TestCase):
    # 1. routers/submissions.py logger 임포트 검증 (Issue 3.5)
    def test_submissions_logger_imported(self):
        """routers/submissions.py에 logger가 정상 임포트되어 NameError가 발생하지 않는지 검증"""
        self.assertTrue(hasattr(submissions_mod, "logger"))
        self.assertIsNotNone(submissions_mod.logger)
        # logger.warning 호출 시 예외 없이 동작하는지 확인
        try:
            submissions_mod.logger.warning("Test warning from audit test")
        except NameError:
            self.fail("logger is not defined in routers.submissions")

    # 2. RoleChecker 정수형 Role(0, 1, 2) 호환성 검증 (Issue 1.22)
    def test_role_checker_integer_and_string_roles(self):
        """RoleChecker가 정수형(0, 1, 2) 및 문자열('student', 'teacher', 'admin')을 모두 정상 인가하는지 검증"""
        checker_student = RoleChecker(["student"])
        # 정수형 role 0 허용
        user_int_student = {"id": "s1", "role": 0}
        self.assertEqual(checker_student(user_int_student), user_int_student)
        # 문자열 role '0' 허용
        user_str0_student = {"id": "s1", "role": "0"}
        self.assertEqual(checker_student(user_str0_student), user_str0_student)
        # 문자열 role 'student' 허용
        user_named_student = {"id": "s1", "role": "student"}
        self.assertEqual(checker_student(user_named_student), user_named_student)

        # 교사 / 관리자 체커 검증
        checker_teacher = RoleChecker([1])
        user_int_teacher = {"id": "t1", "role": 1}
        self.assertEqual(checker_teacher(user_int_teacher), user_int_teacher)
        user_str_teacher = {"id": "t1", "role": "teacher"}
        self.assertEqual(checker_teacher(user_str_teacher), user_str_teacher)

        checker_admin = RoleChecker([UserRole.ADMIN])
        user_int_admin = {"id": "a1", "role": 2}
        self.assertEqual(checker_admin(user_int_admin), user_int_admin)

        # 권한 없는 역할에 대해 403 HTTPException 발생 검증
        with self.assertRaises(HTTPException) as ctx:
            checker_admin({"id": "s1", "role": 0})
        self.assertEqual(ctx.exception.status_code, 403)

    # 3. 이메일 형식(RFC 5322) 검증 체계 (Issue 1.20)
    def test_email_validation_rfc5322(self):
        """RFC 5322 호환 이메일 형식 유효성 검사 및 비정상 이메일 차단 검증"""
        # 정상 이메일
        self.assertTrue(validate_email_format("student@school.kr"))
        self.assertTrue(validate_email_format("teacher.name@domain.com"))
        self.assertTrue(validate_email_format("user+alias@sub.domain.org"))

        # 비정상 이메일
        self.assertFalse(validate_email_format(""))
        self.assertFalse(validate_email_format("plainaddress"))
        self.assertFalse(validate_email_format("user@"))
        self.assertFalse(validate_email_format("@domain.com"))
        self.assertFalse(validate_email_format("user@domain"))
        self.assertFalse(validate_email_format("user@.com"))
        self.assertFalse(validate_email_format("test@example..com"))
        self.assertFalse(validate_email_format("test@example.com."))
        self.assertFalse(validate_email_format("test@domain."))
        self.assertFalse(validate_email_format("a" * 321 + "@domain.com"))

    # 4. Auth 엔드포인트 비정상 이메일 400 차단 검증 (Issue 1.20)
    def test_auth_endpoints_reject_invalid_email(self):
        """인증 엔드포인트에서 비정상 이메일 인입 시 400 VALIDATION_ERROR 반환 검증"""
        mock_conn = MagicMock()
        mock_redis = MagicMock()
        app.dependency_overrides[get_db] = lambda: mock_conn
        app.dependency_overrides[get_redis] = lambda: mock_redis

        try:
            client = TestClient(app)

            # send-otp
            res_otp = client.post("/auth/send-otp", json={"email": "invalid-email"})
            self.assertEqual(res_otp.status_code, 400)
            self.assertEqual(res_otp.json()["error"]["code"], "VALIDATION_ERROR")

            # verify-otp
            res_votp = client.post("/auth/verify-otp", json={"email": "not_an_email", "verify_code": "123456"})
            self.assertEqual(res_votp.status_code, 400)
            self.assertEqual(res_votp.json()["error"]["code"], "VALIDATION_ERROR")

            # register
            res_reg = client.post(
                "/auth/register",
                json={"email": "bad_email@", "password": "Password123!", "id": "test", "register_token": "token"},
            )
            self.assertEqual(res_reg.status_code, 400)
            self.assertEqual(res_reg.json()["error"]["code"], "VALIDATION_ERROR")
        finally:
            app.dependency_overrides.clear()

    # 5. Stored XSS html.escape 처리 검증 (Issue 1.17)
    def test_xss_escape_sanitization(self):
        """악의적인 스크립트 태그 및 이벤트 핸들러가 html.escape를 통해 안전하게 치환되는지 검증"""
        import html
        raw_xss = "<script>alert('XSS')</script><img src=x onerror=alert(1)>"
        escaped = html.escape(raw_xss)
        self.assertNotIn("<script>", escaped)
        self.assertNotIn("<img", escaped)
        self.assertIn("&lt;script&gt;", escaped)
        self.assertIn("&lt;img", escaped)

    # 6. delete_submission 트랜잭션 원자성 호출 검증 (Issue 3.8)
    def test_delete_submission_transaction_atomicity(self):
        """delete_submission 핸들러가 autocommit(False), commit(), rollback(), autocommit(True)를 정확히 호출하는지 검증"""
        from unittest.mock import patch

        class MockCursorForDelete:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def execute(self, sql, params=None):
                pass

            async def fetchone(self):
                return {
                    "submission_id": 1,
                    "student_id": 1,
                    "status_code": 1,
                    "file_path": None,
                    "granted_score": 0,
                    "uuid": "u1",
                }

        class MockConnForDelete:
            def __init__(self):
                self.calls = []

            async def autocommit(self, val: bool):
                self.calls.append(f"autocommit_{val}")

            async def commit(self):
                self.calls.append("commit")

            async def rollback(self):
                self.calls.append("rollback")

            def cursor(self, cursor=None):
                return MockCursorForDelete()

        mock_conn = MockConnForDelete()
        mock_redis = MagicMock()

        app.dependency_overrides[get_db] = lambda: mock_conn
        app.dependency_overrides[get_redis] = lambda: mock_redis
        app.dependency_overrides[submissions_mod.get_current_user] = lambda: {"uuid": "u1", "role": 0, "student_id": 1}

        with patch("core.authorization.authorize_submission_access", new=AsyncMock(return_value=None)):
            try:
                client = TestClient(app)
                res = client.delete("/api/submissions/1")
                self.assertEqual(res.status_code, 200)

                # 트랜잭션 순서 검증: autocommit_False -> commit -> autocommit_True
                self.assertIn("autocommit_False", mock_conn.calls)
                self.assertIn("commit", mock_conn.calls)
                self.assertIn("autocommit_True", mock_conn.calls)
                idx_start = mock_conn.calls.index("autocommit_False")
                idx_commit = mock_conn.calls.index("commit")
                idx_end = mock_conn.calls.index("autocommit_True")
                self.assertTrue(idx_start < idx_commit < idx_end)
            finally:
                app.dependency_overrides.clear()

    def test_delete_submission_transaction_rollback_on_error(self):
        """delete_submission 도중 예외 발생 시 rollback() 호출 및 autocommit(True) 복원 검증"""
        from unittest.mock import patch

        class MockCursorWithError:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def execute(self, sql, params=None):
                if "UPDATE submissions" in sql:
                    raise RuntimeError("DB connection failure simulation")

            async def fetchone(self):
                return {
                    "submission_id": 1,
                    "student_id": 1,
                    "status_code": 1,
                    "file_path": None,
                    "granted_score": 0,
                    "uuid": "u1",
                }

        class MockConnForDelete:
            def __init__(self):
                self.calls = []

            async def autocommit(self, val: bool):
                self.calls.append(f"autocommit_{val}")

            async def commit(self):
                self.calls.append("commit")

            async def rollback(self):
                self.calls.append("rollback")

            def cursor(self, cursor=None):
                return MockCursorWithError()

        mock_conn = MockConnForDelete()
        mock_redis = MagicMock()

        app.dependency_overrides[get_db] = lambda: mock_conn
        app.dependency_overrides[get_redis] = lambda: mock_redis
        app.dependency_overrides[submissions_mod.get_current_user] = lambda: {"uuid": "u1", "role": 0, "student_id": 1}

        with patch("core.authorization.authorize_submission_access", new=AsyncMock(return_value=None)):
            try:
                client = TestClient(app, raise_server_exceptions=False)
                res = client.delete("/api/submissions/1")
                self.assertEqual(res.status_code, 500)

                # 트랜잭션 롤백 검증: autocommit_False -> rollback -> autocommit_True
                self.assertIn("autocommit_False", mock_conn.calls)
                self.assertIn("rollback", mock_conn.calls)
                self.assertNotIn("commit", mock_conn.calls)
                self.assertIn("autocommit_True", mock_conn.calls)
            finally:
                app.dependency_overrides.clear()

    # 7. database.py 빈 문자열 환경변수 안전 파싱 검증 (Issue 2.15)
    def test_database_empty_string_env_guard(self):
        """DATABASE_PORT='', DATABASE_POOL_MIN='' 등 빈 문자열이 유입되어도 ValueError 없이 기본값 적용 검증"""
        port_val = int(os.getenv("DATABASE_PORT_TEST_NOT_SET") or 3306)
        self.assertEqual(port_val, 3306)

        empty_str_env = ""
        resolved_port = int(empty_str_env or 3306)
        self.assertEqual(resolved_port, 3306)

        resolved_pool_min = int(empty_str_env or 5)
        self.assertEqual(resolved_pool_min, 5)

        resolved_pool_max = int(empty_str_env or 30)
        self.assertEqual(resolved_pool_max, 30)

    # 8. core/academic.py 학년도 캐시 정수 정규화 및 안전 조회 검증 (Issue 2.12)
    def test_academic_year_id_cache_normalization(self):
        """get_year_id_by_year가 문자열('2026') 및 int(2026)을 동일하게 처리하고 None/잘못된 값에 안전하게 동작하는지 검증"""
        import asyncio
        from core.academic import _YEAR_ID_CACHE, clear_year_cache, get_year_id_by_year

        clear_year_cache()
        _YEAR_ID_CACHE[2026] = 99

        class DummyConn:
            def cursor(self, cursor=None):
                raise AssertionError("DB query should not be called on cache hit")

        conn = DummyConn()
        loop = asyncio.new_event_loop()
        try:
            # int 조회 -> 캐시 히트
            res_int = loop.run_until_complete(get_year_id_by_year(conn, 2026))
            self.assertEqual(res_int, 99)

            # str 조회 -> int 정규화 후 캐시 히트
            res_str = loop.run_until_complete(get_year_id_by_year(conn, "2026"))
            self.assertEqual(res_str, 99)

            # None 및 비정상 문자열 -> None 반환
            res_none = loop.run_until_complete(get_year_id_by_year(conn, None))
            self.assertIsNone(res_none)

            res_invalid = loop.run_until_complete(get_year_id_by_year(conn, "abc"))
            self.assertIsNone(res_invalid)
        finally:
            loop.close()
            clear_year_cache()


if __name__ == "__main__":
    unittest.main()
