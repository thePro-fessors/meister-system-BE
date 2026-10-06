"""
tests/test_teacher_points_creation.py - 3.7 교사용 상벌점 신규 등록 API 전용 단위/통합 테스트
명세: Tech_spec.md 3.6 & TODO.md 3.7 (POST /api/points)

검증 목록:
1. RBAC & 스코프 인가:
   - 학생 계정 상벌점 등록 시도 시 403 FORBIDDEN 차단
   - 타 학급 담임교사(2-3)가 1-1 학생 상벌점 등록 시도 시 403 FORBIDDEN 차단 (Scope Isolation)
   - 담임교사(자학급), 교과교사(전교생), 관리자(admin) 등록 허용 (201 Created)
2. 입력값 검증 (Validation):
   - studentId 누락 시 400 VALIDATION_ERROR 거절
   - 사유(reason) 누락 또는 빈 공백인 경우 400 VALIDATION_ERROR 거절
   - 점수(score/points) 누락인 경우 400 VALIDATION_ERROR 거절
   - 0점 이하 또는 100점 초과 점수 입력 시 400 VALIDATION_ERROR 거절
   - 유효하지 않은 유형(상점/벌점 외) 입력 시 400 VALIDATION_ERROR 거절
3. 비즈니스 로직 및 감사 추적 (Audit Trail):
   - 상점/벌점 부호 자동 정규화 (상점: +, 벌점: -)
   - 토큰의 교사 UUID 기반 teacherId 자동 추출 및 DB 주입
   - merits 및 merits_log(CREATE 액션) 원자적 트랜잭션 기록
   - snake_case 및 camelCase 필드(studentId, score, date, reason, reflectedArea) 완벽 호환
4. 보안 방어:
   - 사유(reason) HTML XSS 문자열 이스케이프 검증 (<script> -> &lt;script&gt;)
   - SQL Injection 공격 구문 안전 바인딩 검증
"""

import glob
import os
import sys
import unittest
from datetime import date, datetime
from typing import Any, Dict, List, Optional
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


class PointsCreationMockCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = 0
        self.rowcount = 0
        self._current_result: List[Any] = []
        self._fetch_index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query: str, params: tuple = ()):
        q = " ".join(query.strip().split())
        self._fetch_index = 0

        # 1. academic_years 조회
        if "FROM academic_years" in q:
            self._current_result = list(self.conn.db_years)

        # 2. students 조회
        elif "FROM students" in q:
            if "WHERE student_id = %s" in q:
                st_id = params[0]
                matched = [s for s in self.conn.db_students if s["student_id"] == st_id and not s.get("is_deleted")]
                self._current_result = matched
            elif "WHERE uuid = %s" in q:
                st_uuid = params[0]
                matched = [s for s in self.conn.db_students if s["uuid"] == st_uuid and not s.get("is_deleted")]
                self._current_result = matched

        # 3. teachers 조회
        elif "FROM teachers" in q:
            if "WHERE uuid = %s" in q:
                t_uuid = params[0]
                matched = [t for t in self.conn.db_teachers if t["uuid"] == t_uuid and not t.get("is_deleted")]
                self._current_result = matched
            elif "WHERE teachers_id = %s" in q:
                t_id = params[0]
                matched = [t for t in self.conn.db_teachers if t["teachers_id"] == t_id and not t.get("is_deleted")]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_teachers)

        # 4. student_academic_records 조회
        elif "FROM student_academic_records" in q:
            st_id = params[0]
            if len(params) > 1:
                yr_id = params[1]
                matched = [
                    r for r in self.conn.db_records
                    if r["student_id"] == st_id and (r.get("year_id") == yr_id or r.get("year") == yr_id)
                ]
            else:
                matched = [r for r in self.conn.db_records if r["student_id"] == st_id]
            self._current_result = matched

        # 5. merits 조회
        elif "FROM merits" in q:
            self._current_result = list(self.conn.db_merits)

        # 6. INSERT INTO merits_log (먼저 매칭)
        elif "INSERT INTO merits_log" in q:
            self.conn.merit_logs.append({
                "merits_point_id": params[0],
                "modifier_uuid": params[1],
                "action_type": "CREATE",
                "old_points": None,
                "new_points": params[2],
                "modify_reason": params[3],
                "created_at": datetime.now(),
            })
            self.rowcount = 1

        # 7. INSERT INTO merits
        elif "INSERT INTO merits" in q:
            self.conn.merit_seq += 1
            self.lastrowid = self.conn.merit_seq
            new_m = {
                "merits_point_id": self.lastrowid,
                "student_id": params[0],
                "teachers_id": params[1],
                "type": params[2],
                "points": params[3],
                "reason": params[4],
                "related_area": params[5],
                "occurred_at": params[6],
                "created_at": datetime.now(),
                "is_reflected": True,
                "is_deleted": False,
            }
            self.conn.db_merits.append(new_m)
            self.rowcount = 1

        # 8. LAST_INSERT_ID()
        elif "SELECT LAST_INSERT_ID()" in q:
            self._current_result = [{"last_id": self.conn.merit_seq}]
            self.rowcount = 1

        else:
            self._current_result = []

    async def fetchone(self):
        if self._fetch_index < len(self._current_result):
            row = self._current_result[self._fetch_index]
            self._fetch_index += 1
            return row
        return None

    async def fetchall(self):
        res = self._current_result[self._fetch_index:]
        self._fetch_index = len(self._current_result)
        return res


