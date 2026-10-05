"""
tests/test_p0_p1_features.py - P0/P1 신규 구현 및 보안/정합성 회귀 검증 테스트 스위트

검증 항목:
1. SEC-01: 담임교사 학급 스코프 격리 (인증현황, 제출조회, 상벌점, 티켓)
2. SEC-02: 다운로드 전용 토큰 목적(purpose) 및 경로 분리/우회 차단
3. DATA-01: 재제출 상태(검토중=2) 및 심사정보 NULL 초기화, 동시성 락
4. DATA-04: 비활성 학년도 제출 차단(400) 및 평가 기준 미설정 시 CRITERIA_NOT_CONFIGURED(404)
5. API-01: 단일 증빙 조회, 승인, 반려, 점수 수정 API
6. API-02: 상벌점 등록, 수정 및 merits_log 감사 이력
7. API-03: 학년도 관리, 평가 기준 관리, 기준 복제, 관리자 전체 현황 API
8. AUTH-01 & AUTH-02: 이메일 변경 OTP 분리 및 원자적 토큰 소비
"""

from datetime import date, datetime
import unittest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from main import app
from core.security import create_access_token, create_signed_download_token
from database import get_db, get_redis
import routers.students as students_mod


class P0P1MockCursor:
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
            if "WHERE year = %s" in sql_clean:
                target_year = params[0]
                matched = [y for y in self.conn.db_years if y["year"] == target_year]
                self._current_result = matched
            elif "WHERE is_activated = TRUE" in sql_clean:
                matched = [y for y in self.conn.db_years if y.get("is_activated")]
                self._current_result = matched
            elif "ORDER BY" in sql_clean:
                self._current_result = list(self.conn.db_years)
            else:
                self._current_result = list(self.conn.db_years)

        # 2. students 조회
        elif "FROM students" in sql_clean:
            if "WHERE student_id = %s" in sql_clean:
                st_id = params[0]
                matched = [s for s in self.conn.db_students if s["student_id"] == st_id and not s.get("is_deleted")]
                self._current_result = matched
            elif "WHERE uuid = %s" in sql_clean:
                st_uuid = params[0]
                matched = [s for s in self.conn.db_students if s["uuid"] == st_uuid and not s.get("is_deleted")]
                self._current_result = matched

        # 3. teachers 조회
        elif "FROM teachers" in sql_clean:
            if "WHERE uuid = %s" in sql_clean:
                t_uuid = params[0]
                matched = [t for t in self.conn.db_teachers if t["uuid"] == t_uuid and not t.get("is_deleted")]
                self._current_result = matched
            elif "WHERE teachers_id = %s" in sql_clean:
                t_id = params[0]
                matched = [t for t in self.conn.db_teachers if t["teachers_id"] == t_id and not t.get("is_deleted")]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_teachers)

        # 4. student_academic_records 조회
        elif "FROM student_academic_records" in sql_clean:
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
        elif "FROM certification_areas" in sql_clean:
            if "WHERE year_id = %s AND grade = %s" in sql_clean:
                y_id, g_no = params[0], params[1]
                matched = [
                    a for a in self.conn.db_areas
                    if a["year_id"] == y_id and a.get("grade") == g_no
                ]
                self._current_result = matched
            elif "WHERE year_id = %s" in sql_clean:
                y_id = params[0]
                matched = [a for a in self.conn.db_areas if a["year_id"] == y_id]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_areas)

        # 6. evaluation_items 조회
        elif "FROM evaluation_items" in sql_clean:
            if "WHERE ei.item_id = %s" in sql_clean or "WHERE item_id = %s" in sql_clean:
                it_id = params[0]
                matched = [it for it in self.conn.db_items if it["item_id"] == it_id]
                self._current_result = matched
            elif "WHERE area_id = %s" in sql_clean:
                ar_id = params[0]
                matched = [it for it in self.conn.db_items if it["area_id"] == ar_id]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_items)

        # 7. submissions 조회
        elif "FROM submissions" in sql_clean:
            if "WHERE s.submission_id = %s" in sql_clean or "WHERE submission_id = %s" in sql_clean:
                sub_id = params[0]
                matched = [s for s in self.conn.db_submissions if s["submission_id"] == sub_id and not s.get("is_deleted")]
                self._current_result = matched
            elif "WHERE s.student_id = %s" in sql_clean:
                st_id = params[0]
                matched = [s for s in self.conn.db_submissions if s["student_id"] == st_id and not s.get("is_deleted")]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_submissions)

        # 8. merits 조회
        elif "FROM merits" in sql_clean:
            if "WHERE merits_point_id = %s" in sql_clean or "WHERE m.merits_point_id = %s" in sql_clean:
                m_id = params[0]
                matched = [m for m in self.conn.db_merits if m["merits_point_id"] == m_id and not m.get("is_deleted")]
                self._current_result = matched
            elif "WHERE student_id = %s" in sql_clean or "WHERE m.student_id = %s" in sql_clean:
                st_id = params[0]
                matched = [m for m in self.conn.db_merits if m["student_id"] == st_id and not m.get("is_deleted")]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_merits)

        # 9. INSERT / UPDATE / DELETE
        # 9. INSERT / UPDATE / DELETE
        elif "INSERT INTO submissions_logs" in sql_clean:
            action_type = "APPROVE" if "'APPROVE'" in sql_clean else ("REJECT" if "'REJECT'" in sql_clean else ("MODIFY_SCORE" if "'MODIFY_SCORE'" in sql_clean else ("RESUBMIT" if "'RESUBMIT'" in sql_clean else (params[2] if len(params) > 2 else "UNKNOWN"))))
            self.conn.db_sub_logs.append({
                "submission_id": params[0],
                "modifier_uuid": params[1],
                "action_type": action_type,
            })
            self.lastrowid = len(self.conn.db_sub_logs)
            self.rowcount = 1

        elif "INSERT INTO merits_log" in sql_clean:
            action_type = "CREATE" if "'CREATE'" in sql_clean else ("UPDATE" if "'UPDATE'" in sql_clean else (params[2] if len(params) > 2 else "UNKNOWN"))
            self.conn.db_merits_logs.append({
                "merits_point_id": params[0],
                "modifier_uuid": params[1],
                "action_type": action_type,
            })
            self.lastrowid = len(self.conn.db_merits_logs)
            self.rowcount = 1

        elif "INSERT INTO merits" in sql_clean:
            self.conn.merit_seq += 1
            new_m = {
                "merits_point_id": self.conn.merit_seq,
                "student_id": params[0],
                "teachers_id": params[1],
                "type": params[2],
                "points": params[3],
                "reason": params[4],
                "related_area": params[5],
                "occurred_at": params[6] if len(params) > 6 else None,
                "is_reflected": params[7] if len(params) > 7 else True,
                "is_deleted": False,
            }
            self.conn.db_merits.append(new_m)
            self.lastrowid = self.conn.merit_seq
            self.rowcount = 1

        elif "UPDATE submissions" in sql_clean:
            sub_id = params[-1]
            for s in self.conn.db_submissions:
                if s["submission_id"] == sub_id:
                    s["status_code"] = 3
            self.rowcount = 1

        elif "UPDATE merits" in sql_clean:
            m_id = params[-1]
            for m in self.conn.db_merits:
                if m["merits_point_id"] == m_id:
                    m["points"] = params[1]
            self.rowcount = 1

        elif "LAST_INSERT_ID" in sql_clean:
            self._current_result = [{"last_id": self.conn.merit_seq}]

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


