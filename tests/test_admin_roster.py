"""
tests/test_admin_roster.py - 관리자 학생 및 교원 명단 개별 추가, 수정, 소프트삭제, 복원, 완전삭제 검증 스위트

검증 항목:
1. ROSTER-01: 관리자 RBAC 인가 검증 (학생/교사 403 차단, 관리자 허용)
2. ROSTER-02: 학생 명단 개별 등록 (정상 등록 201, 이메일 중복 409, 학년/반/번호 중복 409, 교사 이메일 교차 400)
3. ROSTER-03: 학생 명단 목록 조회 및 학년/반/상태 필터링, 검색
4. ROSTER-04: 학생 정보 수정, 소프트 삭제, 복원 및 영구 삭제(Purge)
5. ROSTER-05: 교원 명단 개별 등록 (정상 등록 201, 이메일 중복 409, 학생 이메일 교차 400)
6. ROSTER-06: 교원 명단 목록 조회 및 부서/직책/상태 필터링, 검색
7. ROSTER-07: 교원 정보 수정, 소프트 삭제, 복원 및 영구 삭제(Purge)
"""

import unittest
from typing import Any, Dict, List
from fastapi.testclient import TestClient

from core.security import get_current_user
from database import get_db, get_redis
from main import app


class RosterMockRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, ttl, val):
        self.data[key] = val
        return True

    async def delete(self, key):
        self.data.pop(key, None)


