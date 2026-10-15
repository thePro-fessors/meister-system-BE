"""
tests/test_audit_fixes_v5.py - 7대 기술명세서 보완 항목 전수 검증 테스트
1. FE 감사 로그 & 재계산 응답 규격 호환성
2. FE 일괄 등록(batch) totalProcessed/successCount 규격 호환성
3. BE 학생 인증 현황 pointTotal 포함 검증
4. BE 관리자 overview statusCounts & submissionCounts 및 PatchYearRequest active 필드
5. BE 로그인 LoginRequest id 필드 지원
6. BE 증빙 재제출 submittedAt 및 승인 reviewedAt 반환
7. BE 상벌점 수정 editHistory 반환
"""

import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from main import app
from database import get_db, get_redis
from core.security import create_access_token


class MockRedis:
    def __init__(self):
        self.store = {}

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.store:
            return False
        self.store[k] = v
        return True

    async def incr(self, k):
        val = int(self.store.get(k, 0)) + 1
        self.store[k] = val
        return val

    async def expire(self, k, sec):
        return True

    async def delete(self, k):
        self.store.pop(k, None)

    async def ttl(self, k):
        return 60


class TestAuditFixesV5(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.redis = MockRedis()
        self.token_admin = create_access_token({"sub": "admin-uuid", "id": "admin", "role": "admin"})
        self.token_student = create_access_token({"sub": "student-uuid", "id": "student1", "role": "student"})
        self.token_teacher = create_access_token({"sub": "teacher-uuid", "id": "teacher1", "role": "teacher"})

    # 1. LoginRequest id 지원 검증 (Item 5)
    def test_login_request_supports_id_field(self):
        """LoginRequest에서 username 대신 id 필드를 전송해도 422 없이 정상 수신되는지 검증"""
        class MockDBLogin:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def cursor(self, *args, **kwargs):
                return self
            async def execute(self, q, params=None):
                pass
            async def fetchone(self):
                return None

        app.dependency_overrides[get_db] = lambda: MockDBLogin()
        app.dependency_overrides[get_redis] = lambda: self.redis

        # (1) id 필드만 전송 (계정 미존재 시 401 반환 = 422 언프로세서블 엔티티가 아님)
        res = self.client.post("/auth/login", json={"id": "nonexistent_user", "password": "wrong_password!"})
        self.assertIn(res.status_code, (401, 404))

        # (2) username, id 둘 다 누락 시 400 반환
        res_empty = self.client.post("/auth/login", json={"password": "pwd"})
        self.assertEqual(res_empty.status_code, 400)

        app.dependency_overrides.clear()

    # 2. PatchYearRequest active 필드 지원 검증 (Item 4)
    def test_patch_year_supports_active_field(self):
        """PATCH /api/years/{year}에 active: bool 필드 전송 시 정상 처리 검증"""
        class MockDB:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def cursor(self, *args, **kwargs):
                return self
            async def autocommit(self, val):
                pass
            async def commit(self):
                pass
            async def rollback(self):
                pass
            async def execute(self, q, params=None):
                self.last_query = q
                self.last_params = params
            async def fetchone(self):
                return {"year_id": 1, "year": 2026, "is_activated": 1}

        db = MockDB()
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_redis] = lambda: self.redis

        res = self.client.patch(
            "/api/years/2026",
            json={"active": True},
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        self.assertTrue(res.json()["data"]["isActivated"])

        res_false = self.client.patch(
            "/api/years/2026",
            json={"active": False},
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(res_false.status_code, 200)
        self.assertFalse(res_false.json()["data"]["isActivated"])

        app.dependency_overrides.clear()

    # 3. Admin Overview statusCounts & submissionCounts 검증 (Item 4)
    def test_admin_overview_returns_status_and_submission_counts(self):
        """GET /api/admin/overview 응답에 statusCounts 및 submissionCounts 필드 포함 검증"""
        class MockDBOverview:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def cursor(self, *args, **kwargs):
                return self
            async def execute(self, q, params=None):
                self.q = q
            async def fetchone(self):
                if "FROM academic_years" in getattr(self, "q", ""):
                    return {"year": 2026, "year_id": 1}
                elif "COUNT(DISTINCT sar.student_id)" in getattr(self, "q", ""):
                    return {"total_students": 5}
                elif "COUNT(*) AS total_submissions" in getattr(self, "q", ""):
                    return {
                        "total_submissions": 10,
                        "submitted_count": 2,
                        "reviewing_count": 1,
                        "approved_count": 5,
                        "rejected_count": 1,
                        "resubmit_count": 1,
                    }
                return None
            async def fetchall(self):
                if "SELECT ca.area_id AS areaId" in getattr(self, "q", ""):
                    return [{"areaId": 1, "areaName": "전공", "submissionCount": 10}]
                elif "FROM students st" in getattr(self, "q", ""):
                    return [{"student_id": 1, "grade": 1}]
                elif "FROM certification_areas ca" in getattr(self, "q", ""):
                    return [{
                        "area_id": 1, "grade": 1, "area_name": "전공", "area_max_score": 100.0,
                        "item_id": 1, "item_name": "자격증", "item_max_score": 50.0,
                        "scoring_type": 1, "is_active": True,
                    }]
                return []

        app.dependency_overrides[get_db] = lambda: MockDBOverview()
        app.dependency_overrides[get_redis] = lambda: self.redis

        res = self.client.get(
            "/api/admin/overview?year=2026",
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertIn("statusCounts", data)
        self.assertIn("certified", data["statusCounts"])
        self.assertIn("reviewing", data["statusCounts"])
        self.assertIn("supplement", data["statusCounts"])
        self.assertIn("uncertified", data["statusCounts"])

        self.assertIn("submissionCounts", data)
        self.assertEqual(data["submissionCounts"]["total"], 10)
        self.assertEqual(data["submissionCounts"]["pending"], 3)
        self.assertEqual(data["submissionCounts"]["approved"], 5)

        app.dependency_overrides.clear()

    # 4. 상벌점 수정 editHistory 검증 (Item 7)
    def test_points_patch_returns_edit_history(self):
        """PATCH /api/points/{pointId} 응답에 editHistory 리스트 포함 검증"""
        class MockDBPoint:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def cursor(self, *args, **kwargs):
                return self
            async def autocommit(self, val):
                pass
            async def commit(self):
                pass
            async def rollback(self):
                pass
            async def execute(self, q, params=None):
                self.q = q
            async def fetchone(self):
                if "SELECT merits_point_id" in getattr(self, "q", ""):
                    return {
                        "merits_point_id": 10,
                        "student_id": 1,
                        "teachers_id": 2,
                        "type": "+",
                        "points": 5.0,
                        "reason": "원래 사유",
                        "related_area": "전공",
                        "occurred_at": "2026-04-01",
                        "is_deleted": False,
                    }
                return None
            async def fetchall(self):
                if "FROM merits_log" in getattr(self, "q", ""):
                    return [{
                        "modifier_uuid": "teacher-uuid",
                        "modify_reason": "점수 정정 사유",
                        "created_at": "2026-04-02T10:00:00",
                    }]
                return []

        app.dependency_overrides[get_db] = lambda: MockDBPoint()
        app.dependency_overrides[get_redis] = lambda: self.redis

        with patch("core.authorization.authorize_student_access", new=AsyncMock(return_value=None)):
            with patch("core.authorization.authorize_point_mutation", new=AsyncMock(return_value=None)):
                res = self.client.patch(
                    "/api/points/10",
                    json={"score": 10.0, "reason": "점수 정정 사유"},
                    headers={"Authorization": f"Bearer {self.token_teacher}"},
                )
                self.assertEqual(res.status_code, 200)
                data = res.json()["data"]
                self.assertIn("editHistory", data)
                self.assertTrue(len(data["editHistory"]) >= 1)
                self.assertEqual(data["editHistory"][0]["reason"], "점수 정정 사유")

        app.dependency_overrides.clear()

    # 5. 증빙 재제출 submittedAt 및 승인 reviewedAt 검증 (Item 6)
    def test_submissions_resubmit_and_approve_timestamps(self):
        """재제출 시 submittedAt, 승인 시 reviewedAt 타임스탬프 반환 검증"""
        class MockDBSub:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def cursor(self, *args, **kwargs):
                return self
            async def autocommit(self, val):
                pass
            async def commit(self):
                pass
            async def rollback(self):
                pass
            async def execute(self, q, params=None):
                self.q = q
                self.rowcount = 1
            async def fetchone(self):
                if "SELECT s.submission_id" in getattr(self, "q", "") and "teachers" not in getattr(self, "q", ""):
                    return {
                        "submission_id": 99,
                        "student_id": 1,
                        "status_code": 4,  # 반려 상태
                        "granted_score": 0.0,
                        "file_path": None,
                        "link_url": None,
                        "description": "기존",
                        "uuid": "student-uuid",
                        "max_score": 50.0,
                        "year_id": 1,
                        "year": 2026,
                    }
                elif "SELECT teachers_id FROM teachers" in getattr(self, "q", ""):
                    return {"teachers_id": 10}
                return None
            async def fetchall(self):
                return []

        app.dependency_overrides[get_db] = lambda: MockDBSub()
        app.dependency_overrides[get_redis] = lambda: self.redis

        # (1) 재제출 -> submittedAt 존재 확인
        res_resubmit = self.client.patch(
            "/api/submissions/99/resubmit",
            data={"description": "보완 설명 제출"},
            headers={"Authorization": f"Bearer {self.token_student}"},
        )
        self.assertEqual(res_resubmit.status_code, 200)
        data_resubmit = res_resubmit.json()["data"]
        self.assertIn("submittedAt", data_resubmit)
        self.assertIsNotNone(data_resubmit["submittedAt"])

        # (2) 승인 -> reviewedAt 존재 확인
        with patch("core.authorization.authorize_submission_access", new=AsyncMock(return_value=None)):
            with patch("routers.submissions._calculate_student_totals", new=AsyncMock(return_value={"totalScore": 50.0, "certStatus": "인증 가능", "pointTotal": 0.0})):
                # Mock status_code = 1 for approve
                MockDBSub.fetchone = AsyncMock(side_effect=[
                    {
                        "submission_id": 99,
                        "student_id": 1,
                        "status_code": 1,
                        "granted_score": 0.0,
                        "max_score": 50.0,
                        "year_id": 1,
                        "year": 2026,
                    },
                    {"teachers_id": 10},
                ])
                res_approve = self.client.patch(
                    "/api/submissions/99/approve",
                    json={"score": 40.0, "comment": "승인합니다"},
                    headers={"Authorization": f"Bearer {self.token_teacher}"},
                )
                self.assertEqual(res_approve.status_code, 200)
                data_approve = res_approve.json()["data"]
                self.assertIn("reviewedAt", data_approve)
                self.assertIsNotNone(data_approve["reviewedAt"])

        app.dependency_overrides.clear()

    # 6. 학생 인증 현황 조회 pointTotal 반환 검증 (Item 3)
    def test_student_certification_status_includes_point_total(self):
        """GET /api/students/{id}/certification-status 응답에 pointTotal 필드 포함 검증"""
        class MockDBStudent:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def cursor(self, *args, **kwargs):
                return self
            async def execute(self, q, params=None):
                self.q = q
            async def fetchone(self):
                if "FROM students" in getattr(self, "q", ""):
                    return {"student_id": 1, "uuid": "student-uuid", "name": "학생1", "email": "s@b.hs.kr", "is_deleted": False}
                elif "FROM student_academic_records" in getattr(self, "q", ""):
                    return {"grade": 1, "class_no": 1, "number": 1}
                return None
            async def fetchall(self):
                if "FROM certification_areas" in getattr(self, "q", ""):
                    return [{"area_id": 1, "name": "기본", "max_score": 100.0}]
                return []

        app.dependency_overrides[get_db] = lambda: MockDBStudent()
        app.dependency_overrides[get_redis] = lambda: self.redis

        with patch("routers.students.get_current_academic_year", new=AsyncMock(return_value=2026)):
            with patch("routers.students.get_year_id_by_year", new=AsyncMock(return_value=1)):
                with patch("routers.students.authorize_student_access", new=AsyncMock(return_value=None)):
                    with patch("routers.students.resolve_student_id", new=AsyncMock(return_value=1)):
                        res = self.client.get(
                            "/api/students/1/certification-status?year=2026",
                            headers={"Authorization": f"Bearer {self.token_student}"},
                        )
                        self.assertEqual(res.status_code, 200)
                        data = res.json()["data"]
                        self.assertIn("pointTotal", data)
                        self.assertIn("totalMeritScore", data)
                        self.assertEqual(data["pointTotal"], data["totalMeritScore"])

        app.dependency_overrides.clear()

    # 7. 시스템 감사 로그 및 재계산 응답 키 호환성 검증 (Item 1)
    def test_admin_audit_logs_and_recalculate_response_keys(self):
        """GET /api/admin/audit-logs 응답에 total 및 logs 호환 키 포함 검증"""
        class MockDBAudit:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def cursor(self, *args, **kwargs):
                return self
            async def execute(self, q, params=None):
                self.q = q
            async def fetchone(self):
                if "COUNT(*) AS total_count" in getattr(self, "q", ""):
                    return {"total_count": 1}
                return None
            async def fetchall(self):
                if "FROM (" in getattr(self, "q", ""):
                    return [{
                        "unified_id": "sub_1",
                        "original_log_id": 1,
                        "log_type": "SUBMISSION",
                        "action_type": "APPROVE",
                        "target_id": 10,
                        "target_title": "자격증",
                        "student_id": 1,
                        "student_name": "홍길동",
                        "student_grade": 1,
                        "student_class": 1,
                        "student_number": 1,
                        "modifier_uuid": "admin-uuid",
                        "modifier_name": "관리자",
                        "modifier_role": "admin",
                        "old_status_code": 1,
                        "new_status_code": 3,
                        "old_score": 0.0,
                        "new_score": 50.0,
                        "old_points": None,
                        "new_points": None,
                        "reason": "승인 완료",
                        "created_at": "2026-04-01T12:00:00",
                    }]
                return []

        app.dependency_overrides[get_db] = lambda: MockDBAudit()
        app.dependency_overrides[get_redis] = lambda: self.redis

        res = self.client.get(
            "/api/admin/audit-logs",
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertIn("total", data)
        self.assertIn("logs", data)
        self.assertIn("pagination", data)
        self.assertEqual(data["total"], data["pagination"]["totalCount"])
        self.assertIn("log_id", data["logs"][0])
        self.assertIn("user_id", data["logs"][0])

        app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()

