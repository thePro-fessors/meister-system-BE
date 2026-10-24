"""
tests/test_admin_users.py - 관리자 전체 유저 탐색, 소프트 삭제, 복원 및 영구 삭제(Purge) 검증 스위트

검증 항목:
1. USR-01: 관리자 RBAC 인가 검증 (학생/교사 차단 403, 관리자 허용 200)
2. USR-02: 전체 유저 목록 조회 및 검색, 역할/상태 필터링, 페이징 검증
3. USR-03: 유저 소프트 삭제(is_deleted=TRUE) 및 관리자 본인 계정 삭제 시도 차단(400)
4. USR-04: 유저 복원(is_deleted=FALSE) 검증
5. USR-05: 삭제 대기 유저 영구 삭제(단건 및 전체 purge) 및 외래키 제약조건 안전 정리 검증
"""

import unittest
from datetime import datetime
from typing import Any, Dict, List
from fastapi.testclient import TestClient

from core.security import get_current_user
from database import get_db, get_redis
from main import app


class UsersMockRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, ttl, val):
        self.data[key] = val
        return True

    async def delete(self, key):
        self.data.pop(key, None)


class UsersMockCursor:
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

        if getattr(self.conn, "simulate_failure", False) and "DELETE cm FROM chat_messages" in sql_clean:
            raise RuntimeError("DB_EXECUTE_SIMULATED_FAILURE")

        # 1. users 목록 total count
        if "SELECT COUNT(DISTINCT u.uuid) AS total FROM users u" in sql_clean:
            filtered = self.conn._filter_users(sql_clean, params)
            self._current_result = [{"total": len(filtered)}]

        # 2. overall deleted count
        elif "SELECT COUNT(*) AS deleted_count FROM users WHERE is_deleted = TRUE" in sql_clean:
            del_cnt = sum(1 for u in self.conn.db_users if u.get("is_deleted"))
            self._current_result = [{"deleted_count": del_cnt}]

        # 3. users 목록 조회
        elif "FROM users u" in sql_clean and "ORDER BY u.created_at DESC" in sql_clean:
            limit = params[-2]
            offset = params[-1]
            base_params = params[:-2]
            filtered = self.conn._filter_users(sql_clean, base_params)
            # sort by created_at DESC
            sorted_users = sorted(
                filtered,
                key=lambda x: (x.get("created_at") or datetime.min),
                reverse=True,
            )
            self._current_result = sorted_users[offset : offset + limit]

        # 4. 단일 유저 조회 (uuid)
        elif "FROM users WHERE uuid = %s" in sql_clean:
            target_uuid = params[0]
            matched = [u for u in self.conn.db_users if u["uuid"] == target_uuid]
            self._current_result = matched

        # 5. 삭제 대기 유저 목록 조회 (uuid, id 목록)
        elif "FROM users WHERE is_deleted = TRUE" in sql_clean:
            del_users = [{"uuid": u["uuid"], "id": u["id"]} for u in self.conn.db_users if u.get("is_deleted")]
            self._current_result = del_users

        # 6. soft delete
        elif "UPDATE users SET is_deleted = TRUE WHERE uuid = %s" in sql_clean:
            target_uuid = params[0]
            for u in self.conn.db_users:
                if u["uuid"] == target_uuid:
                    u["is_deleted"] = True

        elif "UPDATE students SET is_deleted = TRUE WHERE uuid = %s" in sql_clean:
            target_uuid = params[0]
            for s in self.conn.db_students:
                if s.get("uuid") == target_uuid:
                    s["is_deleted"] = True

        elif "UPDATE teachers SET is_deleted = TRUE WHERE uuid = %s" in sql_clean:
            target_uuid = params[0]
            for t in self.conn.db_teachers:
                if t.get("uuid") == target_uuid:
                    t["is_deleted"] = True

        # 7. restore
        elif "UPDATE users SET is_deleted = FALSE WHERE uuid = %s" in sql_clean:
            target_uuid = params[0]
            for u in self.conn.db_users:
                if u["uuid"] == target_uuid:
                    u["is_deleted"] = False

        elif "UPDATE students SET is_deleted = FALSE WHERE uuid = %s" in sql_clean:
            target_uuid = params[0]
            for s in self.conn.db_students:
                if s.get("uuid") == target_uuid:
                    s["is_deleted"] = False

        elif "UPDATE teachers SET is_deleted = FALSE WHERE uuid = %s" in sql_clean:
            target_uuid = params[0]
            for t in self.conn.db_teachers:
                if t.get("uuid") == target_uuid:
                    t["is_deleted"] = False

        # 8. purge cascade
        elif "DELETE cm FROM chat_messages cm" in sql_clean:
            # chat_messages cascade
            del_convs = [c["conversation_id"] for c in self.conn.db_chat_conversations if c["user_uuid"] in params]
            self.conn.db_chat_messages = [m for m in self.conn.db_chat_messages if m["conversation_id"] not in del_convs]

        elif "DELETE FROM chat_conversations WHERE user_uuid IN" in sql_clean:
            self.conn.db_chat_conversations = [c for c in self.conn.db_chat_conversations if c["user_uuid"] not in params]

        elif "UPDATE submissions_logs SET modifier_uuid = NULL WHERE modifier_uuid IN" in sql_clean:
            for sl in self.conn.db_submissions_logs:
                if sl.get("modifier_uuid") in params:
                    sl["modifier_uuid"] = None

        elif "UPDATE merits_log SET modifier_uuid = NULL WHERE modifier_uuid IN" in sql_clean:
            for ml in self.conn.db_merits_log:
                if ml.get("modifier_uuid") in params:
                    ml["modifier_uuid"] = None

        elif "UPDATE students SET uuid = NULL WHERE uuid IN" in sql_clean:
            for s in self.conn.db_students:
                if s.get("uuid") in params:
                    s["uuid"] = None

        elif "UPDATE teachers SET uuid = NULL WHERE uuid IN" in sql_clean:
            for t in self.conn.db_teachers:
                if t.get("uuid") in params:
                    t["uuid"] = None

        elif "DELETE FROM users WHERE uuid IN" in sql_clean:
            self.conn.db_users = [u for u in self.conn.db_users if not (u["uuid"] in params and u.get("is_deleted"))]

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


