"""
tests/test_submission_score_modify.py - 3.6 증빙자료 점수 사후 수정 API 전용 단위/통합 테스트
명세: Tech_spec.md 3.4 & TODO.md 3.6 (PATCH /api/submissions/{submissionId}/score)

검증 목록:
1. RBAC & 스코프 인가:
   - 학생 계정 점수 수정 시도 시 403 FORBIDDEN 차단
   - 타 학급 담임교사 점수 수정 시도 시 403 FORBIDDEN 차단 (Scope Isolation)
   - 담임교사(자학급), 교과교사(전교생), 관리자(admin) 점수 수정 허용 (200 OK)
2. 유효성 검증:
   - 승인 완료(3) 상태가 아닌 건(status_code 1, 2, 4 등) 수정 시도 시 400 INVALID_STATUS 거절
   - 수정 사유(reason) 누락 또는 빈 공백인 경우 400 VALIDATION_ERROR 거절
   - 수정 점수(score) 누락인 경우 400 VALIDATION_ERROR 거절
   - 음수 점수 또는 항목의 maxScore(50.0) 초과 시 400 VALIDATION_ERROR 거절
   - 존재하지 않는 submission_id -> 404 ITEM_NOT_FOUND
3. 비즈니스 로직 및 감사 추적:
   - granted_score 갱신
   - submissions_logs에 'SCORE_MODIFY' 액션, 이전 점수(old_score), 신규 점수(new_score), 사유 원자적 영구 기록
   - studentTotals 총점 및 인증상태 실시간 재계산 응답 반환
4. 보안 방어:
   - 수정 사유(reason) HTML XSS 문자열 이스케이프 검증 (<script> -> &lt;script&gt;)
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


class ScoreModifyMockCursor:
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
                matched = [dict(s) for s in self.conn.db_submissions if s["submission_id"] == sub_id and not s.get("is_deleted")]
                self._current_result = matched
            elif "WHERE s.student_id = %s" in q or "WHERE student_id = %s" in q:
                st_id = params[1] if len(params) > 1 and "year_id" in q else params[0]
                matched = [dict(s) for s in self.conn.db_submissions if s["student_id"] == st_id and not s.get("is_deleted")]
                self._current_result = matched
            else:
                self._current_result = [dict(s) for s in self.conn.db_submissions]

        # 8. merits 조회
        elif "FROM merits" in q:
            st_id = params[0]
            matched = [m for m in self.conn.db_merits if m["student_id"] == st_id and not m.get("is_deleted")]
            self._current_result = matched

        # 9. UPDATE submissions (점수 수정)
        elif "UPDATE submissions" in q:
            score, comment, reviewer_id, sub_id = params[0], params[1], params[2], params[3]
            updated = False
            for s in self.conn.db_submissions:
                if s["submission_id"] == sub_id and s["status_code"] == 3:
                    s["granted_score"] = score
                    s["teacher_comment"] = comment
                    s["reviewer_id"] = reviewer_id or s.get("reviewer_id")
                    s["reviewed_at"] = datetime.now()
                    updated = True
                    break
            self.rowcount = 1 if updated else 0

        # 10. INSERT INTO submissions_logs
        elif "INSERT INTO submissions_logs" in q:
            # (submission_id, user_uuid, row.get("granted_score"), score, comment)
            # VALUES (%s, %s, 'SCORE_MODIFY', 3, 3, %s, %s, %s, NOW())
            self.conn.sub_logs.append({
                "submission_id": params[0],
                "modifier_uuid": params[1],
                "action_type": "SCORE_MODIFY",
                "old_status_code": 3,
                "new_status_code": 3,
                "old_score": params[2],
                "new_score": params[3],
                "comment": params[4],
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


class ScoreModifyMockConnection:
    def __init__(self):
        self.cursor_instance = ScoreModifyMockCursor(self)
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
            # 승인 완료된 증빙 건 (status_code=3, 초기 점수 30.0)
            {
                "submission_id": 501, "student_id": 1, "item_id": 101, "detail": "정보처리기능사",
                "activity_date": date(2026, 5, 10), "file_path": "/uploads/submissions/cert.pdf",
                "original_filename": "cert.pdf", "link_url": None, "description": "기능사 취득",
                "status_code": 3, "granted_score": 30.0, "teacher_comment": "최초 승인",
                "created_at": datetime(2026, 5, 11, 10, 0, 0), "reviewed_at": datetime(2026, 5, 12, 10, 0, 0),
                "reviewer_id": 10, "is_deleted": False, "year": 2026, "year_id": 1, "area_name": "직업기초능력",
                "item_name": "기능사", "scoring_type": 1, "item_max_score": 50.0, "max_score": 50.0, "area_id": 10,
            },
            # 대기 중인 증빙 건 (status_code=1)
            {
                "submission_id": 502, "student_id": 1, "item_id": 101, "detail": "심사대기건",
                "activity_date": date(2026, 4, 10), "file_path": None, "original_filename": None,
                "link_url": "https://example.com", "description": "심사 대기",
                "status_code": 1, "granted_score": None, "teacher_comment": None,
                "created_at": datetime(2026, 4, 11, 10, 0, 0), "reviewed_at": None,
                "reviewer_id": None, "is_deleted": False, "year": 2026, "year_id": 1, "area_name": "직업기초능력",
                "item_name": "기능사", "scoring_type": 1, "item_max_score": 50.0, "max_score": 50.0, "area_id": 10,
            },
        ]
        self.db_merits = []
        self.sub_logs = []

    async def autocommit(self, val: bool):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def cursor(self, cursor=None):
        return self.cursor_instance


class TestSubmissionScoreModifyAPI(unittest.TestCase):
    """3.6 증빙자료 점수 사후 수정 API 단위/통합 테스트"""

    def setUp(self):
        self.client = TestClient(app)
        self.conn = ScoreModifyMockConnection()
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

    def test_student_cannot_modify_score_forbidden(self):
        """학생 권한으로 점수 수정 호출 시 403 FORBIDDEN 차단"""
        res = self.client.patch(
            "/api/submissions/501/score",
            json={"score": 50.0, "reason": "점수 셀프 상향"},
            headers={"Authorization": f"Bearer {self.token_student}"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_cross_class_teacher_cannot_modify_score_forbidden(self):
        """타 학급 담임교사(2-3)가 1-1 학생 증빙 점수 수정 시 403 FORBIDDEN 차단 (Scope Isolation)"""
        res = self.client.patch(
            "/api/submissions/501/score",
            json={"score": 40.0, "reason": "타학급 점수 수정 시도"},
            headers={"Authorization": f"Bearer {self.token_other_teacher}"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_not_approved_submission_cannot_modify_score(self):
        """승인 완료(3) 상태가 아닌 대기건(status_code=1) 수정 시도 시 400 INVALID_STATUS 거절"""
        res = self.client.patch(
            "/api/submissions/502/score",
            json={"score": 40.0, "reason": "미승인건 점수 수정"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "INVALID_STATUS")

    def test_missing_reason_validation_error(self):
        """수정 사유(reason) 누락 또는 공백인 경우 400 VALIDATION_ERROR 거절"""
        res_empty = self.client.patch(
            "/api/submissions/501/score",
            json={"score": 40.0, "reason": "   "},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res_empty.status_code, 400)
        self.assertEqual(res_empty.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("사유", res_empty.json()["error"]["message"])

        res_none = self.client.patch(
            "/api/submissions/501/score",
            json={"score": 40.0},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res_none.status_code, 400)
        self.assertEqual(res_none.json()["error"]["code"], "VALIDATION_ERROR")

    def test_missing_score_or_invalid_score_bounds(self):
        """점수 누락 또는 음수/maxScore 초과 점수 입력 시 400 VALIDATION_ERROR 거절"""
        # 1. 점수 누락
        res1 = self.client.patch(
            "/api/submissions/501/score",
            json={"reason": "점수 값 누락"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res1.status_code, 400)
        self.assertEqual(res1.json()["error"]["code"], "VALIDATION_ERROR")

        # 2. 음수 점수
        res2 = self.client.patch(
            "/api/submissions/501/score",
            json={"score": -10.0, "reason": "음수 점수"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res2.status_code, 400)
        self.assertEqual(res2.json()["error"]["code"], "VALIDATION_ERROR")

        # 3. 최대 배점(50.0) 초과 점수
        res3 = self.client.patch(
            "/api/submissions/501/score",
            json={"score": 60.0, "reason": "배점 초과"},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res3.status_code, 400)
        self.assertEqual(res3.json()["error"]["code"], "VALIDATION_ERROR")

    def test_homeroom_teacher_modify_score_success_with_audit_and_student_totals(self):
        """
        정당한 담임교사의 점수 사후 수정 성공 (200 OK)
        - granted_score 30.0 -> 45.0 갱신
        - submissions_logs에 old_score(30.0), new_score(45.0), reason 기록
        - XSS 문자열 방어(escape) 처리
        - studentTotals 총점 및 인증상태 반환 검증
        """
        xss_reason = "<script>alert('hack')</script>증빙 재검토에 따른 점수 상향"
        res = self.client.patch(
            "/api/submissions/501/score",
            json={"score": 45.0, "reason": xss_reason},
            headers={"Authorization": f"Bearer {self.token_homeroom}"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        data = body["data"]

        self.assertEqual(data["status"], "인정완료")
        self.assertEqual(data["statusCode"], 3)
        self.assertEqual(data["score"], 45.0)
        self.assertEqual(data["grantedScore"], 45.0)

        # XSS 이스케이프 검증
        self.assertNotIn("<script>", data["reason"])
        self.assertIn("&lt;script&gt;", data["reason"])

        # studentTotals 반환 검증
        self.assertIn("studentTotals", data)
        self.assertIsNotNone(data["studentTotals"])
        self.assertEqual(data["studentTotals"]["totalScore"], 45.0)

        # submissions_logs 감사 로그 검증 (old_score=30.0, new_score=45.0)
        audit_log = next((l for l in self.conn.sub_logs if l["action_type"] == "SCORE_MODIFY"), None)
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log["old_score"], 30.0)
        self.assertEqual(audit_log["new_score"], 45.0)
        self.assertIn("&lt;script&gt;", audit_log["comment"])

    def test_subject_teacher_and_admin_can_modify_score(self):
        """비담임 교과교사 및 관리자(admin) 점수 사후 수정 권한 정상 허용 검증"""
        # 1. 관리자 점수 수정 -> 200
        res_admin = self.client.patch(
            "/api/submissions/501/score",
            json={"score": 50.0, "reason": "관리자 점수 정정"},
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(res_admin.status_code, 200)
        self.assertEqual(res_admin.json()["data"]["score"], 50.0)

        # 2. 교과교사 점수 수정 -> 200
        res_sub = self.client.patch(
            "/api/submissions/501/score",
            json={"score": 40.0, "reason": "교과교사 점수 정정"},
            headers={"Authorization": f"Bearer {self.token_subject_teacher}"},
        )
        self.assertEqual(res_sub.status_code, 200)
        self.assertEqual(res_sub.json()["data"]["score"], 40.0)


if __name__ == "__main__":
    unittest.main()
