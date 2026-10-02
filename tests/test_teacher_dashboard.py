"""
tests/test_teacher_dashboard.py - 교사 대시보드 조회 API (GET /api/teacher/dashboard) 단위/통합 테스트
Tech_spec.md 3.1 & TODO.md 3.1 명세 검증
"""

import glob
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

from fastapi.testclient import TestClient

from core.security import create_access_token
from database import get_db, get_redis
from main import app
import routers.students as students_mod


class TeacherDashboardMockCursor:
    """SQL 구문 매칭 기반 Mock Cursor"""

    def __init__(self, teacher_data=None, stats_data=None, recent_data=None, year_id=101):
        self.teacher_data = teacher_data
        self.stats_data = stats_data or {"pending_count": 0, "resubmitted_count": 0, "unscored_count": 0}
        self.recent_data = recent_data or []
        self.year_id = year_id
        self._current_result = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query: str, params: tuple = ()):
        q = " ".join(query.strip().split())
        if "FROM teachers" in q:
            self._current_result = [self.teacher_data] if self.teacher_data else []
        elif "FROM academic_years" in q:
            self._current_result = [{"year_id": self.year_id}]
        elif "pending_count" in q:
            self._current_result = [self.stats_data]
        elif "recentSubmissions" in q or "ORDER BY s.created_at" in q:
            self._current_result = list(self.recent_data)
        else:
            self._current_result = []

    async def fetchone(self):
        if self._current_result:
            return self._current_result.pop(0)
        return None

    async def fetchall(self):
        res = list(self._current_result)
        self._current_result.clear()
        return res


class TeacherDashboardMockConnection:
    def __init__(self, cursor_mock):
        self.cursor_mock = cursor_mock

    def cursor(self, cursor=None):
        return self.cursor_mock


class TestTeacherDashboardAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_redis = MagicMock()
        self.mock_redis.get = MagicMock(return_value=None)
        self.mock_redis.set = MagicMock(return_value=True)

        async def _fake_redis_get(key):
            return None

        self.mock_redis.get = _fake_redis_get
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        # Dummy DB override for general endpoints
        dummy_cursor = TeacherDashboardMockCursor()
        app.dependency_overrides[get_db] = lambda: TeacherDashboardMockConnection(dummy_cursor)

        # Clear cached year_ids
        students_mod._YEAR_ID_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        students_mod._YEAR_ID_CACHE.clear()

    def test_student_access_forbidden(self):
        """학생 권한(role='student')으로 교사 대시보드 접근 시 403 FORBIDDEN 차단 검증"""
        student_token = create_access_token(
            data={"uuid": "student-uuid-1", "id": "student1", "role": "student"}
        )

        resp = self.client.get(
            "/api/teacher/dashboard",
            headers={"Authorization": f"Bearer {student_token}"},
        )
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"]["code"], "FORBIDDEN")

    def test_unauthenticated_access_unauthorized(self):
        """인증 헤더 없이 대시보드 접근 시 401 UNAUTHORIZED 차단 검증"""
        resp = self.client.get("/api/teacher/dashboard")
        self.assertEqual(resp.status_code, 401)

    def test_teacher_dashboard_homeroom_scope_success(self):
        """담임교사(2학년 3반) 대시보드 조회: 통계 집계 및 해당 학급 증빙 목록 반환 검증"""
        teacher_uuid = "teacher-uuid-1"
        teacher_token = create_access_token(
            data={"uuid": teacher_uuid, "id": "teacher1", "role": "teacher"}
        )

        teacher_row = {
            "teachers_id": 10,
            "name": "홍길동",
            "subject": "정보",
            "grade": 2,
            "class": 3,
            "email": "teacher1@school.hs.kr",
        }
        stats_row = {
            "pending_count": 3,
            "resubmitted_count": 1,
            "unscored_count": 3,
        }
        recent_rows = [
            {
                "submission_id": 501,
                "student_id": 12,
                "student_name": "김학생",
                "grade": 2,
                "class_no": 3,
                "number": 15,
                "area_id": 1,
                "area_name": "전공능력",
                "item_id": 101,
                "item_name": "정보처리기능사",
                "detail": "자격증 취득",
                "activity_date": datetime(2026, 9, 20).date(),
                "file_path": "submissions/2026/cert.pdf",
                "link_url": None,
                "description": "2026년 9월 취득 증빙",
                "status_code": 1,
                "granted_score": None,
                "created_at": datetime(2026, 10, 1, 14, 30, 0),
                "is_resubmitted": 0,
            },
            {
                "submission_id": 490,
                "student_id": 13,
                "student_name": "이학생",
                "grade": 2,
                "class_no": 3,
                "number": 16,
                "area_id": 2,
                "area_name": "외국어능력",
                "item_id": 102,
                "item_name": "TOEIC Speaking",
                "detail": "IM2 취득",
                "activity_date": datetime(2026, 9, 15).date(),
                "file_path": None,
                "link_url": "https://example.com/score",
                "description": "재제출합니다.",
                "status_code": 1,
                "granted_score": None,
                "created_at": datetime(2026, 9, 30, 11, 20, 0),
                "is_resubmitted": 1,
            },
        ]

        cursor = TeacherDashboardMockCursor(
            teacher_data=teacher_row,
            stats_data=stats_row,
            recent_data=recent_rows,
            year_id=101,
        )
        app.dependency_overrides[get_db] = lambda: TeacherDashboardMockConnection(cursor)

        resp = self.client.get(
            "/api/teacher/dashboard",
            headers={"Authorization": f"Bearer {teacher_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]

        # 지표 검증
        self.assertEqual(data["pendingCount"], 3)
        self.assertEqual(data["resubmittedCount"], 1)
        self.assertEqual(data["unscoredCount"], 3)
        self.assertEqual(data["scopeLabel"], "2학년 3반 (정보)")
        self.assertEqual(data["teacher"]["name"], "홍길동")
        self.assertEqual(data["teacher"]["grade"], 2)
        self.assertEqual(data["teacher"]["classNo"], 3)

        # 최근 제출 목록 검증
        self.assertEqual(len(data["recentSubmissions"]), 2)
        sub1 = data["recentSubmissions"][0]
        self.assertEqual(sub1["submissionId"], 501)
        self.assertEqual(sub1["studentName"], "김학생")
        self.assertEqual(sub1["area"], "전공능력")
        self.assertEqual(sub1["status"], "제출완료")
        self.assertFalse(sub1["isResubmitted"])

        sub2 = data["recentSubmissions"][1]
        self.assertEqual(sub2["submissionId"], 490)
        self.assertEqual(sub2["studentName"], "이학생")
        self.assertTrue(sub2["isResubmitted"])

    def test_teacher_not_found(self):
        """교사 권한이지만 teachers 테이블에 없는 경우 404 TEACHER_NOT_FOUND 반환 검증"""
        teacher_token = create_access_token(
            data={"uuid": "unknown-teacher", "id": "teacher99", "role": "teacher"}
        )
        cursor = TeacherDashboardMockCursor(teacher_data=None)
        app.dependency_overrides[get_db] = lambda: TeacherDashboardMockConnection(cursor)

        resp = self.client.get(
            "/api/teacher/dashboard",
            headers={"Authorization": f"Bearer {teacher_token}"},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "TEACHER_NOT_FOUND")

    def test_admin_scope_and_alias_route(self):
        """관리자 권한으로 복수형 별칭 URL(/api/teachers/dashboard) 호출 시 전체 학급 스코프 검증"""
        admin_uuid = "admin-uuid-99"
        admin_token = create_access_token(
            data={"uuid": admin_uuid, "id": "admin", "role": "admin"}
        )

        stats_row = {"pending_count": 25, "resubmitted_count": 5, "unscored_count": 20}
        cursor = TeacherDashboardMockCursor(
            teacher_data=None,
            stats_data=stats_row,
            recent_data=[],
            year_id=101,
        )
        app.dependency_overrides[get_db] = lambda: TeacherDashboardMockConnection(cursor)

        resp = self.client.get(
            "/api/teachers/dashboard",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["scopeLabel"], "전체 학급 (관리자)")
        self.assertEqual(data["pendingCount"], 25)
        self.assertEqual(data["resubmittedCount"], 5)
        self.assertEqual(data["unscoredCount"], 20)
        self.assertEqual(data["recentSubmissions"], [])


if __name__ == "__main__":
    unittest.main()