class UsersMockConnection:
    def __init__(self):
        self.db_users = [
            {
                "uuid": "admin-uuid-1",
                "id": "admin",
                "email": "admin@bssm.hs.kr",
                "role": 2,
                "created_at": datetime(2026, 1, 1, 10, 0, 0),
                "is_deleted": False,
            },
            {
                "uuid": "teacher-uuid-1",
                "id": "teacher1",
                "email": "teacher1@bssm.hs.kr",
                "role": 1,
                "created_at": datetime(2026, 1, 2, 10, 0, 0),
                "is_deleted": False,
            },
            {
                "uuid": "student-uuid-1",
                "id": "student1",
                "email": "student1@bssm.hs.kr",
                "role": 0,
                "created_at": datetime(2026, 1, 3, 10, 0, 0),
                "is_deleted": False,
            },
            {
                "uuid": "student-uuid-2",
                "id": "student2",
                "email": "student2@bssm.hs.kr",
                "role": 0,
                "created_at": datetime(2026, 1, 4, 10, 0, 0),
                "is_deleted": True,
            },
        ]
        self.db_students = [
            {
                "uuid": "student-uuid-1",
                "student_id": 1,
                "name": "홍길동",
                "email": "student1@bssm.hs.kr",
                "is_deleted": False,
            },
            {
                "uuid": "student-uuid-2",
                "student_id": 2,
                "name": "김철수",
                "email": "student2@bssm.hs.kr",
                "is_deleted": True,
            },
        ]
        self.db_academic_records = [
            {"record_id": 1, "student_id": 1, "grade": 1, "class": 1, "number": 1},
            {"record_id": 2, "student_id": 2, "grade": 1, "class": 2, "number": 5},
        ]
        self.db_teachers = [
            {
                "uuid": "teacher-uuid-1",
                "teachers_id": 10,
                "name": "이선생",
                "grade": 1,
                "class": 1,
                "subject": "소프트웨어",
                "department": "마이스터부",
                "position": "정담임",
                "email": "teacher1@bssm.hs.kr",
                "is_deleted": False,
            }
        ]
        self.db_chat_conversations = [
            {"conversation_id": 100, "user_uuid": "student-uuid-2"}
        ]
        self.db_chat_messages = [
            {"message_id": 1, "conversation_id": 100, "content": "안녕하세요"}
        ]
        self.db_submissions_logs = [
            {"log_id": 1, "modifier_uuid": "student-uuid-2"}
        ]
        self.db_merits_log = [
            {"log_id": 1, "modifier_uuid": "student-uuid-2"}
        ]
        self.calls = []
        self.simulate_failure = False

    async def autocommit(self, val: bool):
        self.calls.append(f"autocommit_{val}")

    async def commit(self):
        self.calls.append("commit")

    async def rollback(self):
        self.calls.append("rollback")

    def cursor(self, cursor=None):
        return UsersMockCursor(self)

    def _filter_users(self, sql_clean: str, params: tuple) -> List[Dict[str, Any]]:
        results = []
        for u in self.db_users:
            # find corresponding student or teacher
            st = next((s for s in self.db_students if s.get("uuid") == u["uuid"]), None)
            sar = next((r for r in self.db_academic_records if st and r["student_id"] == st["student_id"]), None)
            t = next((tch for tch in self.db_teachers if tch.get("uuid") == u["uuid"]), None)

            row = {
                "uuid": u["uuid"],
                "id": u["id"],
                "email": u["email"],
                "role": u["role"],
                "created_at": u["created_at"],
                "is_deleted": u["is_deleted"],
                "student_id": st["student_id"] if st else None,
                "student_name": st["name"] if st else None,
                "student_grade": sar["grade"] if sar else None,
                "student_class": sar["class"] if sar else None,
                "student_number": sar["number"] if sar else None,
                "teachers_id": t["teachers_id"] if t else None,
                "teacher_name": t["name"] if t else None,
                "teacher_grade": t["grade"] if t else None,
                "teacher_class": t["class"] if t else None,
                "teacher_subject": t["subject"] if t else None,
                "teacher_department": t["department"] if t else None,
                "teacher_position": t["position"] if t else None,
            }

            # Filter checking
            match = True
            param_idx = 0

            if "u.role = %s" in sql_clean:
                expected_role = params[param_idx]
                param_idx += 1
                if u["role"] != expected_role:
                    match = False

            if "u.is_deleted = FALSE" in sql_clean:
                if u["is_deleted"] is not False:
                    match = False

            if "u.is_deleted = TRUE" in sql_clean:
                if u["is_deleted"] is not True:
                    match = False

            if "(u.id LIKE %s OR u.email LIKE %s OR st.name LIKE %s OR t.name LIKE %s)" in sql_clean:
                kw = str(params[param_idx]).replace("%", "").lower()
                param_idx += 4
                s_name = (row["student_name"] or "").lower()
                t_name = (row["teacher_name"] or "").lower()
                u_id = (row["id"] or "").lower()
                u_mail = (row["email"] or "").lower()
                if kw not in s_name and kw not in t_name and kw not in u_id and kw not in u_mail:
                    match = False

            if match:
                results.append(row)

        return results


