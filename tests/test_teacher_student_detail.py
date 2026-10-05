"""
tests/test_teacher_student_detail.py - 교사용 특정 학생 상세 현황 및 제출 목록 조회 API 단위/통합 테스트
관련 명세: Tech_spec.md 3.3 & TODO.md 3.3 (GET /api/teacher/students/{studentId} & /api/teachers/students/{studentId})
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


class TeacherStudentDetailMockCursor:
    """SQL 쿼리 구문 매칭 기반 Mock Cursor"""

    def __init__(
        self,
        teacher_row: Optional[Dict[str, Any]] = None,
        year_id: Optional[int] = 101,
        student_row: Optional[Dict[str, Any]] = None,
        academic_record: Optional[Dict[str, Any]] = None,
        areas: Optional[List[Dict[str, Any]]] = None,
        submissions: Optional[List[Dict[str, Any]]] = None,
        merits: Optional[List[Dict[str, Any]]] = None,
    ):
        self.teacher_row = teacher_row
        self.year_id = year_id
        self.student_row = student_row
        self.academic_record = academic_record
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
            if self.year_id is not None:
                self._current_result = [{"year_id": self.year_id}]
            else:
                self._current_result = []

        # 3. 학생 기본 정보 조회
        elif "FROM students" in q:
            self._current_result = [self.student_row] if self.student_row else []

        # 4. 학적 정보 조회
        elif "FROM student_academic_records" in q:
            self._current_result = [self.academic_record] if self.academic_record else []

        # 5. 인증 영역 조회
        elif "FROM certification_areas" in q:
            self._current_result = list(self.areas)

        # 6. 증빙 제출 목록 조회
        elif "FROM submissions s" in q and "JOIN evaluation_items ei" in q:
            self._current_result = list(self.submissions)

        # 7. 상벌점 목록 조회
        elif "FROM merits" in q:
            self._current_result = list(self.merits)

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


class TeacherStudentDetailMockConnection:
    def __init__(self, cursor: TeacherStudentDetailMockCursor):
        self._cursor = cursor

    def cursor(self, *args, **kwargs):
        return self._cursor


class TestTeacherStudentDetailAPI(unittest.TestCase):
    """3.3 특정 학생 상세 현황 및 제출 목록 조회 API 단위/통합 테스트"""

    def setUp(self):
        self.client = TestClient(app)
        students_mod._YEAR_ID_CACHE.clear()

        # 모의 Redis 설정
        mock_redis = MagicMock()
        async def _fake_redis_get(key):
            return None
        mock_redis.get = _fake_redis_get
        app.dependency_overrides[get_redis] = lambda: mock_redis

        # 기본 DB override
        dummy_cursor = TeacherStudentDetailMockCursor()
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(dummy_cursor)

        # 공통 기본 데이터
        self.teacher_homeroom = {
            "teachers_id": 10,
            "uuid": "teacher-uuid-10",
            "name": "홍길동교사",
            "subject": "소프트웨어",
            "grade": 2,
            "class": 3,
            "class_num": 3,
            "email": "teacher10@school.hs.kr",
        }

        self.teacher_subject_only = {
            "teachers_id": 20,
            "uuid": "teacher-uuid-20",
            "name": "김교과교사",
            "subject": "데이터베이스",
            "grade": None,
            "class": None,
            "class_num": None,
            "email": "teacher20@school.hs.kr",
        }

        self.student_2_3 = {
            "student_id": 1,
            "uuid": "student-uuid-1",
            "name": "성춘향",
            "email": "student1@school.hs.kr",
            "is_deleted": False,
        }

        self.academic_record_2_3 = {
            "grade": 2,
            "class_no": 3,
            "number": 15,
        }

        self.student_1_1 = {
            "student_id": 2,
            "uuid": "student-uuid-2",
            "name": "이몽룡",
            "email": "student2@school.hs.kr",
            "is_deleted": False,
        }

        self.academic_record_1_1 = {
            "grade": 1,
            "class_no": 1,
            "number": 5,
        }

        self.areas_fixture = [
            {"area_id": 1, "year_id": 101, "grade": 2, "name": "기초직무역량", "max_score": 100.0},
            {"area_id": 2, "year_id": 101, "grade": 2, "name": "전문기술역량", "max_score": 150.0},
            {"area_id": 3, "year_id": 101, "grade": 2, "name": "외국어능력", "max_score": 80.0},
            {"area_id": 4, "year_id": 101, "grade": 2, "name": "인성/직업의식", "max_score": 70.0},
            {"area_id": 5, "year_id": 101, "grade": 2, "name": "창의적체험활동", "max_score": 50.0},
        ]

    def tearDown(self):
        app.dependency_overrides.clear()
        students_mod._YEAR_ID_CACHE.clear()

    def test_unauthenticated_access_blocked(self):
        """인증 토큰 없이 접근 시 401 Unauthorized 차단 검증"""
        resp = self.client.get("/api/teacher/students/1")
        self.assertEqual(resp.status_code, 401)

    def test_student_role_access_blocked(self):
        """학생 권한 토큰으로 교사용 API 접근 시 403 Forbidden 차단 검증"""
        student_token = create_access_token(data={"uuid": "student-uuid-1", "id": "student1", "role": "student"})
        resp = self.client.get("/api/teacher/students/1", headers={"Authorization": f"Bearer {student_token}"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

    def test_teacher_not_found(self):
        """교사 DB에 등록되지 않은 비관리자 계정 접근 시 404 TEACHER_NOT_FOUND 검증"""
        token = create_access_token(data={"uuid": "unknown-teacher", "id": "teacherX", "role": "teacher"})
        cursor = TeacherStudentDetailMockCursor(teacher_row=None)
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/1", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "TEACHER_NOT_FOUND")

    def test_student_not_found(self):
        """존재하지 않거나 삭제된 학생 ID 조회 시 404 USER_NOT_FOUND 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentDetailMockCursor(
            teacher_row=self.teacher_homeroom,
            student_row=None,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/999", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "USER_NOT_FOUND")

    def test_academic_year_not_found(self):
        """시스템에 존재하지 않는 학년도 요청 시 404 ITEM_NOT_FOUND 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentDetailMockCursor(
            teacher_row=self.teacher_homeroom,
            student_row=self.student_2_3,
            year_id=None,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/1?year=1990", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "ITEM_NOT_FOUND")

    def test_student_academic_record_not_found(self):
        """해당 학년도에 학생 학적 정보가 없는 경우 404 USER_NOT_FOUND 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentDetailMockCursor(
            teacher_row=self.teacher_homeroom,
            student_row=self.student_2_3,
            year_id=101,
            academic_record=None,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/1", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "USER_NOT_FOUND")

    def test_homeroom_teacher_cross_grade_blocked(self):
        """담임교사(2학년 3반)가 타 학년(1학년 1반) 학생 상세 조회 시 403 FORBIDDEN 차단 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        cursor = TeacherStudentDetailMockCursor(
            teacher_row=self.teacher_homeroom,
            student_row=self.student_1_1,
            academic_record=self.academic_record_1_1,
            year_id=101,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/2", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")
        self.assertIn("담당 학급", resp.json()["error"]["message"])

    def test_homeroom_teacher_cross_class_blocked(self):
        """담임교사(2학년 3반)가 같은 학년 타 학급(2학년 1반) 학생 상세 조회 시 403 FORBIDDEN 차단 검증"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        student_2_1 = {"student_id": 3, "uuid": "st-3", "name": "홍길동", "email": "st3@school.hs.kr", "is_deleted": False}
        academic_2_1 = {"grade": 2, "class_no": 1, "number": 10}

        cursor = TeacherStudentDetailMockCursor(
            teacher_row=self.teacher_homeroom,
            student_row=student_2_1,
            academic_record=academic_2_1,
            year_id=101,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/3", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

    def test_homeroom_teacher_own_student_success(self):
        """담임교사(2학년 3반)가 본인 학급 학생(2학년 3반) 상세 정상 조회 (200 OK, 영역별 점수/등급 및 제출목록 반환)"""
        token = create_access_token(data={"uuid": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})

        submissions_fixture = [
            {
                "submission_id": 501,
                "student_id": 1,
                "year": 2026,
                "area_id": 2,
                "area_name": "전문기술역량",
                "item_id": 201,
                "item_name": "정보처리기능사",
                "scoring_type": 1,
                "item_max_score": 150.0,
                "detail": "정보처리기능사 자격증 취득",
                "activity_date": date(2026, 4, 15),
                "description": "국가기술자격 취득 증빙",
                "file_path": "/uploads/cert_501.pdf",
                "original_filename": "cert.pdf",
                "link_url": "https://q-net.or.kr/cert/12345",
                "status_code": 3,
                "granted_score": 140.0,
                "teacher_comment": "자격 취득 확인 완료",
                "created_at": datetime(2026, 4, 20, 10, 30, 0),
                "reviewed_at": datetime(2026, 4, 22, 14, 0, 0),
                "reviewer_id": 10,
            },
            {
                "submission_id": 502,
                "student_id": 1,
                "year": 2026,
                "area_id": 3,
                "area_name": "외국어능력",
                "item_id": 301,
                "item_name": "TOEIC Speaking",
                "scoring_type": 8,  # 최상위인정형
                "item_max_score": 80.0,
                "detail": "TOEIC Speaking IM3",
                "activity_date": date(2026, 5, 10),
                "description": "어학 성적표 제출",
                "file_path": "/uploads/toeic_502.pdf",
                "original_filename": "toeic.pdf",
                "link_url": None,
                "status_code": 2,  # 검토중
                "granted_score": None,
                "teacher_comment": None,
                "created_at": datetime(2026, 5, 12, 9, 0, 0),
                "reviewed_at": None,
                "reviewer_id": None,
            },
        ]

        merits_fixture = [
            {
                "merits_point_id": 10,
                "type": "+",
                "points": 5.0,
                "related_area": "인성/직업의식",
                "occurred_at": date(2026, 4, 1),
            },
            {
                "merits_point_id": 11,
                "type": "벌점",
                "points": 2.0,
                "related_area": "인성/직업의식",
                "occurred_at": date(2026, 4, 10),
            },
        ]

        cursor = TeacherStudentDetailMockCursor(
            teacher_row=self.teacher_homeroom,
            student_row=self.student_2_3,
            academic_record=self.academic_record_2_3,
            year_id=101,
            areas=self.areas_fixture,
            submissions=submissions_fixture,
            merits=merits_fixture,
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/1", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)

        body = resp.json()
        self.assertTrue(body["success"])
        data = body["data"]

        # 기본 학생 정보 검증
        self.assertEqual(data["studentId"], 1)
        self.assertEqual(data["name"], "성춘향")
        self.assertEqual(data["grade"], 2)
        self.assertEqual(data["classNo"], 3)
        self.assertEqual(data["number"], 15)

        # 상벌점 및 대기건수 검증 (+5 -2 = +3)
        self.assertEqual(data["pointTotal"], 3.0)
        self.assertEqual(data["pendingCount"], 1)

        # 5대 영역 검증
        areas = data["areas"]
        self.assertEqual(len(areas), 5)
        area_names = [a["area"] for a in areas]
        self.assertIn("전문기술역량", area_names)
        self.assertIn("외국어능력", area_names)
        self.assertIn("인성/직업의식", area_names)

        # 전문기술역량: 140 / 150 = 93.3% -> S 등급, 달성
        tech_area = next(a for a in areas if a["area"] == "전문기술역량")
        self.assertEqual(tech_area["score"], 140.0)
        self.assertEqual(tech_area["grade"], "S")
        self.assertEqual(tech_area["status"], "달성")

        # 인성/직업의식: 상벌점 +3점 반영
        char_area = next(a for a in areas if a["area"] == "인성/직업의식")
        self.assertEqual(char_area["score"], 3.0)

        # 증빙 제출 목록 검증
        submissions = data["submissions"]
        self.assertEqual(len(submissions), 2)
        sub1 = submissions[0]
        self.assertEqual(sub1["id"], 501)
        self.assertEqual(sub1["area"], "전문기술역량")
        self.assertEqual(sub1["itemName"], "정보처리기능사")
        self.assertEqual(sub1["status"], "인정완료")
        self.assertEqual(sub1["score"], 140.0)
        self.assertEqual(sub1["teacherComment"], "자격 취득 확인 완료")
        self.assertIsNotNone(sub1["submittedAt"])
        self.assertIsNotNone(sub1["reviewedAt"])

        sub2 = submissions[1]
        self.assertEqual(sub2["id"], 502)
        self.assertEqual(sub2["status"], "검토중")
        self.assertIsNone(sub2["score"])

    def test_admin_can_access_any_student(self):
        """관리자(admin)는 타 학급/타 학년 학생을 자유롭게 조회 가능 (200 OK)"""
        admin_token = create_access_token(data={"uuid": "admin-uuid", "id": "admin", "role": "admin"})

        cursor = TeacherStudentDetailMockCursor(
            teacher_row=None,  # 관리자는 teachers 행이 없어도 접근 가능
            student_row=self.student_1_1,
            academic_record=self.academic_record_1_1,
            year_id=101,
            areas=self.areas_fixture,
            submissions=[],
            merits=[],
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/2", headers={"Authorization": f"Bearer {admin_token}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["name"], "이몽룡")

    def test_subject_teacher_without_homeroom_can_access_any_student(self):
        """담임이 배정되지 않은 교과교사(grade=None, class=None)는 전교생 상세 조회 가능 (200 OK)"""
        token = create_access_token(data={"uuid": "teacher-uuid-20", "id": "teacher20", "role": "teacher"})

        cursor = TeacherStudentDetailMockCursor(
            teacher_row=self.teacher_subject_only,
            student_row=self.student_1_1,
            academic_record=self.academic_record_1_1,
            year_id=101,
            areas=self.areas_fixture,
            submissions=[],
            merits=[],
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/2", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["name"], "이몽룡")

    def test_plural_alias_endpoint(self):
        """복수형 별칭 엔드포인트(/api/teachers/students/{studentId}) 정상 동작 검증"""
        admin_token = create_access_token(data={"uuid": "admin-uuid", "id": "admin", "role": "admin"})

        cursor = TeacherStudentDetailMockCursor(
            teacher_row=None,
            student_row=self.student_2_3,
            academic_record=self.academic_record_2_3,
            year_id=101,
            areas=self.areas_fixture,
            submissions=[],
            merits=[],
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teachers/students/1", headers={"Authorization": f"Bearer {admin_token}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["studentId"], 1)

    def test_custom_year_query_parameter(self):
        """year 쿼리 파라미터 지정 시 해당 학년도 조회 동작 검증"""
        admin_token = create_access_token(data={"uuid": "admin-uuid", "id": "admin", "role": "admin"})

        cursor = TeacherStudentDetailMockCursor(
            teacher_row=None,
            student_row=self.student_2_3,
            academic_record=self.academic_record_2_3,
            year_id=105,
            areas=self.areas_fixture,
            submissions=[],
            merits=[],
        )
        app.dependency_overrides[get_db] = lambda: TeacherStudentDetailMockConnection(cursor)

        resp = self.client.get("/api/teacher/students/1?year=2025", headers={"Authorization": f"Bearer {admin_token}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["year"], 2025)


if __name__ == "__main__":
    unittest.main()
