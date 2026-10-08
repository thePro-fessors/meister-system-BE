"""
tests/test_admin_teacher_batch.py - 관리자 교사 명단 일괄 등록(4.7) 및 NEIS 교원 연동 단위/통합 테스트
"""

import io
import unittest
from typing import Any, Dict, List
import openpyxl
from fastapi.testclient import TestClient

from core.academic import _YEAR_ID_CACHE
from core.security import get_current_user
from database import get_db, get_redis
from main import app


class TeacherBatchMockRedis:
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


class TeacherBatchMockCursor:
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
                self._current_result = [y for y in self.conn.db_years if y["year"] == target_year]
            elif "ORDER BY is_activated DESC" in sql_clean:
                sorted_years = sorted(self.conn.db_years, key=lambda y: (y.get("is_activated", False), y["year"]), reverse=True)
                self._current_result = sorted_years
            else:
                self._current_result = list(self.conn.db_years)

        # teachers 조회
        elif "SELECT teachers_id, name, subject, grade, class, is_deleted FROM teachers WHERE email = %s" in sql_clean:
            target_email = params[0]
            self._current_result = [t for t in self.conn.db_teachers if t["email"] == target_email]

        # teachers 갱신
        elif "UPDATE teachers SET name = %s, subject = %s, grade = %s, class = %s" in sql_clean:
            t_id = params[4]
            found = False
            for t in self.conn.db_teachers:
                if t["teachers_id"] == t_id:
                    t["name"] = params[0]
                    t["subject"] = params[1]
                    t["grade"] = params[2]
                    t["class"] = params[3]
                    found = True
            self.rowcount = 1 if found else 0

        # teachers 삽입
        elif "INSERT INTO teachers" in sql_clean:
            new_id = len(self.conn.db_teachers) + 1
            t_data = {
                "teachers_id": new_id,
                "name": params[0],
                "subject": params[1],
                "grade": params[2],
                "class": params[3],
                "email": params[4],
                "is_deleted": False,
            }
            self.conn.db_teachers.append(t_data)
            self.lastrowid = new_id

        # LAST_INSERT_ID
        elif "SELECT LAST_INSERT_ID()" in sql_clean:
            self._current_result = [{"last_id": self.lastrowid}]

        else:
            self._current_result = []

    async def fetchone(self):
        if self._fetch_index < len(self._current_result):
            row = self._current_result[self._fetch_index]
            self._fetch_index += 1
            return row
        return None

    async def fetchall(self):
        res = self._current_result[self._fetch_index :]
        self._fetch_index = len(self._current_result)
        return res


class TeacherBatchMockConnection:
    def __init__(self):
        self.db_years = [
            {"year_id": 1, "year": 2026, "is_activated": True},
            {"year_id": 2, "year": 2025, "is_activated": False},
        ]
        self.db_teachers = []

    def cursor(self, cursor=None):
        return TeacherBatchMockCursor(self)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def autocommit(self, val: bool):
        pass


