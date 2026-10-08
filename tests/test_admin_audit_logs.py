"""
tests/test_admin_audit_logs.py - 관리자 시스템 종합 감사 로그 조회(4.8) 단위/통합 테스트
"""

import unittest
from datetime import datetime
from typing import Any, Dict, List
from fastapi.testclient import TestClient

from core.security import get_current_user
from database import get_db, get_redis
from main import app


class AuditLogsMockRedis:
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


class AuditLogsMockCursor:
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

        # 1. COUNT(*) 쿼리
        if "SELECT COUNT(*) AS total_count FROM" in sql_clean:
            # params에서 필터링 조건을 시뮬레이션
            filtered = self.conn._filter_logs(sql_clean, params)
            self._current_result = [{"total_count": len(filtered)}]

        # 2. 페이징 쿼리
        elif "FROM (" in sql_clean and "AS combined_logs" in sql_clean and "ORDER BY created_at DESC" in sql_clean:
            # params: base_params + [limit, offset]
            limit = params[-2]
            offset = params[-1]
            base_params = params[:-2]

            filtered = self.conn._filter_logs(sql_clean, base_params)
            # sort by created_at DESC, original_log_id DESC
            sorted_logs = sorted(
                filtered,
                key=lambda x: (x.get("created_at") or datetime.min, x.get("original_log_id", 0)),
                reverse=True,
            )
            paged = sorted_logs[offset : offset + limit]
            self._current_result = paged
        else:
            self._current_result = []

    async def fetchone(self):
        if self._fetch_index < len(self._current_result):
            res = self._current_result[self._fetch_index]
            self._fetch_index += 1
            return res
        return None

    async def fetchall(self):
        res = self._current_result[self._fetch_index :]
        self._fetch_index = len(self._current_result)
        return res


