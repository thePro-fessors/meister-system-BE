"""
tests/test_history_and_points.py - 내 제출 내역 조회 및 상벌점 내역 조회 API 단위/통합 테스트

검증 대상:
1. GET /api/submissions (내 제출 내역 조회)
2. GET /api/students/{studentId}/submissions (학생 제출 내역 조회)
3. GET /api/points (내 상벌점 내역 조회)
4. GET /api/students/{studentId}/points (학생 상벌점 내역 조회)
5. 본인 소유권 검증 및 타 학생 조회 차단 (403 Forbidden)
6. 교사/관리자의 특정 학생 조회 허용 (200 OK) 및 studentId 파라미터 누락 방어 (400 Bad Request)
7. 학사년도(year) 필터링 검증
8. FE 호환 필드 매핑 검증 (filePath/fileUrl, linkUrl/link, pointId/id, points/score, 한글 상태명 등)
"""

import unittest
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from core.security import get_current_user
from database import get_db, get_redis
from main import app


class MockRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = val
        return True

    async def delete(self, key):
        self.data.pop(key, None)


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

        # 1. 학생 조회 (student_id 또는 uuid)
        if "FROM students" in sql_clean:
            if "WHERE student_id = %s" in sql_clean:
                target_id = params[0]
                matched = [s for s in self.conn.db_students if s["student_id"] == target_id and not s["is_deleted"]]
                self._current_result = matched
            elif "WHERE uuid = %s" in sql_clean:
                target_uuid = params[0]
                matched = [s for s in self.conn.db_students if s["uuid"] == target_uuid and not s["is_deleted"]]
                self._current_result = matched

        # 2. 제출 내역 조회 (submissions JOIN evaluation_items JOIN certification_areas JOIN academic_years)
        elif "FROM submissions s" in sql_clean and "JOIN evaluation_items ei" in sql_clean:
            st_id = params[0]
            year_filter = params[1] if len(params) > 1 else None

            matched = []
            for sub in self.conn.db_submissions:
                if sub["student_id"] != st_id or sub.get("is_deleted"):
                    continue

                item = next((it for it in self.conn.db_items if it["item_id"] == sub["item_id"]), None)
                if not item:
                    continue

                area = next((ar for ar in self.conn.db_areas if ar["area_id"] == item["area_id"]), None)
                if not area:
                    continue

                ay = next((y for y in self.conn.db_academic_years if y["year_id"] == area["year_id"]), None)
                if not ay:
                    continue

                if year_filter is not None and ay["year"] != year_filter:
                    continue

                matched.append({
                    "submission_id": sub["submission_id"],
                    "student_id": sub["student_id"],
                    "year": ay["year"],
                    "area_name": area["name"],
                    "item_id": sub["item_id"],
                    "item_name": item["name"],
                    "detail": sub.get("detail"),
                    "activity_date": sub.get("activity_date"),
                    "description": sub.get("description"),
                    "file_path": sub.get("file_path"),
                    "original_filename": sub.get("original_filename"),
                    "link_url": sub.get("link_url"),
                    "status_code": sub.get("status_code", 1),
                    "granted_score": sub.get("granted_score"),
                    "teacher_comment": sub.get("teacher_comment"),
                    "created_at": sub.get("created_at"),
                    "reviewed_at": sub.get("reviewed_at"),
                    "reviewer_id": sub.get("reviewer_id"),
                })
            # 최신순 정렬
            matched.sort(key=lambda x: (x["created_at"], x["submission_id"]), reverse=True)
            self._current_result = matched

        # 3. 상벌점 내역 조회 (merits LEFT JOIN teachers)
        elif "FROM merits m" in sql_clean and "JOIN teachers t" in sql_clean:
            st_id = params[0]
            start_date = params[1] if len(params) > 1 else None
            end_date = params[2] if len(params) > 2 else None

            matched = []
            for m in self.conn.db_merits:
                if m["student_id"] != st_id or m.get("is_deleted"):
                    continue

                occ = m.get("occurred_at")
                if start_date is not None and end_date is not None:
                    if not (start_date <= occ < end_date):
                        continue

                teacher = next((t for t in self.conn.db_teachers if t["teachers_id"] == m.get("teachers_id")), None)
                teacher_name = teacher["name"] if teacher else None

                matched.append({
                    "merits_point_id": m["merits_point_id"],
                    "student_id": m["student_id"],
                    "teachers_id": m.get("teachers_id"),
                    "teacher_name": teacher_name,
                    "type": m.get("type"),
                    "points": m.get("points"),
                    "reason": m.get("reason"),
                    "related_area": m.get("related_area"),
                    "occurred_at": m.get("occurred_at"),
                    "created_at": m.get("created_at"),
                    "is_reflected": m.get("is_reflected", True),
                })
            matched.sort(key=lambda x: (x["occurred_at"], x["merits_point_id"]), reverse=True)
            self._current_result = matched

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