class TestAdminTeacherBatchAPI(unittest.TestCase):
    def setUp(self):
        _YEAR_ID_CACHE.clear()
        self.conn = TeacherBatchMockConnection()
        self.redis = TeacherBatchMockRedis()
        self.current_user_mock = None

        app.dependency_overrides[get_db] = lambda: self.conn
        app.dependency_overrides[get_redis] = lambda: self.redis

        async def _override_get_current_user():
            if self.current_user_mock is None:
                from fastapi import HTTPException
                raise HTTPException(status_code=401, detail="Unauthorized")
            return self.current_user_mock

        app.dependency_overrides[get_current_user] = _override_get_current_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        _YEAR_ID_CACHE.clear()

    # 1. RBAC 인가 제어 검증
    def test_teacher_batch_unauthenticated_blocked(self):
        self.current_user_mock = None
        res = self.client.post("/api/admin/teachers/batch")
        self.assertEqual(res.status_code, 401)

    def test_teacher_batch_student_role_forbidden(self):
        self.current_user_mock = {"uuid": "stu-uuid", "role": "student", "sub": "student"}
        res = self.client.post("/api/admin/teachers/batch")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_teacher_batch_teacher_role_forbidden(self):
        self.current_user_mock = {"uuid": "tch-uuid", "role": "teacher", "sub": "teacher"}
        res = self.client.post("/api/admin/teachers/batch")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    # 2. XLSX 파일 업로드 일괄 등록 및 담임 배정 검증
    def test_teacher_batch_admin_xlsx_success(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["교사명", "이메일", "담당교과", "담당학급"])
        ws.append(["김담임", "kim@bssm.hs.kr", "소프트웨어", "1-2"])
        ws.append(["박교과", "park@bssm.hs.kr", "수학", "비담임"])

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        files = {"file": ("teachers.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/teachers/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 2)
        self.assertEqual(res_data["failedCount"], 0)
        self.assertEqual(res_data["createdTeachersCount"], 2)
        self.assertEqual(len(self.conn.db_teachers), 2)

        # 담임교사 배정 확인
        t1 = self.conn.db_teachers[0]
        self.assertEqual(t1["name"], "김담임")
        self.assertEqual(t1["grade"], 1)
        self.assertEqual(t1["class"], 2)
        self.assertEqual(t1["subject"], "소프트웨어")

        # 비담임 교과교사 확인
        t2 = self.conn.db_teachers[1]
        self.assertEqual(t2["name"], "박교과")
        self.assertIsNone(t2["grade"])
        self.assertIsNone(t2["class"])
        self.assertEqual(t2["subject"], "수학")

    # 3. CSV 멀티 인코딩(UTF-8, CP949) 및 한글 학급 표기 파싱 검증
    def test_teacher_batch_csv_utf8_success(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = "성명,이메일,담당교과,담당학년,담당반\n이선생,lee@bssm.hs.kr,데이터베이스,2,3\n".encode("utf-8")
        files = {"file": ("teachers.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/teachers/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 1)
        self.assertEqual(self.conn.db_teachers[0]["grade"], 2)
        self.assertEqual(self.conn.db_teachers[0]["class"], 3)

    def test_teacher_batch_csv_cp949_korean_encoding_success(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = "교사명,이메일,교과,담당학급\n최담임,choi@bssm.hs.kr,인공지능,3학년 4반\n".encode("cp949")
        files = {"file": ("teachers_cp949.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/teachers/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 1)
        self.assertEqual(self.conn.db_teachers[0]["grade"], 3)
        self.assertEqual(self.conn.db_teachers[0]["class"], 4)

    # 4. 수식 인젝션 (Formula Injection) 방어 검증
    def test_teacher_batch_formula_injection_sanitization(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = "성명,이메일,담당교과\n=CMD('calc.exe'),teacher_calc@bssm.hs.kr,+EVAL(1)\n".encode("utf-8")
        files = {"file": ("injection.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/teachers/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        t = self.conn.db_teachers[0]
        self.assertFalse(t["name"].startswith("="))
        self.assertFalse(t["subject"].startswith("+"))

    # 5. 동일 학급 담임 중복 배정 차단 검증
    def test_teacher_batch_duplicate_homeroom_blocked(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = (
            "성명,이메일,담당교과,담당학급\n"
            "교사A,a@bssm.hs.kr,수학,1-1\n"
            "교사B,b@bssm.hs.kr,영어,1-1\n"
        ).encode("utf-8")
        files = {"file": ("dup_homeroom.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/teachers/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 1)
        self.assertEqual(res_data["failedCount"], 1)
        self.assertTrue(any("중복" in err["reason"] for err in res_data["errors"]))

    # 6. NEIS 미인가 학급 담임 배정 차단 검증
    def test_teacher_batch_neis_unauthorized_class_blocked(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = "성명,이메일,담당학급\n황교사,hwang@bssm.hs.kr,1-15\n".encode("utf-8")
        files = {"file": ("invalid_class.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "true"}

        res = self.client.post("/api/admin/teachers/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 0)
        self.assertEqual(res_data["failedCount"], 1)
        self.assertTrue(any("NEIS" in err["reason"] or "학제" in err["reason"] for err in res_data["errors"]))

    # 7. 기존 등록 교사 정보 덮어쓰기(Overwrite) 검증
    def test_teacher_batch_overwrite_existing_teacher(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        # 미리 등록된 교사
        self.conn.db_teachers.append({
            "teachers_id": 10,
            "name": "원래이름",
            "subject": "도덕",
            "grade": 1,
            "class": 1,
            "email": "teacher10@bssm.hs.kr",
            "is_deleted": False,
        })

        json_payload = {
            "year": 2026,
            "overwrite": True,
            "validateWithNeis": False,
            "teachers": [
                {"name": "수정이름", "email": "teacher10@bssm.hs.kr", "subject": "윤리", "homeroom": "1-2"}
            ],
        }

        res = self.client.post("/api/admin/teachers/batch", json=json_payload)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["updatedTeachersCount"], 1)
        self.assertEqual(self.conn.db_teachers[0]["name"], "수정이름")
        self.assertEqual(self.conn.db_teachers[0]["subject"], "윤리")
        self.assertEqual(self.conn.db_teachers[0]["class"], 2)

    # 8. JSON Body 직접 일괄 등록 지원 검증
    def test_teacher_batch_json_body_success(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        json_payload = {
            "year": 2026,
            "validateWithNeis": False,
            "teachers": [
                {"name": "김영희", "email": "younghee@bssm.hs.kr", "subject": "물리", "grade": 2, "classNo": 1},
                {"name": "철수교사", "subject": "체육"},
            ],
        }

        res = self.client.post("/api/admin/teachers/batch", json=json_payload)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 2)
        self.assertEqual(len(self.conn.db_teachers), 2)
        self.assertTrue(self.conn.db_teachers[1]["email"].endswith("@bssm.hs.kr"))

    # 9. NEIS 교원 명단 법적/기술적 불가 사유 안내 엔드포인트 검증
    def test_neis_teacher_info_endpoint(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}
        res = self.client.get("/api/admin/neis/teacher-info")
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["schoolName"], "부산소프트웨어마이스터고등학교")
        self.assertFalse(res_data["supported"])
        self.assertTrue(len(res_data["legalAndTechnicalBasis"]) >= 3)


if __name__ == "__main__":
    unittest.main()