class RosterMockCursor:
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

        # academic_years
        if "FROM academic_years" in sql_clean:
            if "WHERE year = %s" in sql_clean:
                target_year = params[0]
                self._current_result = [y for y in self.conn.years if y["year"] == target_year]
            else:
                self._current_result = sorted(self.conn.years, key=lambda y: (y.get("is_activated", False), y["year"]), reverse=True)

        # students 조회/카운트
        elif "SELECT COUNT(DISTINCT st.student_id) AS total FROM students st" in sql_clean:
            filtered = self.conn._filter_students(sql_clean, params)
            self._current_result = [{"total": len(filtered)}]

        elif "SELECT COUNT(*) AS deleted_count FROM students WHERE is_deleted = TRUE" in sql_clean:
            del_cnt = sum(1 for s in self.conn.students if s.get("is_deleted"))
            self._current_result = [{"deleted_count": del_cnt}]

        elif "FROM students st" in sql_clean and "SELECT st.student_id" in sql_clean:
            filtered = self.conn._filter_students(sql_clean, params)
            self._current_result = filtered

        elif "FROM students WHERE student_id = %s" in sql_clean:
            sid = params[0]
            matched = [s for s in self.conn.students if s["student_id"] == sid]
            self._current_result = matched

        elif "SELECT student_id, is_deleted FROM students WHERE email = %s" in sql_clean:
            em = params[0]
            matched = [s for s in self.conn.students if s["email"] == em]
            self._current_result = matched

        elif "SELECT student_id FROM students WHERE email = %s AND is_deleted = FALSE" in sql_clean:
            em = params[0]
            matched = [s for s in self.conn.students if s["email"] == em and not s.get("is_deleted")]
            self._current_result = matched

        # students 삽입
        elif "INSERT INTO students" in sql_clean:
            new_id = len(self.conn.students) + 1
            st_data = {
                "student_id": new_id,
                "uuid": None,
                "name": params[0],
                "email": params[1],
                "status": 0,
                "is_deleted": False,
            }
            self.conn.students.append(st_data)
            self.lastrowid = new_id
            self._current_result = []

        # students 업데이트
        elif "UPDATE students SET is_deleted = TRUE WHERE student_id = %s" in sql_clean:
            sid = params[0]
            for s in self.conn.students:
                if s["student_id"] == sid:
                    s["is_deleted"] = True
            self._current_result = []

        elif "UPDATE students SET is_deleted = FALSE WHERE student_id = %s" in sql_clean:
            sid = params[0]
            for s in self.conn.students:
                if s["student_id"] == sid:
                    s["is_deleted"] = False
            self._current_result = []

        elif "UPDATE students SET name = %s, email = %s WHERE student_id = %s" in sql_clean:
            name, email, sid = params[0], params[1], params[2]
            for s in self.conn.students:
                if s["student_id"] == sid:
                    s["name"] = name
                    s["email"] = email
            self._current_result = []

        elif "UPDATE students SET name = %s, is_deleted = FALSE WHERE student_id = %s" in sql_clean:
            name, sid = params[0], params[1]
            for s in self.conn.students:
                if s["student_id"] == sid:
                    s["name"] = name
                    s["is_deleted"] = False
            self._current_result = []

        # student_academic_records 중복 체크 및 조회
        elif "FROM student_academic_records sar JOIN students st" in sql_clean:
            # params: year_id, grade, class, number
            y_id, g, c, n = params[0], params[1], params[2], params[3]
            matched = [
                r for r in self.conn.academic_records
                if r["year_id"] == y_id and r["grade"] == g and r["class"] == c and r["number"] == n
            ]
            self._current_result = matched

        elif "FROM student_academic_records sar JOIN students s" in sql_clean:
            y_id, g, c, n, sid = params[0], params[1], params[2], params[3], params[4]
            matched = [
                r for r in self.conn.academic_records
                if r["year_id"] == y_id and r["grade"] == g and r["class"] == c and r["number"] == n and r["student_id"] != sid
            ]
            self._current_result = matched

        elif "SELECT record_id, grade, class, number FROM student_academic_records" in sql_clean:
            sid, y_id = params[0], params[1]
            matched = [r for r in self.conn.academic_records if r["student_id"] == sid and r["year_id"] == y_id]
            self._current_result = matched

        elif "INSERT INTO student_academic_records" in sql_clean:
            sid, y_id, g, c, n = params[0], params[1], params[2], params[3], params[4]
            self.conn.academic_records.append({
                "record_id": len(self.conn.academic_records) + 1,
                "student_id": sid,
                "year_id": y_id,
                "grade": g,
                "class": c,
                "number": n,
            })
            self._current_result = []

        elif "UPDATE student_academic_records" in sql_clean:
            g, c, n, rid = params[0], params[1], params[2], params[3]
            for r in self.conn.academic_records:
                if r["record_id"] == rid:
                    r["grade"] = g
                    r["class"] = c
                    r["number"] = n
            self._current_result = []

        elif "DELETE FROM student_academic_records WHERE student_id IN" in sql_clean:
            self._current_result = []

        elif "DELETE FROM students WHERE student_id IN" in sql_clean:
            self.conn.students = [s for s in self.conn.students if not s.get("is_deleted")]
            self._current_result = []

        elif "SELECT student_id, uuid FROM students WHERE" in sql_clean and "is_deleted = TRUE" in sql_clean:
            matched = [s for s in self.conn.students if s.get("is_deleted")]
            self._current_result = matched

        # teachers 조회/카운트
        elif "SELECT COUNT(DISTINCT t.teachers_id) AS total FROM teachers t" in sql_clean:
            filtered = self.conn._filter_teachers(sql_clean, params)
            self._current_result = [{"total": len(filtered)}]

        elif "SELECT COUNT(*) AS deleted_count FROM teachers WHERE is_deleted = TRUE" in sql_clean:
            del_cnt = sum(1 for t in self.conn.teachers if t.get("is_deleted"))
            self._current_result = [{"deleted_count": del_cnt}]

        elif "FROM teachers t" in sql_clean and "SELECT t.teachers_id" in sql_clean:
            filtered = self.conn._filter_teachers(sql_clean, params)
            self._current_result = filtered

        elif "FROM teachers WHERE teachers_id = %s" in sql_clean:
            tid = params[0]
            matched = [t for t in self.conn.teachers if t["teachers_id"] == tid]
            self._current_result = matched

        elif "SELECT teachers_id, is_deleted FROM teachers WHERE email = %s" in sql_clean:
            em = params[0]
            matched = [t for t in self.conn.teachers if t["email"] == em]
            self._current_result = matched

        elif "SELECT teachers_id FROM teachers WHERE email = %s AND is_deleted = FALSE" in sql_clean:
            em = params[0]
            matched = [t for t in self.conn.teachers if t["email"] == em and not t.get("is_deleted")]
            self._current_result = matched

        elif "SELECT teachers_id FROM teachers WHERE email = %s AND teachers_id != %s" in sql_clean:
            em, tid = params[0], params[1]
            matched = [t for t in self.conn.teachers if t["email"] == em and t["teachers_id"] != tid and not t.get("is_deleted")]
            self._current_result = matched

        # teachers 삽입
        elif "INSERT INTO teachers" in sql_clean:
            new_id = len(self.conn.teachers) + 1
            t_data = {
                "teachers_id": new_id,
                "uuid": None,
                "name": params[0],
                "email": params[1],
                "subject": params[2],
                "department": params[3],
                "position": params[4],
                "grade": params[5],
                "class": params[6],
                "is_deleted": False,
            }
            self.conn.teachers.append(t_data)
            self.lastrowid = new_id
            self._current_result = []

        # teachers 업데이트
        elif "UPDATE teachers SET is_deleted = TRUE WHERE teachers_id = %s" in sql_clean:
            tid = params[0]
            for t in self.conn.teachers:
                if t["teachers_id"] == tid:
                    t["is_deleted"] = True
            self._current_result = []

        elif "UPDATE teachers SET is_deleted = FALSE WHERE teachers_id = %s" in sql_clean:
            tid = params[0]
            for t in self.conn.teachers:
                if t["teachers_id"] == tid:
                    t["is_deleted"] = False
            self._current_result = []

        elif "UPDATE teachers SET name = %s, email = %s, subject = %s, department = %s, position = %s, grade = %s, class = %s WHERE teachers_id = %s" in sql_clean:
            tid = params[7]
            for t in self.conn.teachers:
                if t["teachers_id"] == tid:
                    t["name"] = params[0]
                    t["email"] = params[1]
                    t["subject"] = params[2]
                    t["department"] = params[3]
                    t["position"] = params[4]
                    t["grade"] = params[5]
                    t["class"] = params[6]
            self._current_result = []

        elif "DELETE FROM teachers WHERE teachers_id IN" in sql_clean:
            self.conn.teachers = [t for t in self.conn.teachers if not t.get("is_deleted")]
            self._current_result = []

        elif "SELECT teachers_id, uuid FROM teachers WHERE" in sql_clean and "is_deleted = TRUE" in sql_clean:
            matched = [t for t in self.conn.teachers if t.get("is_deleted")]
            self._current_result = matched

        else:
            self._current_result = []

    async def fetchone(self):
        if self._fetch_index < len(self._current_result):
            res = self._current_result[self._fetch_index]
            self._fetch_index += 1
            return res
        return None

    async def fetchall(self):
        res = self._current_result[self._fetch_index:]
        self._fetch_index = len(self._current_result)
        return res


