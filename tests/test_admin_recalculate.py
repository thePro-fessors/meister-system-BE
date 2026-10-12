"""
tests/test_admin_recalculate.py - 관리자 수동 데이터 정합성 검증 및 재계산 배치 단위/통합 테스트 (TODO.md 4.10)
"""

import os
import unittest
from typing import Any, Dict, List
from fastapi.testclient import TestClient

from core.security import create_access_token, get_current_user
from database import get_db, get_redis
from main import app


class RecalculateMockCursor:
    def __init__(self, conn):
        self.conn = conn
        self._current_result = []
        self._fetch_index = 0
        self.rowcount = 1
        self.lastrowid = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql: str, params: tuple = ()):
        sql_clean = " ".join(sql.strip().split())
        self._fetch_index = 0

        # 1. academic_years 조회
        if "FROM academic_years" in sql_clean:
            if params and params[0] == 1999:
                self._current_result = []
            else:
                self._current_result = [{"year_id": 1, "year": 2026, "is_activated": 1}]
            return

        # 2. certification_areas & evaluation_items 조회
        if "FROM certification_areas ca" in sql_clean:
            self._current_result = [
                {
                    "area_id": 1,
                    "grade": 1,
                    "area_name": "전문기술역량",
                    "area_max_score": 20.0,
                    "item_id": 101,
                    "item_name": "정보처리기능사 취득",
                    "item_max_score": 20.0,
                    "scoring_type": 1,
                    "is_active": 1,
                },
                {
                    "area_id": 2,
                    "grade": 1,
                    "area_name": "직업기초능력",
                    "area_max_score": 20.0,
                    "item_id": 102,
                    "item_name": "의사소통 프로젝트",
                    "item_max_score": 20.0,
                    "scoring_type": 1,
                    "is_active": 1,
                },
            ]
            return

        # 3. student_academic_records & students 조회
        if "FROM student_academic_records sar" in sql_clean:
            students = [
                {"student_id": 1, "name": "김학생", "email": "s1@bssm.hs.kr", "grade": 1, "class_no": 1, "number": 1},
                {"student_id": 2, "name": "이학생", "email": "s2@bssm.hs.kr", "grade": 1, "class_no": 1, "number": 2},
                {"student_id": 3, "name": "박학생", "email": "s3@bssm.hs.kr", "grade": 2, "class_no": 1, "number": 1},
            ]
            # 학년 필터가 있는 경우
            if "sar.grade = %s" in sql_clean:
                target_g = None
                for p in params:
                    if isinstance(p, int) and p in (1, 2, 3):
                        target_g = p
                        break
                if target_g:
                    students = [s for s in students if s["grade"] == target_g]

            # 학생 ID 필터가 있는 경우
            if "st.student_id = %s" in sql_clean:
                target_sid = params[-1]
                students = [s for s in students if s["student_id"] == target_sid]

            self._current_result = students
            return

        # 4. submissions 조회
        if "FROM submissions s" in sql_clean:
            self._current_result = list(self.conn.submissions)
            return

        # 5. merits 조회
        if "FROM merits" in sql_clean:
            self._current_result = [
                {"merits_point_id": 1, "student_id": 1, "type": "+", "points": 2.0, "related_area": "인성/직업의식"},
            ]
            return

        # 6. submissions UPDATE (자동 보정)
        if "UPDATE submissions SET granted_score" in sql_clean:
            new_score = params[0]
            sub_id = params[1]
            self.conn.updated_submissions.append((sub_id, new_score))
            for s in self.conn.submissions:
                if s["submission_id"] == sub_id:
                    s["granted_score"] = new_score
            self.rowcount = 1
            return

        # 7. submissions_logs INSERT
        if "INSERT INTO submissions_logs" in sql_clean:
            self.conn.logs_inserted.append(params)
            self.rowcount = 1
            return

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


class RecalculateMockConnection:
    def __init__(self):
        self.submissions = [
            {
                "submission_id": 1,
                "student_id": 1,
                "item_id": 101,
                "status_code": 3,
                "granted_score": 20.0,
                "area_id": 1,
                "scoring_type": 1,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
            },
            {
                "submission_id": 2,
                "student_id": 2,
                "item_id": 101,
                "status_code": 3,
                "granted_score": 25.0,  # 이상치: 최대 배점(20점) 초과!
                "area_id": 1,
                "scoring_type": 1,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
            },
        ]
        self.updated_submissions = []
        self.logs_inserted = []
        self.committed = False

    def cursor(self, **kwargs):
        return RecalculateMockCursor(self)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


