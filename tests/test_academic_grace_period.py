"""
tests/test_academic_grace_period.py - 학년도 유예 기간(Multi-Year Grace Period) 검증 스위트

검증 항목:
1. 관리자 학년도 목록 조회 시 status 필드 ('ACTIVE', 'GRACE_PERIOD', 'CLOSED') 반환 검증
2. 관리자 PATCH /api/years/{year} 로 status = 'GRACE_PERIOD' 토글 검증
3. GRACE_PERIOD 상태 학년도에 학생 신규 제출 시 400 ACADEMIC_YEAR_GRACE_PERIOD 차단 검증
4. GRACE_PERIOD 상태 학년도의 기존 반려 건에 대해 학생 재제출(resubmit) 허용 검증
5. CLOSED 상태 학년도의 기존 건에 대해 학생 재제출 시 400 ACADEMIC_YEAR_CLOSED 차단 검증
"""

import unittest
from typing import Any, Dict, List
from fastapi.testclient import TestClient

from core.academic import clear_year_cache
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
        self._results = []
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query: str, params: Any = None):
        q = " ".join(query.split())
        params = list(params) if params else []

        if "SELECT year_id AS yearId, year, is_activated AS isActivated, status FROM academic_years" in q:
            self._results = [
                {"yearId": y["year_id"], "year": y["year"], "isActivated": y["is_activated"], "status": y["status"]}
                for y in self.conn.years
            ]
        elif "SELECT year_id FROM academic_years WHERE year = %s" in q:
            target_year = params[0]
            matched = [y for y in self.conn.years if y["year"] == target_year]
            self._results = [{"year_id": matched[0]["year_id"]}] if matched else []
        elif "UPDATE academic_years SET is_activated = FALSE WHERE year != %s" in q:
            keep_y = params[0]
            for y in self.conn.years:
                if y["year"] != keep_y:
                    y["is_activated"] = False
        elif "UPDATE academic_years SET is_activated = %s WHERE year = %s" in q:
            is_act, target_year = params[0], params[1]
            for y in self.conn.years:
                if y["year"] == target_year:
                    y["is_activated"] = bool(is_act)
        elif "UPDATE academic_years SET status = %s WHERE year = %s" in q:
            st, target_year = params[0], params[1]
            for y in self.conn.years:
                if y["year"] == target_year:
                    y["status"] = str(st)
        elif "SELECT student_id, uuid, name, email, is_deleted FROM students WHERE uuid = %s" in q:
            self._results = [{"student_id": 101, "uuid": "mock-student-uuid", "name": "학생1", "email": "s101@test.com", "is_deleted": False}]
        elif "SELECT s.submission_id, s.student_id, s.status_code" in q and "FROM submissions s" in q:
            # 재제출 검증용 조회
            sub_id = params[0]
            matched = [s for s in self.conn.submissions if s["submission_id"] == sub_id]
            if matched:
                item = matched[0]
                y_obj = next((y for y in self.conn.years if y["year_id"] == item["year_id"]), {"status": "ACTIVE", "is_activated": True})
                self._results = [{
                    "submission_id": item["submission_id"],
                    "student_id": item["student_id"],
                    "status_code": item["status_code"],
                    "granted_score": item.get("granted_score", 0.0),
                    "file_path": item.get("file_path"),
                    "link_url": item.get("link_url"),
                    "description": item.get("description", ""),
                    "uuid": "mock-student-uuid",
                    "year_status": y_obj["status"],
                    "year_is_activated": y_obj["is_activated"],
                }]
            else:
                self._results = []
        elif "SELECT ei.item_id, ei.area_id, ei.name AS item_name" in q and "FROM evaluation_items ei" in q:
            # 신규 제출용 조회
            item_id = params[0]
            matched_item = next((it for it in self.conn.items if it["item_id"] == item_id), None)
            if matched_item:
                y_obj = next((y for y in self.conn.years if y["year_id"] == matched_item["year_id"]), {"status": "ACTIVE", "is_activated": True})
                self._results = [{
                    "item_id": matched_item["item_id"],
                    "area_id": matched_item["area_id"],
                    "item_name": matched_item["name"],
                    "target_grade": 1,
                    "item_max_score": 10.0,
                    "scoring_type": 1,
                    "requires_evidence": True,
                    "is_active": True,
                    "year_id": matched_item["year_id"],
                    "area_grade": 1,
                    "area_name": "전공",
                    "academic_year": y_obj["year"],
                    "year_is_activated": y_obj["is_activated"],
                    "year_status": y_obj["status"],
                }]
            else:
                self._results = []
        elif "UPDATE submissions SET status_code = 1" in q:
            sub_id = params[-1]
            for s in self.conn.submissions:
                if s["submission_id"] == sub_id:
                    s["status_code"] = 1
        elif "INSERT INTO submissions_logs" in q:
            pass
        else:
            self._results = []

    async def fetchone(self):
        return self._results[0] if self._results else None

    async def fetchall(self):
        return list(self._results)


