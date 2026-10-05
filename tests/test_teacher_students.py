"""
tests/test_teacher_students.py - 담당 학생 목록 및 제출 현황 검색 API (GET /api/teacher/students) 단위/통합 테스트
관련 명세: Tech_spec.md 3.2 & TODO.md 3.2
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


class TeacherStudentsMockCursor:
    """SQL 쿼리 구문 매칭 기반 Mock Cursor"""

    def __init__(
        self,
        teacher_row: Optional[Dict[str, Any]] = None,
        year_id: int = 101,
        students: Optional[List[Dict[str, Any]]] = None,
        areas: Optional[List[Dict[str, Any]]] = None,
        submissions: Optional[List[Dict[str, Any]]] = None,
        merits: Optional[List[Dict[str, Any]]] = None,
    ):
        self.teacher_row = teacher_row
        self.year_id = year_id
        self.students = students or []
        self.areas = areas or []
        self.submissions = submissions or []
        self.merits = merits or []
        self._current_result: List[Any] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query: str, params: tuple = ()):
        q = " ".join(query.strip().split())

        # 1. 교사 조회
        if "FROM teachers" in q:
            self._current_result = [self.teacher_row] if self.teacher_row else []

        # 2. 학년도 조회
        elif "FROM academic_years" in q:
            self._current_result = [{"year_id": self.year_id}]

        # 3. 학생 및 학적 조회
        elif "FROM students st" in q and "JOIN student_academic_records sar" in q:
            matched = list(self.students)
            param_idx = 1  # 0번은 year_id

            if "AND sar.grade = %s" in q:
                g_val = params[param_idx]
                matched = [s for s in matched if s["grade"] == g_val]
                param_idx += 1

            if "AND sar.class = %s" in q:
                c_val = params[param_idx]
                matched = [s for s in matched if s["class_no"] == c_val]
                param_idx += 1

            if "AND st.name LIKE %s" in q:
                name_like = params[param_idx].strip("%")
                matched = [s for s in matched if name_like in s["name"]]
                param_idx += 1

            if "AND (sar.number = %s OR st.student_id = %s)" in q:
                num_val = params[param_idx]
                matched = [s for s in matched if s["number"] == num_val or s["student_id"] == num_val]
                param_idx += 2

            self._current_result = matched

        # 4. 인증 영역 조회
        elif "FROM certification_areas" in q:
            self._current_result = list(self.areas)

        # 5. 증빙 제출 내역 조회
        elif "FROM submissions s" in q and "JOIN evaluation_items ei" in q:
            # params: (year_id, *student_ids)
            st_ids = params[1:]
            matched = [s for s in self.submissions if s["student_id"] in st_ids]
            self._current_result = matched

        # 6. 상벌점 내역 조회
        elif "FROM merits" in q:
            # params: (*student_ids, merit_start, merit_end)
            st_ids = params[:-2]
            matched = [m for m in self.merits if m["student_id"] in st_ids]
            self._current_result = matched

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


class TeacherStudentsMockConnection:
    def __init__(self, cursor_mock: TeacherStudentsMockCursor):
        self.cursor_mock = cursor_mock

    def cursor(self, cursor=None):
        return self.cursor_mock


class TestTeacherStudentsAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_redis = MagicMock()

        async def _fake_redis_get(key):
            return None

        self.mock_redis.get = _fake_redis_get
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        # 기본 DB override (Null acquire 방지)
        dummy_cursor = TeacherStudentsMockCursor()
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(dummy_cursor)

        # 공통 테스트 데이터 세팅 (2026학년도)
        self.teacher_homeroom = {
            "teachers_id": 10,
            "name": "담임선생",
            "subject": "소프트웨어",
            "grade": 2,
            "class": 3,
            "email": "teacher10@school.hs.kr",
        }

        self.db_students = [
            {"student_id": 1, "name": "홍길동", "uuid": "st-uuid-1", "grade": 2, "class_no": 3, "number": 1},
            {"student_id": 2, "name": "성춘향", "uuid": "st-uuid-2", "grade": 2, "class_no": 3, "number": 2},
            {"student_id": 3, "name": "이몽룡", "uuid": "st-uuid-3", "grade": 1, "class_no": 1, "number": 1},
        ]

        self.db_areas = [
            {"area_id": 1, "year_id": 101, "grade": 2, "name": "전문기술역량", "max_score": 100.0},
            {"area_id": 2, "year_id": 101, "grade": 2, "name": "외국어능력", "max_score": 100.0},
        ]

        self.db_submissions = [
            # 홍길동: 정보처리기능사(인정완료 40점), 알고리즘대회(검토중 1건)
            {
                "submission_id": 101,
                "student_id": 1,
                "item_id": 11,
                "status_code": 3,
                "granted_score": 40.0,
                "area_id": 1,
                "area_name": "전문기술역량",
                "scoring_type": 1,
                "item_max_score": 50.0,
            },
            {
                "submission_id": 102,
                "student_id": 1,
                "item_id": 12,
                "status_code": 1,
                "granted_score": None,
                "area_id": 1,
                "area_name": "전문기술역량",
                "scoring_type": 8,
                "item_max_score": 50.0,
            },
            # 성춘향: 정보처리기능사(50점), 알고리즘(45점) -> 전문기술역량 95점(S등급 달성)
            #         토익(95점) -> 외국어능력 95점(S등급 달성) => 인증 가능
            {
                "submission_id": 103,
                "student_id": 2,
                "item_id": 11,
                "status_code": 3,
                "granted_score": 50.0,
                "area_id": 1,
                "area_name": "전문기술역량",
                "scoring_type": 1,
                "item_max_score": 50.0,
            },
            {
                "submission_id": 104,
                "student_id": 2,
                "item_id": 12,
                "status_code": 3,
                "granted_score": 45.0,
                "area_id": 1,
                "area_name": "전문기술역량",
                "scoring_type": 1,
                "item_max_score": 50.0,
            },
            {
                "submission_id": 105,
                "student_id": 2,
                "item_id": 13,
                "status_code": 3,
                "granted_score": 95.0,
                "area_id": 2,
                "area_name": "외국어능력",
                "scoring_type": 1,
                "item_max_score": 100.0,
            },
        ]

        self.db_merits = [
            # 홍길동: 상점 +3점
            {
                "merits_point_id": 201,
                "student_id": 1,
                "type": "+",
                "points": 3.0,
                "related_area": "전문기술역량",
                "occurred_at": date(2026, 5, 1),
            },
        ]

        students_mod._YEAR_ID_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        students_mod._YEAR_ID_CACHE.clear()

    def test_student_access_forbidden(self):
        """학생 권한(role='student')으로 학생 검색 API 접근 시 403 Forbidden 차단 검증"""
        token = create_access_token(data={"uuid": "student-uuid", "id": "st1", "role": "student"})
        resp = self.client.get("/api/teacher/students", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"]["code"], "FORBIDDEN")

    def test_unauthenticated_access_unauthorized(self):
        """인증 헤더 없이 접근 시 401 Unauthorized 차단 검증"""
        resp = self.client.get("/api/teacher/students")
        self.assertEqual(resp.status_code, 401)

    def test_teacher_not_found(self):
        """교사 권한이지만 teachers 테이블에 없는 경우 404 TEACHER_NOT_FOUND 검증"""
        token = create_access_token(data={"uuid": "unknown-teacher", "id": "tc99", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(teacher_row=None)
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get("/api/teacher/students", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "TEACHER_NOT_FOUND")

    def test_homeroom_teacher_scope_enforcement_and_calculations(self):
        """담임교사(2학년 3반) 기본 조회: 본인 학급 학생만 반환 및 점수/인증상태/대기건수/상벌점 계산 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(
            teacher_row=self.teacher_homeroom,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=self.db_submissions,
            merits=self.db_merits,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get("/api/teacher/students", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        data = body["data"]

        # 2학년 3반 학생인 홍길동, 성춘향 2명만 반환되어야 함 (1학년 1반 이몽룡 제외)
        self.assertEqual(len(data), 2)

        # 1. 홍길동 계산 검증
        # 전문기술역량(40점 + 상점 3점 = 43점) + 외국어(0점) = 총점 43점
        # 대기 건수 1건(알고리즘대회 검토중) -> certStatus: '검토중'
        # 상벌점: 3.0점
        st1 = data[0]
        self.assertEqual(st1["studentId"], 1)
        self.assertEqual(st1["name"], "홍길동")
        self.assertEqual(st1["grade"], 2)
        self.assertEqual(st1["classNo"], 3)
        self.assertEqual(st1["number"], 1)
        self.assertEqual(st1["totalScore"], 43.0)
        self.assertEqual(st1["certStatus"], "검토중")
        self.assertEqual(st1["pendingCount"], 1)
        self.assertEqual(st1["pointTotal"], 3.0)

        # 2. 성춘향 계산 검증
        # 전문기술(95점, S등급) + 외국어(95점, S등급) = 총점 190.0점
        # 전 영역 S등급 달성 -> certStatus: '인증 가능'
        # 대기 건수 0건, 상벌점 0.0점
        st2 = data[1]
        self.assertEqual(st2["studentId"], 2)
        self.assertEqual(st2["name"], "성춘향")
        self.assertEqual(st2["grade"], 2)
        self.assertEqual(st2["classNo"], 3)
        self.assertEqual(st2["number"], 2)
        self.assertEqual(st2["totalScore"], 190.0)
        self.assertEqual(st2["certStatus"], "인증 가능")
        self.assertEqual(st2["pendingCount"], 0)
        self.assertEqual(st2["pointTotal"], 0.0)

    def test_homeroom_scope_bypass_blocked(self):
        """담임교사(2-3)가 쿼리 파라미터로 타 학급(grade=1) 요청 시 우회 차단(403 FORBIDDEN) 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(teacher_row=self.teacher_homeroom)
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get(
            "/api/teacher/students?grade=1",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

        resp_class = self.client.get(
            "/api/teacher/students?classNo=2",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp_class.status_code, 403)
        self.assertEqual(resp_class.json()["error"]["code"], "FORBIDDEN")

    def test_admin_custom_filtering(self):
        """관리자 권한으로 타 학급(1학년 1반) 학생 검색 및 복수형 URL(/api/teachers/students) 검증"""
        admin_token = create_access_token(data={"uuid": "admin-uuid", "id": "admin", "role": "admin"})
        cursor = TeacherStudentsMockCursor(
            teacher_row=None,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=[],
            merits=[],
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get(
            "/api/teachers/students?grade=1&classNo=1",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "이몽룡")
        self.assertEqual(data[0]["grade"], 1)
        self.assertEqual(data[0]["classNo"], 1)

    def test_filter_by_name(self):
        """학생 성명 검색 필터(name='춘향') 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(
            teacher_row=self.teacher_homeroom,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=self.db_submissions,
            merits=self.db_merits,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get(
            "/api/teacher/students?name=춘향",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "성춘향")

    def test_filter_by_student_number(self):
        """학생 번호 필터(studentNo=1) 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(
            teacher_row=self.teacher_homeroom,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=self.db_submissions,
            merits=self.db_merits,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get(
            "/api/teacher/students?studentNo=1",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "홍길동")
        self.assertEqual(data[0]["number"], 1)

    def test_filter_by_cert_status(self):
        """인증 상태 필터(status='인증 가능') 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(
            teacher_row=self.teacher_homeroom,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=self.db_submissions,
            merits=self.db_merits,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get(
            "/api/teacher/students?status=인증 가능",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "성춘향")
        self.assertEqual(data[0]["certStatus"], "인증 가능")

    def test_filter_by_area(self):
        """인증 영역 필터(area='외국어능력') 검증: 외국어능력 제출건이 있는 성춘향만 반환"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(
            teacher_row=self.teacher_homeroom,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=self.db_submissions,
            merits=self.db_merits,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get(
            "/api/teacher/students?area=외국어능력",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "성춘향")

    def test_filter_by_submission_status(self):
        """제출 상태 필터(status='검토중') 검증: 검토 대기건이 있는 홍길동 반환"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(
            teacher_row=self.teacher_homeroom,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=self.db_submissions,
            merits=self.db_merits,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get(
            "/api/teacher/students?status=검토중",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "홍길동")
        self.assertEqual(data[0]["pendingCount"], 1)

    def test_filter_by_has_points(self):
        """상벌점 보유 여부 필터(hasPoints=true / hasPoints=false) 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(
            teacher_row=self.teacher_homeroom,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=self.db_submissions,
            merits=self.db_merits,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        # 1. hasPoints=true -> 홍길동만 반환
        resp_true = self.client.get(
            "/api/teacher/students?hasPoints=true",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp_true.status_code, 200)
        data_true = resp_true.json()["data"]
        self.assertEqual(len(data_true), 1)
        self.assertEqual(data_true[0]["name"], "홍길동")
        self.assertEqual(data_true[0]["pointTotal"], 3.0)

        # 2. hasPoints=false -> 성춘향만 반환
        cursor_false = TeacherStudentsMockCursor(
            teacher_row=self.teacher_homeroom,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=self.db_submissions,
            merits=self.db_merits,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor_false)
        resp_false = self.client.get(
            "/api/teacher/students?hasPoints=false",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp_false.status_code, 200)
        data_false = resp_false.json()["data"]
        self.assertEqual(len(data_false), 1)
        self.assertEqual(data_false[0]["name"], "성춘향")
        self.assertEqual(data_false[0]["pointTotal"], 0.0)

    def test_empty_results(self):
        """조건에 부합하는 학생이 없는 경우 빈 리스트([]) 200 OK 반환 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentsMockCursor(
            teacher_row=self.teacher_homeroom,
            year_id=101,
            students=self.db_students,
            areas=self.db_areas,
            submissions=[],
            merits=[],
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentsMockConnection(cursor)

        resp = self.client.get(
            "/api/teacher/students?name=존재하지않는학생",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"], [])


if __name__ == "__main__":
    unittest.main()