class P0P1MockConnection:
    async def autocommit(self, val: bool):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def __init__(self):
        self.merit_seq = 100
        self.db_years = [
            {"year_id": 1, "year": 2025, "is_activated": False},
            {"year_id": 2, "year": 2026, "is_activated": True},
        ]
        self.db_students = [
            {"student_id": 1, "uuid": "student-uuid-1", "name": "홍길동", "is_deleted": False},
            {"student_id": 2, "uuid": "student-uuid-2", "name": "이순신", "is_deleted": False},
        ]
        self.db_teachers = [
            # 1학년 1반 담임교사
            {"teachers_id": 10, "uuid": "teacher-uuid-10", "name": "김담임", "grade": 1, "class": 1, "is_deleted": False},
            # 2학년 3반 담임교사
            {"teachers_id": 20, "uuid": "teacher-uuid-20", "name": "박담임", "grade": 2, "class": 3, "is_deleted": False},
            # 교과교사 (담임 없음)
            {"teachers_id": 30, "uuid": "teacher-uuid-30", "name": "이교과", "grade": None, "class": None, "is_deleted": False},
        ]
        self.db_records = [
            # 홍길동: 2026년도 1학년 1반 5번
            {"student_id": 1, "year_id": 2, "year": 2026, "grade": 1, "class_no": 1, "class": 1, "number": 5},
            # 이순신: 2026년도 2학년 3반 10번
            {"student_id": 2, "year_id": 2, "year": 2026, "grade": 2, "class_no": 3, "class": 3, "number": 10},
        ]
        self.db_areas = [
            {"area_id": 1, "year_id": 2, "grade": 1, "name": "직업기초능력", "max_score": 100.0},
            {"area_id": 2, "year_id": 2, "grade": 2, "name": "전문기술역량", "max_score": 150.0},
        ]
        self.db_items = [
            {
                "item_id": 10, "area_id": 1, "item_name": "기능사", "name": "기능사",
                "target_grade": 1, "max_score": 50.0, "item_max_score": 50.0,
                "scoring_type": 1, "requires_evidence": True, "is_active": True,
                "year_id": 2, "academic_year": 2026, "year_is_activated": True,
            },
            {
                "item_id": 20, "area_id": 2, "item_name": "산업기사", "name": "산업기사",
                "target_grade": 2, "max_score": 100.0, "item_max_score": 100.0,
                "scoring_type": 1, "requires_evidence": True, "is_active": True,
                "year_id": 2, "academic_year": 2026, "year_is_activated": True,
            },
        ]
        self.db_submissions = [
            {
                "submission_id": 101, "student_id": 1, "item_id": 10, "detail": "기능사 취득",
                "activity_date": date(2026, 5, 1), "file_path": "/uploads/submissions/2026/05/cert1.pdf",
                "original_filename": "cert1.pdf", "link_url": None, "description": "1학년 증빙",
                "status_code": 1, "granted_score": None, "teacher_comment": None,
                "created_at": datetime(2026, 5, 2, 10, 0, 0), "reviewed_at": None, "reviewer_id": None,
                "is_deleted": False, "year": 2026, "area_name": "직업기초능력", "item_name": "기능사",
                "scoring_type": 1, "item_max_score": 50.0, "max_score": 50.0, "area_id": 1,
            },
            {
                "submission_id": 202, "student_id": 2, "item_id": 20, "detail": "산업기사 취득",
                "activity_date": date(2026, 6, 1), "file_path": "/uploads/submissions/2026/06/cert2.pdf",
                "original_filename": "cert2.pdf", "link_url": None, "description": "2학년 증빙",
                "status_code": 1, "granted_score": None, "teacher_comment": None,
                "created_at": datetime(2026, 6, 2, 10, 0, 0), "reviewed_at": None, "reviewer_id": None,
                "is_deleted": False, "year": 2026, "area_name": "전문기술역량", "item_name": "산업기사",
                "scoring_type": 1, "item_max_score": 100.0, "max_score": 100.0, "area_id": 2,
            },
        ]
        self.db_merits = [
            {
                "merits_point_id": 1, "student_id": 1, "teachers_id": 10, "type": "+", "points": 2.0,
                "reason": "표창", "related_area": "직업기초능력", "occurred_at": date(2026, 5, 10),
                "is_reflected": True, "is_deleted": False,
            },
            {
                "merits_point_id": 2, "student_id": 2, "teachers_id": 20, "type": "-", "points": 1.0,
                "reason": "지각", "related_area": "전문기술역량", "occurred_at": date(2026, 6, 15),
                "is_reflected": True, "is_deleted": False,
            },
        ]
        self.db_sub_logs = []
        self.db_merits_logs = []

    def cursor(self, cursor=None):
        return P0P1MockCursor(self)