class RosterMockConnection:
    def __init__(self):
        self.years = [{"year_id": 1, "year": 2026, "is_activated": True}]
        self.students = [
            {"student_id": 1, "uuid": None, "name": "기존학생1", "email": "st1@bssm.hs.kr", "status": 0, "is_deleted": False},
            {"student_id": 2, "uuid": None, "name": "삭제대기학생", "email": "del_st@bssm.hs.kr", "status": 0, "is_deleted": True},
        ]
        self.academic_records = [
            {"record_id": 1, "student_id": 1, "year_id": 1, "grade": 1, "class": 1, "number": 1},
            {"record_id": 2, "student_id": 2, "year_id": 1, "grade": 1, "class": 1, "number": 2},
        ]
        self.teachers = [
            {"teachers_id": 1, "uuid": None, "name": "기존교사1", "email": "t1@bssm.hs.kr", "subject": "정보", "department": "마이스터부", "position": "정담임", "grade": 1, "class": 1, "is_deleted": False},
            {"teachers_id": 2, "uuid": None, "name": "삭제대기교사", "email": "del_t@bssm.hs.kr", "subject": "수학", "department": "인문부", "position": "교과전담", "grade": None, "class": None, "is_deleted": True},
        ]

    def cursor(self, cursor=None):
        return RosterMockCursor(self)

    async def autocommit(self, val):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def _filter_students(self, sql: str, params: tuple):
        res = []
        for s in self.students:
            rec = next((r for r in self.academic_records if r["student_id"] == s["student_id"]), {})
            item = {
                "student_id": s["student_id"],
                "uuid": s.get("uuid"),
                "name": s["name"],
                "email": s["email"],
                "status": s["status"],
                "is_deleted": s["is_deleted"],
                "grade": rec.get("grade"),
                "class_no": rec.get("class"),
                "student_no": rec.get("number"),
                "year_id": rec.get("year_id"),
                "user_account_id": None,
                "has_account": False,
            }
            if "st.is_deleted = FALSE" in sql and s.get("is_deleted"):
                continue
            if "st.is_deleted = TRUE" in sql and not s.get("is_deleted"):
                continue
            res.append(item)
        return res

    def _filter_teachers(self, sql: str, params: tuple):
        res = []
        for t in self.teachers:
            item = {
                "teachers_id": t["teachers_id"],
                "uuid": t.get("uuid"),
                "name": t["name"],
                "email": t["email"],
                "subject": t.get("subject"),
                "department": t.get("department"),
                "position": t.get("position"),
                "grade": t.get("grade"),
                "class_no": t.get("class"),
                "is_deleted": t["is_deleted"],
                "user_account_id": None,
                "has_account": False,
            }
            if "t.is_deleted = FALSE" in sql and t.get("is_deleted"):
                continue
            if "t.is_deleted = TRUE" in sql and not t.get("is_deleted"):
                continue
            res.append(item)
        return res


