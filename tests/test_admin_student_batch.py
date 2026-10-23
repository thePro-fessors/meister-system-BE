"""
tests/test_admin_student_batch.py - 관리자 학생 명단 일괄 등록(4.6) 및 NEIS 연동 단위/통합 테스트
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


class BatchMockRedis:
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


class BatchMockCursor:
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
        elif "FROM teachers" in sql_clean:
            teachers = getattr(self.conn, "db_teachers", [])
            if "WHERE email IN" in sql_clean:
                self._current_result = [t for t in teachers if t.get("email") in params and not t.get("is_deleted")]
            elif "WHERE email = %s" in sql_clean:
                target_email = params[0]
                self._current_result = [t for t in teachers if t.get("email") == target_email and not t.get("is_deleted")]
            else:
                self._current_result = list(teachers)

        # students 조회
        elif "FROM students" in sql_clean and "SELECT" in sql_clean:
            if "WHERE email IN" in sql_clean:
                self._current_result = [s for s in self.conn.db_students if s.get("email") in params and not s.get("is_deleted")]
            elif "WHERE email = %s" in sql_clean:
                target_email = params[0]
                self._current_result = [s for s in self.conn.db_students if s.get("email") == target_email and not s.get("is_deleted")]
            else:
                self._current_result = list(self.conn.db_students)

        # students 삽입
        elif "INSERT INTO students" in sql_clean:
            new_id = len(self.conn.db_students) + 1
            st_data = {
                "student_id": new_id,
                "name": params[0],
                "email": params[1],
                "status": 0,
                "is_deleted": False,
            }
            self.conn.db_students.append(st_data)
            self.lastrowid = new_id

        # student_academic_records 조회
        elif "SELECT record_id" in sql_clean and "FROM student_academic_records" in sql_clean:
            if "student_id IN" in sql_clean:
                yr_id = params[0]
                st_ids = set(params[1:])
                self._current_result = [
                    r for r in self.conn.db_records if r["year_id"] == yr_id and r["student_id"] in st_ids
                ]
            else:
                st_id, yr_id = params[0], params[1]
                self._current_result = [
                    r for r in self.conn.db_records if r["student_id"] == st_id and r["year_id"] == yr_id
                ]

        # student_academic_records 삽입
        elif "INSERT INTO student_academic_records" in sql_clean:
            new_id = len(self.conn.db_records) + 1
            self.conn.db_records.append({
                "record_id": new_id,
                "student_id": params[0],
                "year_id": params[1],
                "grade": params[2],
                "class": params[3],
                "number": params[4],
            })
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


class BatchMockConnection:
    def __init__(self):
        self.db_years = [
            {"year_id": 1, "year": 2026, "is_activated": True},
            {"year_id": 2, "year": 2025, "is_activated": False},
        ]
        self.db_students = []
        self.db_records = []

    def cursor(self, cursor=None):
        return BatchMockCursor(self)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def autocommit(self, val: bool):
        pass


class TestAdminStudentBatchAPI(unittest.TestCase):
    def setUp(self):
        _YEAR_ID_CACHE.clear()
        self.conn = BatchMockConnection()
        self.redis = BatchMockRedis()
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

    # 1. RBAC 인가 검증
    def test_batch_unauthenticated_blocked(self):
        self.current_user_mock = None
        res = self.client.post("/api/admin/students/batch")
        self.assertEqual(res.status_code, 401)

    def test_batch_student_role_forbidden(self):
        self.current_user_mock = {"uuid": "stu-uuid", "role": "student", "sub": "student"}
        res = self.client.post("/api/admin/students/batch")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    def test_batch_teacher_role_forbidden(self):
        self.current_user_mock = {"uuid": "tch-uuid", "role": "teacher", "sub": "teacher"}
        res = self.client.post("/api/admin/students/batch")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN")

    # 2. XLSX 파일 업로드 일괄 등록 성공 검증
    def test_batch_admin_xlsx_success(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["학번", "성명", "이메일"])
        ws.append(["1101", "홍길동", "gildong@bssm.hs.kr"])
        ws.append(["1205", "이순신", "sunshin@bssm.hs.kr"])

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        files = {"file": ("students.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/students/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 2)
        self.assertEqual(res_data["failedCount"], 0)
        self.assertEqual(res_data["createdStudentsCount"], 2)
        self.assertEqual(len(self.conn.db_students), 2)
        self.assertEqual(len(self.conn.db_records), 2)
        self.assertEqual(self.conn.db_students[0]["name"], "홍길동")
        self.assertEqual(self.conn.db_records[0]["grade"], 1)
        self.assertEqual(self.conn.db_records[0]["class"], 1)
        self.assertEqual(self.conn.db_records[0]["number"], 1)

    # 3. CSV UTF-8 및 CP949 다중 인코딩 지원 검증
    def test_batch_admin_csv_utf8_success(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = "학년,반,번호,성명,이메일\n2,3,10,강감찬,gamchan@bssm.hs.kr\n".encode("utf-8")
        files = {"file": ("students.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/students/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 1)
        self.assertEqual(self.conn.db_students[0]["name"], "강감찬")

    def test_batch_admin_csv_cp949_korean_encoding_success(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = "학년,반,번호,성명,이메일\n2,4,15,신사임당,saimdang@bssm.hs.kr\n".encode("cp949")
        files = {"file": ("students_cp949.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/students/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 1)
        self.assertEqual(self.conn.db_students[0]["name"], "신사임당")

    # 4. 이메일 누락 시 부산소프트웨어마이스터고 기본 규격 자동 생성 검증
    def test_batch_default_email_fallback(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = "학년,반,번호,성명\n1,1,3,유관순\n".encode("utf-8")
        files = {"file": ("students.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false", "default_email_domain": "bssm.hs.kr"}

        res = self.client.post("/api/admin/students/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.conn.db_students[0]["email"], "s10103@bssm.hs.kr")

    # 5. 수식 인젝션 (Formula Injection) 방어 검증
    def test_batch_formula_injection_sanitization(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = "학년,반,번호,성명\n1,2,5,=CMD('calc.exe')\n".encode("utf-8")
        files = {"file": ("injection.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/students/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        # 등호(=) 기호가 살균되었는지 검증
        self.assertFalse(self.conn.db_students[0]["name"].startswith("="))
        self.assertTrue("calc.exe" in self.conn.db_students[0]["name"])

    # 6. 행별 유효성 검증 실패 리포트 (부분 성공 허용) 검증
    def test_batch_validation_errors_partial_success(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        csv_content = (
            "학년,반,번호,성명\n"
            "1,1,1,정상학생\n"
            "4,1,2,초과학년\n"  # 4학년 (고교 학제 오류)
            "1,1,3,\n"         # 이름 누락
        ).encode("utf-8")

        files = {"file": ("partial.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/students/batch", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 1)
        self.assertEqual(res_data["failedCount"], 2)
        self.assertEqual(len(res_data["errors"]), 2)

    # 7. JSON Body 직접 일괄 등록 지원 검증
    def test_batch_json_body_success(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}

        json_payload = {
            "year": 2026,
            "validateWithNeis": False,
            "students": [
                {"name": "김철수", "grade": 1, "classNo": 2, "studentNo": 11, "email": "chulsoo@bssm.hs.kr"},
                {"name": "이영희", "studentNumber": "1212"},
            ],
        }

        res = self.client.post("/api/admin/students/batch", json=json_payload)
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["successCount"], 2)
        self.assertEqual(len(self.conn.db_students), 2)
        self.assertEqual(self.conn.db_students[1]["email"], "s10212@bssm.hs.kr")

    # 8. NEIS 법적/기술적 근거 및 학교 정보 안내 엔드포인트 검증
    def test_neis_info_endpoint(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}
        res = self.client.get("/api/admin/neis/info")
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["schoolName"], "부산소프트웨어마이스터고등학교")
        self.assertEqual(res_data["officeCode"], "C10")
        self.assertFalse(res_data["supported"])
        self.assertTrue(len(res_data["legalAndTechnicalBasis"]) >= 3)

    # 9. NEIS 학급 조회 및 개설 학급 수 검증
    def test_neis_classes_endpoint(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}
        res = self.client.get("/api/admin/neis/classes?year=2026")
        self.assertEqual(res.status_code, 200)
        res_data = res.json()["data"]
        self.assertEqual(res_data["schoolName"], "부산소프트웨어마이스터고등학교")
        self.assertIn("classes", res_data)

    # 10. NEIS 표준 양식 전용 엔드포인트 별칭 검증
    def test_neis_batch_alias_endpoint(self):
        self.current_user_mock = {"uuid": "admin-uuid", "role": "admin", "sub": "admin"}
        csv_content = "학번,성명\n1101,네이스학생\n".encode("utf-8")
        files = {"file": ("neis_export.csv", csv_content, "text/csv")}
        data = {"year": 2026, "validate_with_neis": "false"}

        res = self.client.post("/api/admin/students/batch/neis", files=files, data=data)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["successCount"], 1)

    # 11. SSE 실시간 진행률 스트림 Query 파라미터(?token=...) 인증 검증
    def test_stream_batch_progress_query_token_auth(self):
        from core.security import create_access_token, get_current_user_with_query_token
        from core.batch_manager import batch_task_manager

        # 임시 관리자 토큰 생성
        admin_token = create_access_token(data={"sub": "admin-uuid", "role": "admin", "id": "admin"})

        # 토큰 없이 호출 시 401 검증 (SSE 401 방지 제어 확인)
        res_no_token = self.client.get("/api/admin/batch/tasks/test-task-1/progress")
        self.assertEqual(res_no_token.status_code, 401)
        self.assertIn("인증 토큰이 누락", res_no_token.json()["error"]["message"])

        # query parameter (?token=...) 로 호출 시 인증 성공 (401 없이 스트림 진입)
        task_id = batch_task_manager.create_task("TEST_TASK", total_count=1)
        batch_task_manager.complete_task(task_id, 1, 0, 0)
        res_with_token = self.client.get(f"/api/admin/batch/tasks/{task_id}/progress?token={admin_token}")
        self.assertEqual(res_with_token.status_code, 200)
        self.assertIn("text/event-stream", res_with_token.headers.get("content-type", ""))
        self.assertIn("COMPLETED", res_with_token.text)

        # Authorization Bearer 헤더로 호출 시에도 인증 성공 검증
        task_id2 = batch_task_manager.create_task("TEST_TASK_2", total_count=1)
        batch_task_manager.complete_task(task_id2, 1, 0, 0)
        res_bearer = self.client.get(
            f"/api/admin/batch/tasks/{task_id2}/progress",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(res_bearer.status_code, 200)
        self.assertIn("text/event-stream", res_bearer.headers.get("content-type", ""))

    # 12. 리포트 다운로드 및 상태 조회 Query 파라미터(?token=...) 인증 및 공백 허용 검증
    def test_batch_report_download_and_status_query_token_auth(self):
        from core.security import create_access_token
        from core.batch_manager import batch_task_manager

        admin_token = create_access_token(data={"sub": "admin-uuid", "role": "admin", "id": "admin"})
        student_token = create_access_token(data={"sub": "student-uuid", "role": "student", "id": "student1"})

        task_id = batch_task_manager.create_task("TEST_REPORT_TASK", total_count=2)
        batch_task_manager.complete_task(
            task_id, 1, 0, 1, parsing_errors=[{"row": 2, "name": "김철수", "reason": "오류 사유 테스트"}]
        )

        # 1) 토큰 누락 시 401
        res_no_tok = self.client.get(f"/api/admin/batch/tasks/{task_id}/report/download")
        self.assertEqual(res_no_tok.status_code, 401)

        # 2) 학생 토큰 시 403
        res_stud = self.client.get(f"/api/admin/batch/tasks/{task_id}/report/download?token={student_token}")
        self.assertEqual(res_stud.status_code, 403)

        # 3) 쿼리 토큰으로 CSV 리포트 다운로드 성공 (200 OK & text/csv)
        res_csv = self.client.get(f"/api/admin/batch/tasks/{task_id}/report/download?token={admin_token}")
        self.assertEqual(res_csv.status_code, 200)
        self.assertIn("text/csv", res_csv.headers.get("content-type", ""))
        self.assertIn("attachment; filename=", res_csv.headers.get("content-disposition", ""))
        self.assertIn("오류 사유 테스트", res_csv.text)

        # 4) 앞뒤 공백 포함 토큰도 정상 통과 검증
        res_ws = self.client.get(f"/api/admin/batch/tasks/{task_id}/report/download?token=%20{admin_token}%20")
        self.assertEqual(res_ws.status_code, 200)

        # 5) 상태 단건 조회 쿼리 토큰 성공 검증
        res_status = self.client.get(f"/api/admin/batch/tasks/{task_id}?token={admin_token}")
        self.assertEqual(res_status.status_code, 200)
        self.assertEqual(res_status.json()["data"]["taskId"], task_id)


if __name__ == "__main__":
    unittest.main()