class TestAdminRecalculate(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_conn = RecalculateMockConnection()
        self.mock_redis = None

        class FakeRedis:
            async def get(self, k):
                return None

        app.dependency_overrides[get_db] = lambda: self.mock_conn
        app.dependency_overrides[get_redis] = lambda: FakeRedis()

        self._orig_gemini_key = os.environ.get("GEMINI_API_KEY")
        self._orig_ai_key = os.environ.get("AI_API_KEY")
        if "GEMINI_API_KEY" in os.environ:
            del os.environ["GEMINI_API_KEY"]
        if "AI_API_KEY" in os.environ:
            del os.environ["AI_API_KEY"]

        self.admin_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000099",
            "id": "admin",
            "role": "admin",
            "name": "최고관리자",
        })
        self.student_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000001",
            "id": "student1",
            "role": "student",
            "name": "김학생",
        })
        self.teacher_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000002",
            "id": "teacher1",
            "role": "teacher",
            "name": "김선생",
        })

    def tearDown(self):
        app.dependency_overrides.clear()
        if self._orig_gemini_key is not None:
            os.environ["GEMINI_API_KEY"] = self._orig_gemini_key
        elif "GEMINI_API_KEY" in os.environ:
            del os.environ["GEMINI_API_KEY"]

        if self._orig_ai_key is not None:
            os.environ["AI_API_KEY"] = self._orig_ai_key
        elif "AI_API_KEY" in os.environ:
            del os.environ["AI_API_KEY"]

    def test_recalculate_forbidden_for_non_admin(self):
        """학생 및 교사 권한으로 접근 시 403 Forbidden 차단 검증"""
        resp_st = self.client.post(
            "/api/admin/recalculate",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={},
        )
        self.assertEqual(resp_st.status_code, 403)
        self.assertEqual(resp_st.json()["error"]["code"], "FORBIDDEN")

        resp_tc = self.client.post(
            "/api/admin/recalculate",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={},
        )
        self.assertEqual(resp_tc.status_code, 403)

    def test_recalculate_year_not_found(self):
        """존재하지 않는 학년도 요청 시 404 에러 반환 검증"""
        resp = self.client.post(
            "/api/admin/recalculate",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 1999},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "YEAR_NOT_FOUND")

    def test_recalculate_dry_run_anomaly_detection(self):
        """DryRun 모드 실행 시 이상치(배점 초과) 탐지 및 DB 수정 방지 검증"""
        resp = self.client.post(
            "/api/admin/recalculate",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"dryRun": True},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(data["isDryRun"])
        self.assertEqual(data["targetYear"], 2026)
        self.assertEqual(data["totalProcessedStudents"], 3)
        self.assertEqual(data["anomaliesCount"], 1)
        self.assertEqual(data["anomaliesFixedCount"], 0)
        self.assertEqual(data["anomaliesFound"][0]["issueType"], "EXCEEDED_MAX_SCORE")
        self.assertEqual(data["anomaliesFound"][0]["grantedScore"], 25.0)
        self.assertEqual(data["anomaliesFound"][0]["itemMaxScore"], 20.0)
        # DryRun이므로 DB UPDATE가 실행되지 않아야 함
        self.assertEqual(len(self.mock_conn.updated_submissions), 0)

    def test_recalculate_fix_anomalies_success(self):
        """fixAnomalies=True 및 dryRun=False 시 이상 점수 자동 클램핑 및 감사 로그 기록 검증"""
        resp = self.client.post(
            "/api/admin/recalculate",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"dryRun": False, "fixAnomalies": True},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertFalse(data["isDryRun"])
        self.assertEqual(data["anomaliesCount"], 1)
        self.assertEqual(data["anomaliesFixedCount"], 1)
        # 실제로 20점으로 클램핑 UPDATE 되었는지 검증
        self.assertEqual(len(self.mock_conn.updated_submissions), 1)
        sub_id, fixed_score = self.mock_conn.updated_submissions[0]
        self.assertEqual(sub_id, 2)
        self.assertEqual(fixed_score, 20.0)
        # 감사 로그에 INSERT 되었는지 검증
        self.assertEqual(len(self.mock_conn.logs_inserted), 1)
        self.assertTrue(self.mock_conn.committed)

    def test_recalculate_grade_filter(self):
        """특정 학년(grade=1) 필터링 적용 시 대상 학생 격리 검증"""
        resp = self.client.post(
            "/api/admin/recalculate",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"grade": 1},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        # 전체 3명 중 1학년 2명만 집계되어야 함
        self.assertEqual(data["totalProcessedStudents"], 2)
        self.assertEqual(data["filter"]["grade"], 1)


if __name__ == "__main__":
    unittest.main()