class MockConnection:
    def __init__(self):
        self.years = [
            {"year_id": 1, "year": 2025, "is_activated": False, "status": "GRACE_PERIOD"},
            {"year_id": 2, "year": 2026, "is_activated": True, "status": "ACTIVE"},
            {"year_id": 3, "year": 2024, "is_activated": False, "status": "CLOSED"},
        ]
        self.items = [
            {"item_id": 10, "area_id": 1, "year_id": 1, "name": "2025년도 활동"},
            {"item_id": 20, "area_id": 2, "year_id": 2, "name": "2026년도 활동"},
            {"item_id": 30, "area_id": 3, "year_id": 3, "name": "2024년도 활동"},
        ]
        self.submissions = [
            {"submission_id": 1001, "student_id": 101, "item_id": 10, "year_id": 1, "status_code": 4, "description": "2025 반려건"},
            {"submission_id": 1002, "student_id": 101, "item_id": 30, "year_id": 3, "status_code": 4, "description": "2024 마감건"},
        ]

    def cursor(self, cursor=None):
        return MockCursor(self)

    async def autocommit(self, val):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class TestAcademicGracePeriod(unittest.TestCase):
    def setUp(self):
        clear_year_cache()
        self.conn = MockConnection()
        self.redis = MockRedis()

        self.current_user = {"uuid": "mock-admin-uuid", "role": "admin"}

        async def override_get_db():
            yield self.conn

        async def override_get_redis():
            yield self.redis

        async def override_get_current_user():
            return self.current_user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_redis] = override_get_redis
        app.dependency_overrides[get_current_user] = override_get_current_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        clear_year_cache()

    def test_01_get_all_years_returns_status(self):
        """1. 전체 학년도 목록 조회 시 status 필드가 정상 반환되는지 검증"""
        res = self.client.get("/api/years")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        y2025 = next(y for y in data if y["year"] == 2025)
        self.assertEqual(y2025["status"], "GRACE_PERIOD")
        y2026 = next(y for y in data if y["year"] == 2026)
        self.assertEqual(y2026["status"], "ACTIVE")

    def test_02_toggle_year_status(self):
        """2. PATCH /api/years/{year} 로 status 변경 검증"""
        res = self.client.patch("/api/years/2025", json={"status": "CLOSED"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["status"], "CLOSED")
        y2025 = next(y for y in self.conn.years if y["year"] == 2025)
        self.assertEqual(y2025["status"], "CLOSED")

    def test_03_submit_evidence_blocked_in_grace_period(self):
        """3. GRACE_PERIOD 상태 학년도에 학생 신규 제출 시 400 ACADEMIC_YEAR_GRACE_PERIOD 차단 검증"""
        self.current_user = {"uuid": "mock-student-uuid", "role": "student"}
        res = self.client.post(
            "/api/submissions",
            data={
                "itemId": 10,  # 2025년도 GRACE_PERIOD 항목
                "detail": "정보처리기능사 실기",
                "activityDate": "2025-11-20",
                "description": "2025 활동 제출 시도",
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "ACADEMIC_YEAR_GRACE_PERIOD")

    def test_04_resubmit_allowed_in_grace_period(self):
        """4. GRACE_PERIOD 상태 학년도의 기존 반려 건에 대해 재제출 허용 검증"""
        self.current_user = {"uuid": "mock-student-uuid", "role": "student"}
        res = self.client.patch(
            "/api/submissions/1001/resubmit",
            data={
                "description": "보완된 증빙 설명입니다.",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])

    def test_05_resubmit_blocked_in_closed_year(self):
        """5. CLOSED 상태 학년도의 기존 건에 대해 학생 재제출 시 400 ACADEMIC_YEAR_CLOSED 차단 검증"""
        self.current_user = {"uuid": "mock-student-uuid", "role": "student"}
        res = self.client.patch(
            "/api/submissions/1002/resubmit",
            data={
                "description": "마감된 학년도 건 재제출 시도",
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "ACADEMIC_YEAR_CLOSED")