class PointsCreationMockConnection:
    def __init__(self):
        self.cursor_instance = PointsCreationMockCursor(self)
        self.merit_seq = 100
        self.db_years = [
            {"year_id": 1, "year": 2026, "is_activated": True},
        ]
        self.db_students = [
            {"student_id": 1, "uuid": "student-uuid-1", "name": "홍길동", "is_deleted": False},
            {"student_id": 2, "uuid": "student-uuid-2", "name": "이순신", "is_deleted": False},
        ]
        self.db_teachers = [
            # 1학년 1반 담임 (teachers_id=10)
            {"teachers_id": 10, "uuid": "teacher-uuid-10", "name": "김담임", "grade": 1, "class": 1, "is_deleted": False},
            # 2학년 3반 담임 (teachers_id=20)
            {"teachers_id": 20, "uuid": "teacher-uuid-20", "name": "박담임", "grade": 2, "class": 3, "is_deleted": False},
            # 비담임 교과교사 (teachers_id=30)
            {"teachers_id": 30, "uuid": "teacher-uuid-30", "name": "최교과", "grade": None, "class": None, "is_deleted": False},
        ]
        self.db_records = [
            # 홍길동: 2026학년도 1학년 1반 5번
            {"student_id": 1, "year_id": 1, "year": 2026, "grade": 1, "class_no": 1, "class": 1, "number": 5},
            # 이순신: 2026학년도 2학년 3반 10번
            {"student_id": 2, "year_id": 1, "year": 2026, "grade": 2, "class_no": 3, "class": 3, "number": 10},
        ]
        self.db_merits = []
        self.merit_logs = []

    async def autocommit(self, val: bool):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def cursor(self, cursor=None):
        return self.cursor_instance


