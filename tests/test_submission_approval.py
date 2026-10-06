"""
tests/test_submission_approval.py - 3.4 증빙자료 승인 및 점수 부여 API 전용 단위/통합 테스트
명세: Tech_spec.md 3.4 & TODO.md 3.4 (PATCH /api/submissions/{submissionId}/approve)

검증 목록:
1. RBAC & 스코프 인가:
   - 학생 계정 승인 시도 시 403 FORBIDDEN
   - 타 학급 담임교사 승인 시도 시 403 FORBIDDEN
   - 담임교사(본인 학급), 교과교사(전교생), 관리자(admin) 승인 허용
2. 유효성 검증:
   - 음수 점수 입력 시 400 VALIDATION_ERROR
   - maxScore 초과 점수 입력 시 400 VALIDATION_ERROR
   - 존재하지 않는 submission_id -> 404 ITEM_NOT_FOUND
   - 이미 승인/반려/삭제된 상태(status_code not in (1, 2)) -> 400 INVALID_STATUS
3. 비즈니스 로직 및 원자적 처리:
   - status_code=3 (인정완료) 전이
   - reviewer_id, reviewed_at, teacher_comment 저장
   - submissions_logs에 APPROVE 감사 이력 원자적 기록
   - studentTotals 갱신 결과 반환 (totalScore, certStatus, pointTotal 등)
4. 보안 방어:
   - teacher_comment HTML XSS 문자열 이스케이프 검증
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


class ApprovalMockCursor:
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

        # 5. certification_areas 조회
        elif "FROM certification_areas" in q:
            if "WHERE year_id = %s AND grade = %s" in q:
                y_id, g_no = params[0], params[1]
                matched = [
                    a for a in self.conn.db_areas
                    if a["year_id"] == y_id and a.get("grade") == g_no
                ]
                self._current_result = matched
            elif "WHERE year_id = %s" in q:
                y_id = params[0]
                matched = [a for a in self.conn.db_areas if a["year_id"] == y_id]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_areas)

        # 6. evaluation_items 조회
        elif "FROM evaluation_items" in q:
            if "WHERE item_id = %s" in q or "WHERE ei.item_id = %s" in q:
                it_id = params[0]
                matched = [it for it in self.conn.db_items if it["item_id"] == it_id]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_items)

        # 7. submissions 조회
        elif "FROM submissions" in q:
            if "WHERE s.submission_id = %s" in q or "WHERE submission_id = %s" in q:
                sub_id = params[0]
                matched = [s for s in self.conn.db_submissions if s["submission_id"] == sub_id and not s.get("is_deleted")]
                self._current_result = matched
            elif "WHERE s.student_id = %s" in q or "WHERE student_id = %s" in q:
                st_id = params[1] if len(params) > 1 and "year_id" in q else params[0]
                matched = [s for s in self.conn.db_submissions if s["student_id"] == st_id and not s.get("is_deleted")]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_submissions)

        # 8. merits 조회
        elif "FROM merits" in q:
            st_id = params[0]
            matched = [m for m in self.conn.db_merits if m["student_id"] == st_id and not m.get("is_deleted")]
            self._current_result = matched

        # 9. UPDATE submissions
        elif "UPDATE submissions" in q:
            score, reviewer_id, comment, sub_id = params[0], params[1], params[2], params[3]
            updated = False
            for s in self.conn.db_submissions:
                if s["submission_id"] == sub_id and s["status_code"] in (1, 2):
                    s["status_code"] = 3
                    s["granted_score"] = score
                    s["reviewer_id"] = reviewer_id
                    s["teacher_comment"] = comment
                    s["reviewed_at"] = datetime.now()
                    updated = True
                    break
            self.rowcount = 1 if updated else 0

        # 10. INSERT INTO submissions_logs
        elif "INSERT INTO submissions_logs" in q:
            # (submission_id, user_uuid, row["status_code"], row.get("granted_score"), score, comment)
            # VALUES (%s, %s, 'APPROVE', %s, 3, %s, %s, %s, NOW())
            self.conn.sub_logs.append({
                "submission_id": params[0],
                "modifier_uuid": params[1],
                "action_type": "APPROVE",
                "old_status_code": params[2],
                "new_status_code": 3,
                "old_score": params[3],
                "new_score": params[4],
                "comment": params[5],
                "created_at": datetime.now(),
            })
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


class ApprovalMockConnection:
    def __init__(self):
        self.cursor_instance = ApprovalMockCursor(self)
        self.db_years = [
            {"year_id": 1, "year": 2026, "is_activated": True},
        ]
        self.db_students = [
            {"student_id": 1, "uuid": "student-uuid-1", "name": "홍길동", "is_deleted": False},
            {"student_id": 2, "uuid": "student-uuid-2", "name": "이순신", "is_deleted": False},
        ]
        self.db_teachers = [
            # 1학년 1반 담임
            {"teachers_id": 10, "uuid": "teacher-uuid-10", "name": "김담임", "grade": 1, "class": 1, "is_deleted": False},
            # 2학년 3반 담임
            {"teachers_id": 20, "uuid": "teacher-uuid-20", "name": "박담임", "grade": 2, "class": 3, "is_deleted": False},
            # 비담임 교과교사
            {"teachers_id": 30, "uuid": "teacher-uuid-30", "name": "최교과", "grade": None, "class": None, "is_deleted": False},
        ]
        self.db_records = [
            # 홍길동: 2026학년도 1학년 1반 5번
            {"student_id": 1, "year_id": 1, "year": 2026, "grade": 1, "class_no": 1, "class": 1, "number": 5},
            # 이순신: 2026학년도 2학년 3반 10번
            {"student_id": 2, "year_id": 1, "year": 2026, "grade": 2, "class_no": 3, "class": 3, "number": 10},
        ]
        self.db_areas = [
            {"area_id": 10, "year_id": 1, "grade": 1, "name": "직업기초능력", "max_score": 100.0},
        ]
        self.db_items = [
            {
                "item_id": 101, "area_id": 10, "item_name": "기능사", "name": "기능사",
                "target_grade": 1, "max_score": 50.0, "item_max_score": 50.0,
                "scoring_type": 1, "requires_evidence": True, "is_active": True,
                "year_id": 1, "academic_year": 2026,
            },
        ]
        self.db_submissions = [
            {
                "submission_id": 501, "student_id": 1, "item_id": 101, "detail": "정보처리기능사",
                "activity_date": date(2026, 5, 10), "file_path": "/uploads/submissions/cert.pdf",
                "original_filename": "cert.pdf", "link_url": None, "description": "기능사 취득",
                "status_code": 1, "granted_score": None, "teacher_comment": None,
                "created_at": datetime(2026, 5, 11, 10, 0, 0), "reviewed_at": None, "reviewer_id": None,
                "is_deleted": False, "year": 2026, "year_id": 1, "area_name": "직업기초능력", "item_name": "기능사",
                "scoring_type": 1, "item_max_score": 50.0, "max_score": 50.0, "area_id": 10,
            },
            {
                "submission_id": 502, "student_id": 1, "item_id": 101, "detail": "이미승인건",
                "activity_date": date(2026, 4, 10), "file_path": None, "original_filename": None,
                "link_url": "https://example.com", "description": "이미 승인된 건",
                "status_code": 3, "granted_score": 50.0, "teacher_comment": "기승인",
                "created_at": datetime(2026, 4, 11, 10, 0, 0), "reviewed_at": datetime(2026, 4, 12, 10, 0, 0),
                "reviewer_id": 10, "is_deleted": False, "year": 2026, "year_id": 1, "area_name": "직업기초능력",
                "item_name": "기능사", "scoring_type": 1, "item_max_score": 50.0, "max_score": 50.0, "area_id": 10,
            },
        ]
        self.db_merits = [
            {
                "merits_point_id": 1, "student_id": 1, "teachers_id": 10, "type": "+", "points": 5.0,
                "reason": "봉사상", "related_area": "직업기초능력", "occurred_at": date(2026, 5, 15),
                "is_reflected": True, "is_deleted": False,
            }
        ]
        self.sub_logs = []

    async def autocommit(self, val: bool):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def cursor(self, cursor=None):
        return self.cursor_instance


class TestSubmissionApprovalAPI(unittest.TestCase):
    """3.4 증빙자료 승인 및 점수 부여 API 통합 테스트"""

    def setUp(self):
        self.client = TestClient(app)
        self.conn = ApprovalMockConnection()
        app.dependency_overrides[get_db] = lambda: self.conn

        mock_redis = MagicMock()
        async def _fake_redis_get(key):
            return None
        mock_redis.get = _fake_redis_get
        app.dependency_overrides[get_redis] = lambda: mock_redis

        students_mod._YEAR_ID_CACHE.clear()

        # 토큰 발급
        self.token_student = create_access_token({"sub": "student-uuid-1", "id": "student1", "role": "student"})
        self.token_homeroom = create_access_token({"sub": "teacher-uuid-10", "id": "teacher10", "role": "teacher"})
        self.token_other_teacher = create_access_token({"sub": "teacher-uuid-20", "id": "teacher20", "role": "teacher"})
        self.token_subject_teacher = create_access_token({"sub": "teacher-uuid-30", "id": "teacher30", "role": "teacher"})
        self.token_admin = create_access_token({"sub": "admin-uuid-1", "id": "admin", "role": "admin"})

    def tearDown(self):
        app.dependency_overrides.clear()
        students_mod._YEAR_ID_CACHE.clear()

    def test_student_cannot_approve_forbidden(self):
        """학생 권한으로 승인 호출 시 403 FORBIDDEN 차단"""
        res = self.client.patch(
            "/api/submissions/501/approve",
            json={"score": 50.0, "comment": "학생 자가 승인 시도"},
            headers={"Authorization": f"Bearer {self.token_student}"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(res.json()["success"])
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_cross_class_teacher_cannot_approve_forbidden(self):
        """타 학급 담임교사(2-3)가 1-1 학생 증빙 심사 승인 시 403 FORBIDDEN 차단 (Scope Isolation)"""
        res = self.client.patch(
            "/api/submissions/501/approve",
            json={"score": 50.0, "comment": "타학급 승인 시도"},
            headers={"Authorization": f"Bearer {self.token_other_teacher}"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_score_exceeding_max_score_rejected(self):
        """부여 점수가 항목의 maxScore(50.0)를 초과하는 경우 400 VALIDATION_ERROR 거절"""
        res = self.client.patch(
            "/api/submissions/501/approve",
            json={"score": 55.0, "comment": "점수 초과 부여"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("인정 점수는", res.json()["error"]["message"])

    def test_negative_score_rejected(self):
        """부여 점수가 음수인 경우 400 VALIDATION_ERROR 거절"""
        res = self.client.patch(
            "/api/submissions/501/approve",
            json={"score": -5.0, "comment": "음수 점수"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    def test_submission_not_found_404(self):
        """존재하지 않는 증빙 건 승인 요청 시 404 ITEM_NOT_FOUND 반환"""
        res = self.client.patch(
            "/api/submissions/99999/approve",
            json={"score": 10.0},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "ITEM_NOT_FOUND")

    def test_already_approved_cannot_approve_again(self):
        """이미 인정완료(3) 상태인 건을 다시 승인하려 할 때 400 INVALID_STATUS 거절"""
        res = self.client.patch(
            "/api/submissions/502/approve",
            json={"score": 50.0},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "INVALID_STATUS")

    def test_homeroom_teacher_approve_success_with_student_totals_and_xss_protection(self):
        """
        정당한 담임교사가 증빙을 정상 승인(200 OK)
        - status_code=3 전이
        - reviewerId, grantedScore, teacherComment 저장
        - HTML XSS 방어(escape) 처리
        - submissions_logs에 'APPROVE' 감사 로그 원자적 기록
        - studentTotals 총점 및 인증상태 응답 포함 검증
        """
        xss_comment = "<script>alert('xss')</script>합격입니다."
        res = self.client.patch(
            "/api/submissions/501/approve",
            json={"score": 40.0, "comment": xss_comment},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        data = body["data"]

        self.assertEqual(data["status"], "인정완료")
        self.assertEqual(data["statusCode"], 3)
        self.assertEqual(data["score"], 40.0)
        self.assertEqual(data["grantedScore"], 40.0)
        self.assertEqual(data["reviewerId"], 10)

        # XSS 스크립트 이스케이프 검증
        self.assertNotIn("<script>", data["teacherComment"])
        self.assertIn("&lt;script&gt;", data["teacherComment"])

        # studentTotals 자동 산출 반환 검증
        self.assertIn("studentTotals", data)
        totals = data["studentTotals"]
        self.assertIsNotNone(totals)
        self.assertIn("totalScore", totals)
        self.assertIn("certStatus", totals)
        self.assertIn("pointTotal", totals)

        # submissions_logs 감사 기록 확인
        self.assertTrue(any(log["action_type"] == "APPROVE" and log["new_status_code"] == 3 for log in self.conn.sub_logs))

    def test_subject_teacher_and_admin_can_approve(self):
        """교과교사(전교생 대상) 및 관리자(admin) 또한 승인 권한 허용 검증"""
        # 1. 관리자 승인 -> 200
        res_admin = self.client.patch(
            "/api/submissions/501/approve",
            json={"score": 50.0, "comment": "관리자 승인"},
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(res_admin.status_code, 200)
        self.assertTrue(res_admin.json()["success"])

        # 원복
        self.conn.db_submissions[0]["status_code"] = 1

        # 2. 교과교사(담임 미지정) 승인 -> 200
        res_sub = self.client.patch(
            "/api/submissions/501/approve",
            json={"score": 45.0, "comment": "교과교사 승인"},
            headers={"Authorization": f"Bearer {self.token_subject_teacher}"},
        )
        self.assertEqual(res_sub.status_code, 200)
        self.assertTrue(res_sub.json()["success"])


if __name__ == "__main__":
    unittest.main()