class MockConnection:
    def __init__(self):
        self.db_academic_years = [
            {"year_id": 1, "year": 2025, "is_activated": False},
            {"year_id": 2, "year": 2026, "is_activated": True},
        ]
        self.db_areas = [
            {"area_id": 1, "year_id": 2, "grade": 1, "name": "전문기술역량"},
            {"area_id": 2, "year_id": 2, "grade": 1, "name": "직업기초역량"},
            {"area_id": 3, "year_id": 1, "grade": 1, "name": "과거역량"},
        ]
        self.db_items = [
            {"item_id": 10, "area_id": 1, "name": "정보처리기능사 취득"},
            {"item_id": 11, "area_id": 2, "name": "외국어 시험"},
            {"item_id": 12, "area_id": 3, "name": "과거 활동"},
        ]
        self.db_students = [
            {"student_id": 1, "uuid": "student-uuid-001", "name": "홍길동", "is_deleted": False},
            {"student_id": 2, "uuid": "student-uuid-002", "name": "이순신", "is_deleted": False},
        ]
        self.db_teachers = [
            {"teachers_id": 101, "uuid": "teacher-uuid-101", "name": "김선생", "is_deleted": False},
        ]
        self.db_submissions = [
            {
                "submission_id": 1,
                "student_id": 1,
                "item_id": 10,
                "detail": "정처기 취득",
                "activity_date": date(2026, 4, 15),
                "file_path": "/uploads/submissions/2026/04/cert.pdf",
                "original_filename": "cert.pdf",
                "link_url": "https://q-net.or.kr",
                "description": "최종 합격",
                "status_code": 3,
                "granted_score": 10.0,
                "teacher_comment": "승인 완료",
                "created_at": datetime(2026, 4, 16, 10, 0, 0),
                "reviewed_at": datetime(2026, 4, 17, 14, 30, 0),
                "reviewer_id": 101,
                "is_deleted": False,
            },
            {
                "submission_id": 2,
                "student_id": 1,
                "item_id": 11,
                "detail": "토익 800",
                "activity_date": date(2026, 5, 20),
                "file_path": "/uploads/submissions/2026/05/toeic.pdf",
                "original_filename": "toeic.pdf",
                "link_url": None,
                "description": "어학 성적",
                "status_code": 1,
                "granted_score": None,
                "teacher_comment": None,
                "created_at": datetime(2026, 5, 21, 9, 0, 0),
                "reviewed_at": None,
                "reviewer_id": None,
                "is_deleted": False,
            },
            {
                "submission_id": 3,
                "student_id": 1,
                "item_id": 12,
                "detail": "2025 과거 활동",
                "activity_date": date(2025, 9, 10),
                "file_path": None,
                "original_filename": None,
                "link_url": "https://example.com",
                "description": "작년 활동",
                "status_code": 3,
                "granted_score": 5.0,
                "teacher_comment": "확인",
                "created_at": datetime(2025, 9, 11, 10, 0, 0),
                "reviewed_at": datetime(2025, 9, 12, 10, 0, 0),
                "reviewer_id": 101,
                "is_deleted": False,
            },
            {
                "submission_id": 4,
                "student_id": 2,
                "item_id": 10,
                "detail": "이순신의 제출",
                "activity_date": date(2026, 6, 1),
                "file_path": None,
                "original_filename": None,
                "link_url": None,
                "description": "타인 제출",
                "status_code": 2,
                "granted_score": None,
                "teacher_comment": None,
                "created_at": datetime(2026, 6, 2, 11, 0, 0),
                "reviewed_at": None,
                "reviewer_id": None,
                "is_deleted": False,
            },
        ]
        self.db_merits = [
            {
                "merits_point_id": 1,
                "student_id": 1,
                "teachers_id": 101,
                "type": "+",
                "points": 2.0,
                "reason": "교내 프로그래밍 대회 최우수상",
                "related_area": "전문기술역량",
                "occurred_at": date(2026, 5, 10),
                "created_at": datetime(2026, 5, 10, 15, 0, 0),
                "is_reflected": True,
                "is_deleted": False,
            },
            {
                "merits_point_id": 2,
                "student_id": 1,
                "teachers_id": 101,
                "type": "-",
                "points": 1.0,
                "reason": "지각 3회",
                "related_area": "직업기초역량",
                "occurred_at": date(2026, 6, 15),
                "created_at": datetime(2026, 6, 15, 16, 0, 0),
                "is_reflected": True,
                "is_deleted": False,
            },
            {
                "merits_point_id": 3,
                "student_id": 1,
                "teachers_id": 101,
                "type": "상점",
                "points": 3.0,
                "reason": "2025 과거 상점",
                "related_area": "과거역량",
                "occurred_at": date(2025, 10, 5),
                "created_at": datetime(2025, 10, 5, 12, 0, 0),
                "is_reflected": True,
                "is_deleted": False,
            },
            {
                "merits_point_id": 4,
                "student_id": 2,
                "teachers_id": 101,
                "type": "+",
                "points": 5.0,
                "reason": "타 학생 상점",
                "related_area": "전문기술역량",
                "occurred_at": date(2026, 7, 1),
                "created_at": datetime(2026, 7, 1, 10, 0, 0),
                "is_reflected": True,
                "is_deleted": False,
            },
        ]

    def cursor(self, cursor=None):
        return MockCursor(self)

    async def autocommit(self, val: bool):
        pass