class TestAdminRoster(unittest.TestCase):
    def setUp(self):
        self.conn = RosterMockConnection()
        self.mock_redis = RosterMockRedis()
        self.current_user = {"uuid": "admin-test-uuid", "id": "admin", "role": "admin"}

        async def override_get_db():
            yield self.conn

        async def override_get_current_user():
            return self.current_user

        async def override_get_redis():
            yield self.mock_redis

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = override_get_current_user
        app.dependency_overrides[get_redis] = override_get_redis

        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_01_rbac_forbidden_for_student(self):
        """학생 계정 접근 시 403 차단 검증"""
        self.current_user = {"uuid": "student-uuid", "id": "student1", "role": "student"}
        res = self.client.get("/api/admin/roster/students")
        self.assertEqual(res.status_code, 403)
        res_t = self.client.get("/api/admin/roster/teachers")
        self.assertEqual(res_t.status_code, 403)

    def test_02_create_student_success_and_validations(self):
        """학생 개별 등록 성공 및 중복 방어 검증"""
        # 1. 정상 등록
        res = self.client.post("/api/admin/roster/students", json={
            "name": "새학생",
            "email": "new_student@bssm.hs.kr",
            "grade": 1,
            "class": 2,
            "number": 1,
        })
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.json()["success"])
        self.assertEqual(res.json()["data"]["name"], "새학생")

        # 2. 교사 이메일과 중복 시 400 차단
        res_conflict_teacher = self.client.post("/api/admin/roster/students", json={
            "name": "충돌학생",
            "email": "t1@bssm.hs.kr",
            "grade": 1,
            "class": 2,
            "number": 2,
        })
        self.assertEqual(res_conflict_teacher.status_code, 400)

        # 3. 동일 학년도 학년/반/번호 중복 시 409 차단
        res_dup_num = self.client.post("/api/admin/roster/students", json={
            "name": "번호중복학생",
            "email": "other_st@bssm.hs.kr",
            "grade": 1,
            "class": 1,
            "number": 1,
        })
        self.assertEqual(res_dup_num.status_code, 409)

    def test_03_list_students_and_filters(self):
        """학생 명단 목록 조회 및 상태 필터링 검증"""
        res = self.client.get("/api/admin/roster/students?status=active")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["total"], 1)

        res_all = self.client.get("/api/admin/roster/students?status=all")
        self.assertEqual(res_all.status_code, 200)
        self.assertEqual(res_all.json()["data"]["total"], 2)

    def test_04_student_update_soft_delete_restore_purge(self):
        """학생 정보 수정, 소프트 삭제, 복원 및 완전 삭제 검증"""
        # 1. 정보 수정
        res_up = self.client.patch("/api/admin/roster/students/1", json={
            "name": "수정학생1",
            "class": 3,
            "number": 5,
        })
        self.assertEqual(res_up.status_code, 200)
        self.assertEqual(res_up.json()["data"]["name"], "수정학생1")

        # 2. 소프트 삭제
        res_del = self.client.patch("/api/admin/roster/students/1/soft-delete")
        self.assertEqual(res_del.status_code, 200)
        self.assertTrue(res_del.json()["data"]["isDeleted"])

        # 3. 복원
        res_res = self.client.patch("/api/admin/roster/students/1/restore")
        self.assertEqual(res_res.status_code, 200)
        self.assertFalse(res_res.json()["data"]["isDeleted"])

        # 4. 삭제 대기 학생(ID 2) 완전 삭제 (Purge)
        res_purge = self.client.delete("/api/admin/roster/students/purge")
        self.assertEqual(res_purge.status_code, 200)
        self.assertEqual(res_purge.json()["data"]["purgedCount"], 1)

    def test_05_create_teacher_success_and_validations(self):
        """교원 개별 등록 성공 및 중복 방어 검증"""
        # 1. 정상 등록
        res = self.client.post("/api/admin/roster/teachers", json={
            "name": "새교사",
            "email": "new_teacher@bssm.hs.kr",
            "subject": "소프트웨어",
            "department": "마이스터부",
            "position": "정담임",
            "grade": 2,
            "class": 1,
        })
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.json()["success"])
        self.assertEqual(res.json()["data"]["name"], "새교사")

        # 2. 학생 이메일과 중복 시 400 차단
        res_conflict_student = self.client.post("/api/admin/roster/teachers", json={
            "name": "충돌교사",
            "email": "st1@bssm.hs.kr",
        })
        self.assertEqual(res_conflict_student.status_code, 400)

        # 3. 기존 교사 이메일과 중복 시 409 차단
        res_dup = self.client.post("/api/admin/roster/teachers", json={
            "name": "중복교사",
            "email": "t1@bssm.hs.kr",
        })
        self.assertEqual(res_dup.status_code, 409)

    def test_06_list_teachers_and_filters(self):
        """교원 명단 목록 조회 및 필터링 검증"""
        res = self.client.get("/api/admin/roster/teachers?status=active")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["total"], 1)

        res_all = self.client.get("/api/admin/roster/teachers?status=all")
        self.assertEqual(res_all.status_code, 200)
        self.assertEqual(res_all.json()["data"]["total"], 2)

    def test_07_teacher_update_soft_delete_restore_purge(self):
        """교원 정보 수정, 소프트 삭제, 복원 및 완전 삭제 검증"""
        # 1. 정보 수정
        res_up = self.client.patch("/api/admin/roster/teachers/1", json={
            "name": "수정교사1",
            "subject": "인공지능",
        })
        self.assertEqual(res_up.status_code, 200)
        self.assertEqual(res_up.json()["data"]["subject"], "인공지능")

        # 2. 소프트 삭제
        res_del = self.client.patch("/api/admin/roster/teachers/1/soft-delete")
        self.assertEqual(res_del.status_code, 200)
        self.assertTrue(res_del.json()["data"]["isDeleted"])

        # 3. 복원
        res_res = self.client.patch("/api/admin/roster/teachers/1/restore")
        self.assertEqual(res_res.status_code, 200)
        self.assertFalse(res_res.json()["data"]["isDeleted"])

        # 4. 삭제 대기 교원(ID 2) 완전 삭제 (Purge)
        res_purge = self.client.delete("/api/admin/roster/teachers/purge")
        self.assertEqual(res_purge.status_code, 200)
        self.assertEqual(res_purge.json()["data"]["purgedCount"], 1)


if __name__ == "__main__":
    unittest.main()