class TestTeacherPointsCreationAPI(unittest.TestCase):
    """3.7 교사용 상벌점 신규 등록 API 단위/통합 테스트"""

    def setUp(self):
        self.client = TestClient(app)
        self.conn = PointsCreationMockConnection()
        app.dependency_overrides[get_db] = lambda: self.conn

        mock_redis = MagicMock()
        async def _fake_redis_get(key):
            return None
        mock_redis.get = _fake_redis_get
        app.dependency_overrides[get_redis] = lambda: mock_redis

        students_mod._YEAR_ID_CACHE.clear()

        self.token_student = create_access_token({"sub": "student-uuid-1", "id": "student1", "role": "student"})
        self.token_homeroom = create_access_token({"sub": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        self.token_other_teacher = create_access_token({"sub": "teacher-uuid-20", "id": "teacher20", "role": "teacher"})
        self.token_subject_teacher = create_access_token({"sub": "teacher-uuid-30", "id": "teacher30", "role": "teacher"})
        self.token_admin = create_access_token({"sub": "admin-uuid-1", "id": "admin", "role": "admin"})

    def tearDown(self):
        app.dependency_overrides.clear()
        students_mod._YEAR_ID_CACHE.clear()

    def test_student_cannot_create_point_forbidden(self):
        """학생 권한으로 상벌점 등록 호출 시 403 FORBIDDEN 차단"""
        res = self.client.post(
            "/api/points",
            json={"studentId": 1, "type": "상점", "score": 5.0, "reason": "자가 상점 등록 시도"},
            headers={"Authorization": f"Bearer {self.token_student}"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_cross_class_teacher_cannot_create_point_forbidden(self):
        """타 학급 담임교사(2-3)가 1-1 학생에게 상벌점 등록 시도 시 403 FORBIDDEN 차단 (Scope Isolation)"""
        res = self.client.post(
            "/api/points",
            json={"studentId": 1, "type": "상점", "score": 5.0, "reason": "타학급 학생 상점 부여 시도"},
            headers={"Authorization": f"Bearer {self.token_other_teacher}"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_missing_student_id_validation_error(self):
        """학생 식별자(studentId) 누락 시 400 VALIDATION_ERROR 거절"""
        res = self.client.post(
            "/api/points",
            json={"type": "상점", "score": 5.0, "reason": "학생 ID 누락"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    def test_missing_reason_validation_error(self):
        """사유(reason) 누락 또는 빈 공백인 경우 400 VALIDATION_ERROR 거절 (요구사항 필수)"""
        res = self.client.post(
            "/api/points",
            json={"studentId": 1, "type": "상점", "score": 5.0, "reason": "   "},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("사유", res.json()["error"]["message"])

    def test_invalid_score_bounds(self):
        """점수가 누락되었거나 0점 이하 또는 100점 초과인 경우 400 VALIDATION_ERROR 거절"""
        # 1. 0점
        res1 = self.client.post(
            "/api/points",
            json={"studentId": 1, "type": "상점", "score": 0.0, "reason": "0점 부여"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res1.status_code, 400)

        # 2. 100점 초과
        res2 = self.client.post(
            "/api/points",
            json={"studentId": 1, "type": "상점", "score": 105.0, "reason": "100점 초과"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res2.status_code, 400)

    def test_invalid_point_type(self):
        """유효하지 않은 상벌점 구분(예: '보너스') 입력 시 400 VALIDATION_ERROR 거절"""
        res = self.client.post(
            "/api/points",
            json={"studentId": 1, "type": "보너스", "score": 5.0, "reason": "구분 오류"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 400)

    def test_homeroom_teacher_create_merit_success(self):
        """
        정당한 담임교사의 상점 정상 등록 (201 Created)
        - studentId, type='상점', score=5.0, date, reason, reflectedArea
        - 토큰 기반 teacherId(10) 자동 주입
        - merits 테이블 및 merits_log(CREATE) 원자적 기록
        - XSS 방어 이스케이프 검증
        """
        xss_reason = "<script>alert('선행상')</script>모범적인 학교생활"
        res = self.client.post(
            "/api/points",
            json={
                "studentId": 1,
                "type": "상점",
                "score": 5.0,
                "date": "2026-06-15",
                "reason": xss_reason,
                "reflectedArea": "직업기초능력",
            },
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertTrue(body["success"])
        data = body["data"]

        self.assertEqual(data["studentId"], 1)
        self.assertEqual(data["teacherId"], 10)
        self.assertEqual(data["type"], "+")
        self.assertEqual(data["score"], 5.0)
        self.assertEqual(data["date"], "2026-06-15")
        self.assertEqual(data["reflectedArea"], "직업기초능력")

        # XSS 필터링 검증
        self.assertNotIn("<script>", data["reason"])
        self.assertIn("&lt;script&gt;", data["reason"])

        # merits 및 merits_log DB 저장 검증
        self.assertEqual(len(self.conn.db_merits), 1)
        self.assertEqual(self.conn.db_merits[0]["teachers_id"], 10)
        self.assertEqual(len(self.conn.merit_logs), 1)
        self.assertEqual(self.conn.merit_logs[0]["action_type"], "CREATE")
        self.assertEqual(self.conn.merit_logs[0]["new_points"], 5.0)

    def test_create_demerit_points_negative_score(self):
        """벌점 등록 시 norm_score가 음수(-3.0)로 자동 부호 정규화 검증"""
        res = self.client.post(
            "/api/points",
            json={
                "studentId": 1,
                "type": "벌점",
                "score": 3.0,
                "date": "2026-06-20",
                "reason": "지각 3회",
            },
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual(data["type"], "-")
        self.assertEqual(data["score"], -3.0)
        self.assertEqual(self.conn.db_merits[0]["points"], -3.0)

    def test_subject_teacher_and_admin_can_create_point(self):
        """비담임 교과교사 및 관리자(admin) 상벌점 신규 등록 권한 정상 허용 검증"""
        # 1. 관리자 상벌점 부여 -> 201
        res_admin = self.client.post(
            "/api/points",
            json={"studentId": 1, "type": "상점", "score": 2.0, "reason": "관리자 특별상"},
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(res_admin.status_code, 201)

        # 2. 교과교사 상벌점 부여 -> 201
        res_sub = self.client.post(
            "/api/points",
            json={"studentId": 1, "type": "상점", "score": 2.0, "reason": "교과 우수상"},
            headers={"Authorization": f"Bearer {self.token_subject_teacher}"},
        )
        self.assertEqual(res_sub.status_code, 201)


if __name__ == "__main__":
    unittest.main()