class TestHistoryAndPointsAPI(unittest.TestCase):
    def setUp(self):
        self.mock_conn = MockConnection()
        self.mock_redis = MockRedis()

        app.dependency_overrides[get_db] = lambda: self.mock_conn
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        self.current_user = {
            "uuid": "student-uuid-001",
            "id": "student1",
            "name": "홍길동",
            "role": 0,
        }
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    # ==========================================================================
    # 1. 내 제출 내역 조회 API (GET /api/submissions) 테스트
    # ==========================================================================
    def test_get_submissions_success_all_and_field_specs(self):
        """학생 본인의 전체 제출 내역 조회 및 FE 호환 필드 규격 검증"""
        resp = self.client.get("/api/submissions")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        data = body["data"]

        # 학생 1의 제출 내역은 총 3건
        self.assertEqual(len(data), 3)

        # 최신순 정렬 확인 (submission_id 2 -> 1 -> 3)
        self.assertEqual(data[0]["id"], 2)
        self.assertEqual(data[1]["id"], 1)
        self.assertEqual(data[2]["id"], 3)

        # 필드 규격 및 듀얼 매핑 검증
        first = data[0]
        self.assertEqual(first["studentId"], 1)
        self.assertEqual(first["year"], 2026)
        self.assertEqual(first["area"], "직업기초역량")
        self.assertEqual(first["itemId"], 11)
        self.assertEqual(first["itemName"], "외국어 시험")
        self.assertEqual(first["detail"], "토익 800")
        self.assertEqual(first["status"], "제출완료")
        self.assertEqual(first["filePath"], first["fileUrl"])
        self.assertEqual(first["linkUrl"], first["link"])

        # 승인 완료 건 상태 및 점수 매핑 확인
        approved = data[1]
        self.assertEqual(approved["id"], 1)
        self.assertEqual(approved["status"], "인정완료")
        self.assertEqual(approved["score"], 10.0)
        self.assertEqual(approved["teacherComment"], "승인 완료")
        self.assertEqual(approved["reviewerId"], 101)

    def test_get_submissions_year_filtering(self):
        """2026 학년도 필터 지정 시 2026년 데이터만 2건 필터링 확인"""
        resp = self.client.get("/api/submissions?year=2026")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 2)
        for item in data:
            self.assertEqual(item["year"], 2026)

    def test_get_submissions_other_student_forbidden(self):
        """학생이 타 학생의 studentId 파라미터로 조회 시 403 FORBIDDEN 차단"""
        resp = self.client.get("/api/submissions?studentId=2")
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"]["code"], "FORBIDDEN")

    def test_get_student_submissions_path_route(self):
        """학생 하위 라우트(GET /api/students/{id}/submissions) 정상 조회 및 타인 차단"""
        # 본인 조회 -> 200 OK
        resp = self.client.get("/api/students/1/submissions")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["data"]), 3)

        # 타인 조회 -> 403 Forbidden
        resp_other = self.client.get("/api/students/2/submissions")
        self.assertEqual(resp_other.status_code, 403)

    def test_teacher_get_submissions_delegation(self):
        """교사 권한: studentId 지정 시 해당 학생 조회 성공(200), 미지정 시 400 차단"""
        self.current_user = {
            "uuid": "teacher-uuid-101",
            "id": "teacher1",
            "name": "김선생",
            "role": 1,
        }
        # studentId 지정 -> 성공
        resp = self.client.get("/api/submissions?studentId=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["studentId"], 2)

        # studentId 미지정 -> 400 VALIDATION_ERROR
        resp_missing = self.client.get("/api/submissions")
        self.assertEqual(resp_missing.status_code, 400)
        self.assertEqual(resp_missing.json()["error"]["code"], "VALIDATION_ERROR")

    # ==========================================================================
    # 2. 내 상벌점 내역 조회 API (GET /api/points) 테스트
    # ==========================================================================
    def test_get_points_success_all_and_field_specs(self):
        """학생 본인의 전체 상벌점 내역 조회 및 필드 규격 변환 검증"""
        resp = self.client.get("/api/points")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        data = body["data"]

        # 학생 1의 상벌점 내역은 총 3건
        self.assertEqual(len(data), 3)

        # 최신순 정렬 확인 (merits_point_id 2 -> 1 -> 3)
        self.assertEqual(data[0]["id"], 2)
        self.assertEqual(data[1]["id"], 1)
        self.assertEqual(data[2]["id"], 3)

        # 필드 규격 검증
        demerit = data[0]
        self.assertEqual(demerit["pointId"], 2)
        self.assertEqual(demerit["type"], "벌점")
        self.assertEqual(demerit["points"], 1.0)
        self.assertEqual(demerit["score"], 1.0)  # Tech_spec 호환
        self.assertEqual(demerit["reason"], "지각 3회")
        self.assertEqual(demerit["date"], "2026-06-15")
        self.assertEqual(demerit["teacherName"], "김선생")
        self.assertTrue(demerit["reflected"])
        self.assertEqual(demerit["reflectedArea"], "직업기초역량")

        merit = data[1]
        self.assertEqual(merit["pointId"], 1)
        self.assertEqual(merit["type"], "상점")
        self.assertEqual(merit["points"], 2.0)
        self.assertEqual(merit["reason"], "교내 프로그래밍 대회 최우수상")

    def test_get_points_year_filtering(self):
        """2026 학년도(2026-03-01 ~ 2027-03-01) 필터링 시 2026년 데이터만 2건 조회"""
        resp = self.client.get("/api/points?year=2026")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 2)
        for item in data:
            self.assertTrue(item["date"].startswith("2026"))

    def test_get_points_other_student_forbidden(self):
        """학생이 타 학생의 studentId로 상벌점 조회 시 403 FORBIDDEN 차단"""
        resp = self.client.get("/api/points?studentId=2")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

    def test_get_student_points_path_route(self):
        """학생 하위 라우트(GET /api/students/{id}/points) 본인 성공 및 타인 차단"""
        resp = self.client.get("/api/students/1/points")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["data"]), 3)

        resp_other = self.client.get("/api/students/2/points")
        self.assertEqual(resp_other.status_code, 403)

    def test_teacher_get_points_delegation(self):
        """교사 권한: studentId 지정 시 해당 학생 조회 성공, 미지정 시 400 차단"""
        self.current_user = {
            "uuid": "teacher-uuid-101",
            "id": "teacher1",
            "name": "김선생",
            "role": 1,
        }
        resp = self.client.get("/api/points?studentId=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["reason"], "타 학생 상점")

        resp_missing = self.client.get("/api/points")
        self.assertEqual(resp_missing.status_code, 400)
        self.assertEqual(resp_missing.json()["error"]["code"], "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