class AuditLogsMockConnection:
    def __init__(self):
        self.db_submission_logs = [
            {
                "unified_id": "SUB_1",
                "original_log_id": 1,
                "log_type": "SUBMISSION",
                "action_type": "SUBMIT",
                "target_id": 101,
                "student_id": 1,
                "student_name": "홍길동",
                "student_grade": 1,
                "student_class": 1,
                "student_number": 1,
                "modifier_uuid": "student-uuid-1",
                "modifier_name": "홍길동",
                "modifier_role": "student",
                "old_status_code": None,
                "new_status_code": 1,
                "old_score": None,
                "new_score": None,
                "old_points": None,
                "new_points": None,
                "reason": "정보처리기능사 취득 제출",
                "target_title": "정보처리기능사",
                "created_at": datetime(2026, 3, 10, 10, 0, 0),
            },
            {
                "unified_id": "SUB_2",
                "original_log_id": 2,
                "log_type": "SUBMISSION",
                "action_type": "APPROVE",
                "target_id": 101,
                "student_id": 1,
                "student_name": "홍길동",
                "student_grade": 1,
                "student_class": 1,
                "student_number": 1,
                "modifier_uuid": "teacher-uuid-1",
                "modifier_name": "김담임",
                "modifier_role": "teacher",
                "old_status_code": 1,
                "new_status_code": 3,
                "old_score": None,
                "new_score": 50.0,
                "old_points": None,
                "new_points": None,
                "reason": "자격증 진본 확인 완료",
                "target_title": "정보처리기능사",
                "created_at": datetime(2026, 3, 11, 14, 0, 0),
            },
            {
                "unified_id": "SUB_3",
                "original_log_id": 3,
                "log_type": "SUBMISSION",
                "action_type": "SCORE_MODIFY",
                "target_id": 102,
                "student_id": 2,
                "student_name": "성춘향",
                "student_grade": 2,
                "student_class": 3,
                "student_number": 5,
                "modifier_uuid": "admin-uuid-1",
                "modifier_name": "관리자",
                "modifier_role": "admin",
                "old_status_code": 3,
                "new_status_code": 3,
                "old_score": 30.0,
                "new_score": 40.0,
                "old_points": None,
                "new_points": None,
                "reason": "가산점 오기재 재산정",
                "target_title": "TOEIC 700",
                "created_at": datetime(2026, 3, 12, 9, 30, 0),
            },
        ]

        self.db_merit_logs = [
            {
                "unified_id": "MERIT_1",
                "original_log_id": 1,
                "log_type": "MERIT",
                "action_type": "CREATE",
                "target_id": 201,
                "student_id": 1,
                "student_name": "홍길동",
                "student_grade": 1,
                "student_class": 1,
                "student_number": 1,
                "modifier_uuid": "teacher-uuid-1",
                "modifier_name": "김담임",
                "modifier_role": "teacher",
                "old_status_code": None,
                "new_status_code": None,
                "old_score": None,
                "new_score": None,
                "old_points": None,
                "new_points": 2.0,
                "reason": "학급 환경미화 봉사 우수",
                "target_title": "학급 환경미화 봉사 우수",
                "created_at": datetime(2026, 3, 15, 16, 0, 0),
            },
            {
                "unified_id": "MERIT_2",
                "original_log_id": 2,
                "log_type": "MERIT",
                "action_type": "DELETE",
                "target_id": 202,
                "student_id": 2,
                "student_name": "성춘향",
                "student_grade": 2,
                "student_class": 3,
                "student_number": 5,
                "modifier_uuid": "admin-uuid-1",
                "modifier_name": "관리자",
                "modifier_role": "admin",
                "old_status_code": None,
                "new_status_code": None,
                "old_score": None,
                "new_score": None,
                "old_points": -1.0,
                "new_points": None,
                "reason": "오등록된 벌점 취소",
                "target_title": "지각 1회",
                "created_at": datetime(2026, 3, 16, 11, 20, 0),
            },
        ]

    def cursor(self, cursor=None):
        return AuditLogsMockCursor(self)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def autocommit(self, val):
        pass

    def _filter_logs(self, sql: str, params: tuple) -> List[Dict[str, Any]]:
        is_sub = "submissions_logs" in sql
        is_merit = "merits_log" in sql

        all_logs = []
        if is_sub and is_merit:
            all_logs = list(self.db_submission_logs) + list(self.db_merit_logs)
        elif is_sub:
            all_logs = list(self.db_submission_logs)
        elif is_merit:
            all_logs = list(self.db_merit_logs)
        else:
            all_logs = list(self.db_submission_logs) + list(self.db_merit_logs)

        # 파라미터 기반 매칭 필터링
        res = []
        for log in all_logs:
            matched = True

            # action_type check
            for p in params:
                if isinstance(p, str) and p in ("SUBMIT", "APPROVE", "SCORE_MODIFY", "CREATE", "DELETE", "UPDATE"):
                    if log["action_type"] != p:
                        matched = False
                        break
            if not matched:
                continue

            # student_id check
            for p in params:
                if isinstance(p, int):
                    if log["student_id"] != p:
                        matched = False
                        break
            if not matched:
                continue

            # student_name LIKE %name%
            for p in params:
                if isinstance(p, str) and p.startswith("%") and p.endswith("%"):
                    sub_str = p[1:-1]
                    # modifier_name vs student_name
                    if sub_str in ("홍길동", "춘향"):
                        if sub_str not in log["student_name"]:
                            matched = False
                            break
                    elif sub_str in ("김담임", "관리자"):
                        if sub_str not in log["modifier_name"]:
                            matched = False
                            break
            if not matched:
                continue

            # date check
            for p in params:
                if isinstance(p, str) and len(p) >= 10 and ("-" in p) and not p.startswith("%"):
                    try:
                        p_dt = datetime.strptime(p[:19], "%Y-%m-%d %H:%M:%S")
                        if ">= %s" in sql and p.endswith("00:00:00"):
                            if log["created_at"] < p_dt:
                                matched = False
                                break
                        elif "<= %s" in sql and p.endswith("23:59:59"):
                            if log["created_at"] > p_dt:
                                matched = False
                                break
                    except Exception:
                        pass
            if not matched:
                continue

            res.append(log)

        return res