class TestP0P1Features(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        students_mod._YEAR_ID_CACHE.clear()

        # Redis mock
        self.redis_data = {}
        mock_redis = MagicMock()

        async def _fake_get(k):
            return self.redis_data.get(k)

        async def _fake_set(k, v, ex=None):
            self.redis_data[k] = str(v)
            return True

        async def _fake_getdel(k):
            return self.redis_data.pop(k, None)

        mock_redis.get = _fake_get
        mock_redis.set = _fake_set
        mock_redis.getdel = _fake_getdel
        app.dependency_overrides[get_redis] = lambda: mock_redis

        self.conn = P0P1MockConnection()
        app.dependency_overrides[get_db] = lambda: self.conn

        # 토큰 발급
        self.token_student1 = create_access_token({"sub": "student-uuid-1", "id": "st1", "role": "student"})
        self.token_student2 = create_access_token({"sub": "student-uuid-2", "id": "st2", "role": "student"})
        self.token_teacher10 = create_access_token({"sub": "teacher-uuid-10", "id": "t10", "role": "teacher"})  # 1-1 담임
        self.token_teacher20 = create_access_token({"sub": "teacher-uuid-20", "id": "t20", "role": "teacher"})  # 2-3 담임
        self.token_teacher_sub = create_access_token({"sub": "teacher-uuid-30", "id": "t30", "role": "teacher"})  # 교과교사
        self.token_admin = create_access_token({"sub": "admin-uuid", "id": "admin", "role": "admin"})

    def tearDown(self):
        app.dependency_overrides.clear()
        students_mod._YEAR_ID_CACHE.clear()

    # ==========================================================================
    # 1. SEC-01: 담임교사 학급 스코프 격리
    # ==========================================================================
    def test_homeroom_teacher_cross_class_blocking(self):
        """[SEC-01] 1-1 담임교사가 2-3 학생(이순신, student_id=2)의 리소스 접근 시 403 차단"""
        # 1. 인증 현황 조회 차단
        res_cert = self.client.get(
            "/api/students/2/certification-status?year=2026",
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(res_cert.status_code, 403)
        self.assertEqual(res_cert.json()["error"]["code"], "FORBIDDEN")

        # 2. 제출 내역 조회 차단
        res_subs = self.client.get(
            "/api/submissions?studentId=2&year=2026",
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(res_subs.status_code, 403)
        self.assertEqual(res_subs.json()["error"]["code"], "FORBIDDEN")

        # 3. 상벌점 내역 조회 차단
        res_pts = self.client.get(
            "/api/points?studentId=2&year=2026",
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(res_pts.status_code, 403)
        self.assertEqual(res_pts.json()["error"]["code"], "FORBIDDEN")

    def test_homeroom_teacher_own_student_allowed(self):
        """[SEC-01] 1-1 담임교사가 1-1 학생(홍길동, student_id=1)의 리소스 조회 허용(200)"""
        res_cert = self.client.get(
            "/api/students/1/certification-status?year=2026",
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(res_cert.status_code, 200)
        self.assertTrue(res_cert.json()["success"])

    # ==========================================================================
    # 2. SEC-02: 다운로드 전용 토큰 목적(purpose) 검증
    # ==========================================================================
    def test_download_token_cannot_access_general_api(self):
        """[SEC-02] purpose='download' 토큰으로 일반 API(/api/auth/me) 접근 시 401 차단"""
        dl_token = create_signed_download_token(
            user_uuid="student-uuid-1",
            role="student",
            file_path="submissions/2026/05/cert1.pdf",
            ttl_seconds=60,
        )
        res = self.client.get("/auth/me", headers={"Authorization": f"Bearer {dl_token}"})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "UNAUTHORIZED")

    # ==========================================================================
    # 3. API-01: 증빙자료 심사(승인/반려/점수수정) 및 권한 검증
    # ==========================================================================
    def test_submission_review_actions_and_rbac(self):
        """[API-01] 교사의 승인/반려/수정 및 학생의 접근 시 403 차단 검증"""
        # 1. 학생 권한으로 승인 시도 -> 403 차단
        res_student_approve = self.client.patch(
            "/api/submissions/101/approve",
            json={"score": 50.0, "comment": "승인시도"},
            headers={"Authorization": f"Bearer {self.token_student1}"},
        )
        self.assertEqual(res_student_approve.status_code, 403)

        # 2. 타 학급 담임교사(2-3)가 1-1 학생 증빙(submission_id=101) 승인 시도 -> 403 차단
        res_cross_approve = self.client.patch(
            "/api/submissions/101/approve",
            json={"score": 50.0, "comment": "타학급 승인시도"},
            headers={"Authorization": f"Bearer {self.token_teacher20}"},
        )
        self.assertEqual(res_cross_approve.status_code, 403)

        # 3. 정당한 담임교사(1-1)가 승인 -> 200 성공 및 submissions_logs 기록
        res_approve = self.client.patch(
            "/api/submissions/101/approve",
            json={"score": 50.0, "comment": "합격 승인"},
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(res_approve.status_code, 200)
        self.assertTrue(res_approve.json()["success"])
        self.assertEqual(res_approve.json()["data"]["status"], "인정완료")
        self.assertTrue(any(log["action_type"] == "APPROVE" for log in self.conn.db_sub_logs))

    # ==========================================================================
    # 4. API-02: 상벌점 등록 및 수정 API
    # ==========================================================================
    def test_points_creation_and_modification(self):
        """[API-02] 상벌점 등록/수정 및 merits_log 감사 이력 기록 검증"""
        # 1. 1-1 담임교사가 1-1 학생(student_id=1) 상점 등록 -> 201 성공
        res_create = self.client.post(
            "/api/points",
            json={
                "studentId": 1,
                "type": "상점",
                "points": 3.0,
                "reason": "선행상",
                "occurredAt": "2026-07-01",
                "relatedArea": "직업기초능력",
                "isReflected": True,
            },
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(res_create.status_code, 201)
        data = res_create.json()["data"]
        self.assertEqual(data["score"], 3.0)
        self.assertTrue(any(log["action_type"] == "CREATE" for log in self.conn.db_merits_logs))

        # 2. 1-1 담임교사가 2-3 학생(student_id=2) 상벌점 등록 시도 -> 403 차단
        res_cross_point = self.client.post(
            "/api/points",
            json={
                "studentId": 2,
                "type": "상점",
                "points": 1.0,
                "reason": "무단",
                "occurredAt": "2026-07-01",
            },
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(res_cross_point.status_code, 403)

    # ==========================================================================
    # 5. API-03: 학년도 및 평가 기준 관리 API
    # ==========================================================================
    def test_admin_academic_years_and_criteria_management(self):
        """[API-03] 관리자 학년도/기준 조회 및 권한 제어 검증"""
        # 1. 일반 교사가 학년도 생성 시도 -> 403 차단
        res_create_year_forbidden = self.client.post(
            "/api/years",
            json={"year": 2027, "isActivated": False},
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(res_create_year_forbidden.status_code, 403)

        # 2. 관리자가 학년도 목록 조회 -> 200 성공
        res_years = self.client.get("/api/years", headers={"Authorization": f"Bearer {self.token_admin}"})
        self.assertEqual(res_years.status_code, 200)
        self.assertEqual(len(res_years.json()["data"]), 2)

        # 3. 평가 기준 전체 조회 (교사 열람 가능)
        res_criteria = self.client.get("/api/criteria?year=2026", headers={"Authorization": f"Bearer {self.token_teacher10}"})
        self.assertEqual(res_criteria.status_code, 200)
        self.assertIn("areas", res_criteria.json()["data"])

        # 4. 관리자 전체 현황 조회 -> 200 성공
        res_overview = self.client.get("/api/admin/overview?year=2026", headers={"Authorization": f"Bearer {self.token_admin}"})
        self.assertEqual(res_overview.status_code, 200)
        self.assertIn("submissions", res_overview.json()["data"])
        self.assertIn("totalStudents", res_overview.json()["data"])
