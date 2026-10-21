"""
tests/test_submission_appeal.py - 반려 건 공식 이의신청(Appeal) 및 2차 재심사 검증 스위트
"""

import unittest
from fastapi.testclient import TestClient

from core.security import get_current_user
from database import get_db, get_redis
from main import app


class MockCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1
        self._results = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query: str, params=None):
        q = " ".join(query.split())
        params = list(params) if params else []

        if "SELECT teachers_id, position, department FROM teachers" in q:
            u = params[0]
            matched = [t for t in self.conn.teachers if t["uuid"] == u]
            self._results = matched if matched else []
        elif "SELECT s.submission_id, s.student_id, s.status_code, st.uuid FROM submissions s" in q:
            sub_id = params[0]
            matched = [s for s in self.conn.submissions if s["submission_id"] == sub_id]
            if matched:
                item = matched[0]
                self._results = [{
                    "submission_id": item["submission_id"],
                    "student_id": item["student_id"],
                    "status_code": item["status_code"],
                    "uuid": item["student_uuid"],
                }]
            else:
                self._results = []
        elif "SELECT s.submission_id, s.student_id, s.status_code, ei.max_score" in q:
            sub_id = params[0]
            matched = [s for s in self.conn.submissions if s["submission_id"] == sub_id]
            if matched:
                item = matched[0]
                self._results = [{
                    "submission_id": item["submission_id"],
                    "student_id": item["student_id"],
                    "status_code": item["status_code"],
                    "max_score": 10.0,
                    "year_id": 1,
                    "year": 2026,
                }]
            else:
                self._results = []
        elif "UPDATE submissions" in q:
            sub_id = params[-1]
            for s in self.conn.submissions:
                if s["submission_id"] == sub_id:
                    if "status_code = 6" in q:
                        s["status_code"] = 6
                    elif len(params) > 1 and isinstance(params[0], int):
                        s["status_code"] = params[0]
        elif "INSERT INTO submissions_logs" in q:
            pass
        elif "SELECT ca.area_id, ca.name AS area_name, ca.max_score" in q:
            self._results = []
        elif "SELECT s.item_id, s.status_code, s.granted_score" in q:
            self._results = []
        else:
            self._results = []

    async def fetchone(self):
        return self._results[0] if self._results else None

    async def fetchall(self):
        return list(self._results)


class MockConnection:
    def __init__(self):
        self.submissions = [
            {"submission_id": 2001, "student_id": 101, "student_uuid": "student-uuid-1", "status_code": 4},
            {"submission_id": 2002, "student_id": 102, "student_uuid": "student-uuid-2", "status_code": 1},
            {"submission_id": 2003, "student_id": 101, "student_uuid": "student-uuid-1", "status_code": 6},
        ]
        self.teachers = [
            {"teachers_id": 10, "uuid": "head-teacher-uuid", "position": "마이스터부장", "department": "마이스터부"},
            {"teachers_id": 20, "uuid": "normal-teacher-uuid", "position": "교과전담", "department": "인문소양부"},
        ]

    def cursor(self, cursor=None):
        return MockCursor(self)

    async def autocommit(self, val):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class TestSubmissionAppeal(unittest.TestCase):
    def setUp(self):
        self.conn = MockConnection()
        self.current_user = {"uuid": "student-uuid-1", "role": "student"}

        async def override_get_db():
            yield self.conn

        async def override_get_current_user():
            return self.current_user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = override_get_current_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_01_student_can_appeal_rejected_submission(self):
        """1. 학생이 본인 반려 건(상태 4)에 대해 이의신청 접수 시 상태 6 전이 검증"""
        res = self.client.post(
            "/api/submissions/2001/appeal",
            json={"appeal_reason": "공인 자격증 원본 발급 번호가 재확인되어 재심사를 요청합니다."},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["statusCode"], 6)
        self.assertEqual(data["status"], "이의신청")

        sub2001 = next(s for s in self.conn.submissions if s["submission_id"] == 2001)
        self.assertEqual(sub2001["status_code"], 6)

    def test_02_student_cannot_appeal_others_submission(self):
        """2. 타 학생 제출 건에 대해 이의신청 시도 시 403 Forbidden 검증"""
        self.current_user = {"uuid": "other-student-uuid", "role": "student"}
        res = self.client.post(
            "/api/submissions/2001/appeal",
            json={"appeal_reason": "타 학생 건 이의신청 시도"},
        )
        self.assertEqual(res.status_code, 403)

    def test_03_cannot_appeal_non_rejected_submission(self):
        """3. 반려(4) 상태가 아닌 건(예: 1번 제출완료)에 대해 이의신청 시 400 차단 검증"""
        self.current_user = {"uuid": "student-uuid-2", "role": "student"}
        res = self.client.post(
            "/api/submissions/2002/appeal",
            json={"appeal_reason": "제출완료 건 이의신청 시도"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "INVALID_STATUS")

    def test_04_normal_teacher_cannot_review_appeal(self):
        """4. 일반 교과교사가 이의신청 2차 재심사 시도 시 403 Forbidden 검증"""
        self.current_user = {"uuid": "normal-teacher-uuid", "role": "teacher"}
        res = self.client.patch(
            "/api/submissions/2003/review-appeal",
            json={"decision": "APPROVE", "granted_score": 10.0, "comment": "승인 시도"},
        )
        self.assertEqual(res.status_code, 403)

    def test_05_head_teacher_can_approve_appeal(self):
        """5. 부장교사가 이의신청(6) 건에 대해 승인(APPROVE) 판정 시 상태 3 전이 검증"""
        self.current_user = {"uuid": "head-teacher-uuid", "role": "teacher"}
        res = self.client.patch(
            "/api/submissions/2003/review-appeal",
            json={"decision": "APPROVE", "granted_score": 10.0, "comment": "이의신청 인정 완료"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["statusCode"], 3)
        self.assertEqual(data["decision"], "APPROVE")

        sub2003 = next(s for s in self.conn.submissions if s["submission_id"] == 2003)
        self.assertEqual(sub2003["status_code"], 3)

    def test_06_head_teacher_can_reject_appeal(self):
        """6. 부장교사가 이의신청(6) 건에 대해 기각(REJECT) 판정 시 상태 4(반려 확정) 전이 검증"""
        self.conn.submissions.append({"submission_id": 2004, "student_id": 101, "student_uuid": "student-uuid-1", "status_code": 6})
        self.current_user = {"uuid": "head-teacher-uuid", "role": "teacher"}
        res = self.client.patch(
            "/api/submissions/2004/review-appeal",
            json={"decision": "REJECT", "comment": "배점 기준 요건 미충족으로 기각"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["statusCode"], 4)
        self.assertEqual(data["decision"], "REJECT")

        sub2004 = next(s for s in self.conn.submissions if s["submission_id"] == 2004)
        self.assertEqual(sub2004["status_code"], 4)