class TestAdminAuditLogsAPI(unittest.TestCase):
    def setUp(self):
        self.mock_redis = AuditLogsMockRedis()
        self.mock_conn = AuditLogsMockConnection()

        app.dependency_overrides[get_redis] = lambda: self.mock_redis
        app.dependency_overrides[get_db] = lambda: self.mock_conn

        self.admin_user = {
            "uuid": "admin-uuid-1",
            "email": "admin@bssm.hs.kr",
            "role": "admin",
            "name": "관리자",
        }
        self.teacher_user = {
            "uuid": "teacher-uuid-1",
            "email": "teacher@bssm.hs.kr",
            "role": "teacher",
            "name": "김담임",
        }
        self.student_user = {
            "uuid": "student-uuid-1",
            "email": "student@bssm.hs.kr",
            "role": "student",
            "name": "홍길동",
        }

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_unauthenticated_access_blocked(self):
        """인증 없이 접근 시 401 Unauthorized 차단 검증"""
        client = TestClient(app)
        res = client.get("/api/admin/audit-logs")
        self.assertEqual(res.status_code, 401)

    def test_teacher_and_student_access_forbidden(self):
        """교사 또는 학생 권한으로 접근 시 403 Forbidden 차단 검증"""
        client = TestClient(app)

        # 1. 교사 접근 시도
        app.dependency_overrides[get_current_user] = lambda: self.teacher_user
        res_t = client.get("/api/admin/audit-logs")
        self.assertEqual(res_t.status_code, 403)
        self.assertEqual(res_t.json().get("error", {}).get("code"), "FORBIDDEN")

        # 2. 학생 접근 시도
        app.dependency_overrides[get_current_user] = lambda: self.student_user
        res_s = client.get("/api/admin/audit-logs")
        self.assertEqual(res_s.status_code, 403)
        self.assertEqual(res_s.json().get("error", {}).get("code"), "FORBIDDEN")

    def test_admin_get_all_audit_logs_default(self):
        """관리자가 파라미터 없이 기본 조회 시 통합 로그(SUBMISSION + MERIT) 최신순 조회 검증"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/audit-logs")
        self.assertEqual(res.status_code, 200)

        data = res.json()["data"]
        logs = data["logs"]
        pagination = data["pagination"]

        # 총 5건 (증빙 3건 + 상벌점 2건)
        self.assertEqual(pagination["totalCount"], 5)
        self.assertEqual(pagination["page"], 1)
        self.assertEqual(pagination["limit"], 20)
        self.assertEqual(len(logs), 5)

        # 최신순 정렬 확인 (2026-03-16 MERIT_2가 1위)
        self.assertEqual(logs[0]["id"], "MERIT_2")
        self.assertEqual(logs[0]["logType"], "MERIT")
        self.assertEqual(logs[0]["actionType"], "DELETE")
        self.assertEqual(logs[0]["modifier"]["name"], "관리자")
        self.assertEqual(logs[0]["modifier"]["role"], "admin")
        self.assertEqual(logs[0]["changes"]["oldPoints"], -1.0)
        self.assertIsNone(logs[0]["changes"]["newPoints"])

        # 증빙 심사 로그 항목 매핑 검증
        approve_log = next(item for item in logs if item["id"] == "SUB_2")
        self.assertEqual(approve_log["logType"], "SUBMISSION")
        self.assertEqual(approve_log["actionType"], "APPROVE")
        self.assertEqual(approve_log["modifier"]["name"], "김담임")
        self.assertEqual(approve_log["modifier"]["role"], "teacher")
        self.assertEqual(approve_log["changes"]["oldStatusCode"], 1)
        self.assertEqual(approve_log["changes"]["oldStatusName"], "제출완료")
        self.assertEqual(approve_log["changes"]["newStatusCode"], 3)
        self.assertEqual(approve_log["changes"]["newStatusName"], "인정완료")
        self.assertEqual(approve_log["changes"]["newScore"], 50.0)

    def test_filter_by_log_type_submission(self):
        """log_type='SUBMISSION' 지정 시 증빙자료 관련 로그만 필터링 조회 검증"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/audit-logs?log_type=SUBMISSION")
        self.assertEqual(res.status_code, 200)

        data = res.json()["data"]
        logs = data["logs"]
        self.assertEqual(len(logs), 3)
        self.assertTrue(all(l["logType"] == "SUBMISSION" for l in logs))

    def test_filter_by_log_type_merit(self):
        """log_type='MERIT' 지정 시 상벌점 관련 로그만 필터링 조회 검증"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/audit-logs?log_type=MERIT")
        self.assertEqual(res.status_code, 200)

        data = res.json()["data"]
        logs = data["logs"]
        self.assertEqual(len(logs), 2)
        self.assertTrue(all(l["logType"] == "MERIT" for l in logs))

    def test_invalid_log_type_returns_400(self):
        """잘못된 log_type 전달 시 400 INVALID_LOG_TYPE 반환 검증"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/audit-logs?log_type=UNKNOWN")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json().get("error", {}).get("code"), "INVALID_LOG_TYPE")

    def test_filter_by_action_type(self):
        """action_type 필터링 조회 검증 (예: 'APPROVE')"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/audit-logs?action_type=APPROVE")
        self.assertEqual(res.status_code, 200)

        data = res.json()["data"]
        logs = data["logs"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["actionType"], "APPROVE")
        self.assertEqual(logs[0]["id"], "SUB_2")

    def test_filter_by_student_id(self):
        """student_id 필터링 조회 검증 (홍길동 student_id=1)"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/audit-logs?student_id=1")
        self.assertEqual(res.status_code, 200)

        data = res.json()["data"]
        logs = data["logs"]
        self.assertEqual(len(logs), 3)  # SUB_1, SUB_2, MERIT_1
        self.assertTrue(all(l["student"]["id"] == 1 for l in logs))

    def test_filter_by_student_name(self):
        """student_name 부분 일치 검색 검증 ('춘향')"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/audit-logs?student_name=춘향")
        self.assertEqual(res.status_code, 200)

        data = res.json()["data"]
        logs = data["logs"]
        self.assertEqual(len(logs), 2)  # SUB_3, MERIT_2
        self.assertTrue(all(l["student"]["name"] == "성춘향" for l in logs))

    def test_filter_by_modifier_name(self):
        """modifier_name 부분 일치 검색 검증 ('김담임')"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/audit-logs?modifier_name=김담임")
        self.assertEqual(res.status_code, 200)

        data = res.json()["data"]
        logs = data["logs"]
        self.assertEqual(len(logs), 2)  # SUB_2, MERIT_1
        self.assertTrue(all(l["modifier"]["name"] == "김담임" for l in logs))

    def test_pagination_and_limits(self):
        """페이지네이션 매개변수 동작 및 메타데이터 산출 검증"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        # page=1, limit=2
        res1 = client.get("/api/admin/audit-logs?page=1&limit=2")
        self.assertEqual(res1.status_code, 200)
        p1_data = res1.json()["data"]
        self.assertEqual(len(p1_data["logs"]), 2)
        self.assertEqual(p1_data["pagination"]["page"], 1)
        self.assertEqual(p1_data["pagination"]["limit"], 2)
        self.assertEqual(p1_data["pagination"]["totalPages"], 3)
        self.assertTrue(p1_data["pagination"]["hasNext"])
        self.assertFalse(p1_data["pagination"]["hasPrev"])

        # page=2, limit=2
        res2 = client.get("/api/admin/audit-logs?page=2&limit=2")
        self.assertEqual(res2.status_code, 200)
        p2_data = res2.json()["data"]
        self.assertEqual(len(p2_data["logs"]), 2)
        self.assertEqual(p2_data["pagination"]["page"], 2)
        self.assertTrue(p2_data["pagination"]["hasNext"])
        self.assertTrue(p2_data["pagination"]["hasPrev"])

        # page=3, limit=2 (마지막 1건)
        res3 = client.get("/api/admin/audit-logs?page=3&limit=2")
        self.assertEqual(res3.status_code, 200)
        p3_data = res3.json()["data"]
        self.assertEqual(len(p3_data["logs"]), 1)
        self.assertFalse(p3_data["pagination"]["hasNext"])
        self.assertTrue(p3_data["pagination"]["hasPrev"])


if __name__ == "__main__":
    unittest.main()