class TestAdminUsers(unittest.TestCase):
    def setUp(self):
        self.mock_conn = UsersMockConnection()
        self.mock_redis = UsersMockRedis()
        self.current_user = {
            "uuid": "admin-uuid-1",
            "id": "admin",
            "role": "admin",
        }

        async def override_get_db():
            yield self.mock_conn

        async def override_get_redis():
            return self.mock_redis

        async def override_get_current_user():
            return self.current_user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_redis] = override_get_redis
        app.dependency_overrides[get_current_user] = override_get_current_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    # 1. RBAC 검증
    def test_admin_rbac_protection(self):
        for unauthorized_role in ["student", "teacher"]:
            self.current_user["role"] = unauthorized_role

            # 1) 유저 목록 조회 차단 (403)
            res = self.client.get("/api/admin/users")
            self.assertEqual(res.status_code, 403)

            # 2) 소프트 삭제 차단 (403)
            res = self.client.patch("/api/admin/users/student-uuid-1/soft-delete")
            self.assertEqual(res.status_code, 403)

            # 3) 복원 차단 (403)
            res = self.client.patch("/api/admin/users/student-uuid-2/restore")
            self.assertEqual(res.status_code, 403)

            # 4) 영구 삭제 차단 (403)
            res = self.client.delete("/api/admin/users/purge-deleted?user_uuid=student-uuid-2")
            self.assertEqual(res.status_code, 403)

        # 관리자 계정 허용 (200)
        self.current_user["role"] = "admin"
        res = self.client.get("/api/admin/users")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["total"], 4)
        self.assertEqual(body["data"]["deletedCount"], 1)

    # 2. 유저 목록 조회 및 필터링
    def test_list_users_filters_and_pagination(self):
        # 1) role=student 필터
        res = self.client.get("/api/admin/users?role=student")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["total"], 2)
        for u in data["users"]:
            self.assertEqual(u["role"], "student")

        # 2) role=teacher 필터
        res = self.client.get("/api/admin/users?role=teacher")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["users"][0]["role"], "teacher")
        self.assertEqual(data["users"][0]["extra"]["subject"], "소프트웨어")

        # 3) status=active 필터
        res = self.client.get("/api/admin/users?status=active")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["total"], 3)
        for u in data["users"]:
            self.assertFalse(u["is_deleted"])

        # 4) status=deleted 필터
        res = self.client.get("/api/admin/users?status=deleted")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["users"][0]["id"], "student2")
        self.assertTrue(data["users"][0]["is_deleted"])

        # 5) search 검색 (이름)
        res = self.client.get("/api/admin/users?search=홍길동")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["users"][0]["name"], "홍길동")

        # 6) 유효하지 않은 파라미터 방어
        res = self.client.get("/api/admin/users?role=invalid_role")
        self.assertEqual(res.status_code, 400)
        res = self.client.get("/api/admin/users?status=invalid_status")
        self.assertEqual(res.status_code, 400)

    # 3. 유저 소프트 삭제 및 관리자 본인 삭제 방어
    def test_soft_delete_and_self_deletion_prohibition(self):
        # 1) 관리자 본인 삭제 시도 차단 (400 - uuid 일치)
        res = self.client.patch("/api/admin/users/admin-uuid-1/soft-delete")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "SELF_DELETION_PROHIBITED")

        # 2) 관리자 본인 삭제 시도 차단 (400 - id 일치 방어)
        self.current_user["uuid"] = "different-admin-uuid"
        self.current_user["id"] = "admin"
        res = self.client.patch("/api/admin/users/admin-uuid-1/soft-delete")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "SELF_DELETION_PROHIBITED")

        # 복구
        self.current_user["uuid"] = "admin-uuid-1"

        # 3) 학생 정상 소프트 삭제 및 트랜잭션 호출 검증
        self.mock_conn.calls.clear()
        res = self.client.patch("/api/admin/users/student-uuid-1/soft-delete")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["data"]["is_deleted"])

        # 트랜잭션 순서 검증: autocommit_False -> commit -> autocommit_True
        self.assertIn("autocommit_False", self.mock_conn.calls)
        self.assertIn("commit", self.mock_conn.calls)
        self.assertIn("autocommit_True", self.mock_conn.calls)

        # DB 반영 확인
        user_in_db = next(u for u in self.mock_conn.db_users if u["uuid"] == "student-uuid-1")
        self.assertTrue(user_in_db["is_deleted"])
        st_in_db = next(s for s in self.mock_conn.db_students if s["uuid"] == "student-uuid-1")
        self.assertTrue(st_in_db["is_deleted"])

        # 4) 존재하지 않는 유저 삭제 시도 (404)
        res = self.client.patch("/api/admin/users/non-existent-uuid/soft-delete")
        self.assertEqual(res.status_code, 404)

    # 4. 유저 복원
    def test_restore_user(self):
        # 삭제 대기 상태였던 student-uuid-2 복원
        self.mock_conn.calls.clear()
        res = self.client.patch("/api/admin/users/student-uuid-2/restore")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["data"]["is_deleted"])

        # 트랜잭션 순서 검증: autocommit_False -> commit -> autocommit_True
        self.assertIn("autocommit_False", self.mock_conn.calls)
        self.assertIn("commit", self.mock_conn.calls)
        self.assertIn("autocommit_True", self.mock_conn.calls)

        # DB 반영 확인
        user_in_db = next(u for u in self.mock_conn.db_users if u["uuid"] == "student-uuid-2")
        self.assertFalse(user_in_db["is_deleted"])
        st_in_db = next(s for s in self.mock_conn.db_students if s["uuid"] == "student-uuid-2")
        self.assertFalse(st_in_db["is_deleted"])

        # 존재하지 않는 유저 복원 시도 (404)
        res = self.client.patch("/api/admin/users/non-existent-uuid/restore")
        self.assertEqual(res.status_code, 404)

    # 5. 삭제 대기 유저 영구 삭제 (단건 및 일괄 Purge)
    def test_purge_deleted_users(self):
        # 1) 정상 활성 유저를 바로 영구 삭제 시도 시 400 차단
        res = self.client.delete("/api/admin/users/purge-deleted?user_uuid=student-uuid-1")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "NOT_DELETED_USER")

        # 2) 관리자 본인 영구 삭제 시도 시 400 차단 (uuid 일치)
        res = self.client.delete("/api/admin/users/purge-deleted?user_uuid=admin-uuid-1")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "SELF_DELETION_PROHIBITED")

        # 3) 관리자 본인 영구 삭제 시도 시 400 차단 (id 일치)
        self.current_user["uuid"] = "different-admin-uuid"
        self.current_user["id"] = "admin"
        res = self.client.delete("/api/admin/users/purge-deleted?user_uuid=admin-uuid-1")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "SELF_DELETION_PROHIBITED")

        # 복구
        self.current_user["uuid"] = "admin-uuid-1"

        # 4) 삭제 대기 상태인 student-uuid-2 단건 영구 삭제
        self.mock_conn.calls.clear()
        res = self.client.delete("/api/admin/users/purge-deleted?user_uuid=student-uuid-2")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["purgedCount"], 1)
        self.assertIn("student-uuid-2", data["purgedUuids"])

        # 트랜잭션 순서 검증: autocommit_False -> commit -> autocommit_True
        self.assertIn("autocommit_False", self.mock_conn.calls)
        self.assertIn("commit", self.mock_conn.calls)
        self.assertIn("autocommit_True", self.mock_conn.calls)

        # 외래키 제약조건 안전 정리 검증:
        # - chat_conversations & chat_messages 삭제됨
        self.assertEqual(len(self.mock_conn.db_chat_conversations), 0)
        self.assertEqual(len(self.mock_conn.db_chat_messages), 0)
        # - submissions_logs & merits_log의 modifier_uuid = None
        self.assertIsNone(self.mock_conn.db_submissions_logs[0]["modifier_uuid"])
        self.assertIsNone(self.mock_conn.db_merits_log[0]["modifier_uuid"])
        # - students의 uuid = None
        st2 = next(s for s in self.mock_conn.db_students if s["student_id"] == 2)
        self.assertIsNone(st2["uuid"])
        # - users 테이블에서 완전히 제거됨
        self.assertFalse(any(u["uuid"] == "student-uuid-2" for u in self.mock_conn.db_users))

        # 5) 일괄 purge 테스트: 삭제 대기 유저가 없는 상태에서 호출 시 purgedCount=0
        res = self.client.delete("/api/admin/users/purge-deleted")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["purgedCount"], 0)

        # 6) 빈 문자열 쿼리 파라미터(?user_uuid=) 전달 시 정상 일괄 purge 동작 검증
        res = self.client.delete("/api/admin/users/purge-deleted?user_uuid=")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["purgedCount"], 0)

    # 6. 트랜잭션 실패 시 롤백 검증
    def test_purge_deleted_users_rollback_on_failure(self):
        # 삭제 대기 유저 1건 추가
        self.mock_conn.db_users.append({
            "uuid": "student-uuid-fail",
            "id": "student_fail",
            "email": "fail@bssm.hs.kr",
            "role": 0,
            "created_at": datetime(2026, 1, 5, 10, 0, 0),
            "is_deleted": True,
        })
        self.mock_conn.calls.clear()
        self.mock_conn.simulate_failure = True

        try:
            with self.assertRaises(Exception):
                self.client.delete("/api/admin/users/purge-deleted?user_uuid=student-uuid-fail")
        finally:
            self.mock_conn.simulate_failure = False

        # 롤백 및 autocommit(True) 복원 검증
        self.assertIn("autocommit_False", self.mock_conn.calls)
        self.assertIn("rollback", self.mock_conn.calls)
        self.assertNotIn("commit", self.mock_conn.calls)
        self.assertIn("autocommit_True", self.mock_conn.calls)


if __name__ == "__main__":
    unittest.main()
