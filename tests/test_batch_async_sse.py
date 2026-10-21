"""
tests/test_batch_async_sse.py - 엑셀 대량 등록 비동기 처리 및 SSE 진행률 스트리밍 검증 스위트

검증 항목:
1. 관리자 POST /api/admin/students/batch/async 호출 시 202 Accepted 및 taskId 반환 검증
2. GET /api/admin/batch/tasks/{taskId}/progress SSE 스트림 헤더 및 JSON 데이터 포맷 검증
3. GET /api/admin/batch/tasks/{taskId} 작업 상태 단건 조회 검증
4. GET /api/admin/batch/tasks/{taskId}/report/download 실패 행 CSV 다운로드 검증
5. 학생/교사 접근 시 RBAC 403 Forbidden 인가 차단 검증
"""

import io
import unittest
from fastapi.testclient import TestClient

from core.batch_manager import batch_task_manager
from core.security import get_current_user
from database import get_db, get_redis
from main import app


class MockCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1
        self.lastrowid = 1
        self._results = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query: str, params=None):
        q = " ".join(query.split())
        if "FROM academic_years" in q:
            self._results = [{"year_id": 1, "year": 2026}]
        elif "SELECT student_id, uuid FROM students" in q:
            self._results = []
        elif "INSERT INTO users" in q:
            self.lastrowid = 101
        elif "INSERT INTO students" in q:
            self.lastrowid = 101
        elif "SELECT LAST_INSERT_ID()" in q:
            self._results = [{"last_id": 101}]
        else:
            self._results = []

    async def fetchone(self):
        return self._results[0] if self._results else None

    async def fetchall(self):
        return list(self._results)


class MockConnection:
    def cursor(self, cursor=None):
        return MockCursor(self)

    async def autocommit(self, val):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class TestBatchAsyncSSE(unittest.TestCase):
    def setUp(self):
        self.conn = MockConnection()
        self.current_user = {"uuid": "mock-admin-uuid", "role": "admin"}

        async def override_get_db():
            yield self.conn

        async def override_get_current_user():
            return self.current_user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = override_get_current_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_01_async_batch_rbac_forbidden(self):
        """학생 계정으로 비동기 배치 요청 시 403 Forbidden 차단 검증"""
        self.current_user = {"uuid": "mock-student-uuid", "role": "student"}
        csv_data = "학년,반,번호,이름,이메일\n1,1,1,홍길동,s10101@bssm.hs.kr\n"
        files = {"file": ("students.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
        res = self.client.post("/api/admin/students/batch/async", files=files)
        self.assertEqual(res.status_code, 403)

    def test_02_async_batch_creates_task_and_returns_202(self):
        """정상 엑셀 업로드 시 202 Accepted 및 taskId 반환 검증"""
        csv_data = "학년,반,번호,이름,이메일\n1,1,1,홍길동,s10101@bssm.hs.kr\n1,1,2,김철수,s10102@bssm.hs.kr\n"
        files = {"file": ("students.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
        res = self.client.post(
            "/api/admin/students/batch/async",
            files=files,
            data={"year": 2026, "validate_with_neis": "false"},
        )
        self.assertEqual(res.status_code, 202)
        data = res.json()["data"]
        self.assertIn("taskId", data)
        self.assertEqual(data["totalCount"], 2)
        self.assertTrue(data["taskId"].startswith("task_"))

    def test_03_get_task_status(self):
        """작업 단건 조회 API 검증"""
        task_id = batch_task_manager.create_task("students", 10, "test.xlsx")
        batch_task_manager.update_progress(task_id, processed_count=5, created_count=5)

        res = self.client.get(f"/api/admin/batch/tasks/{task_id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["taskId"], task_id)
        self.assertEqual(data["progress"], 50)
        self.assertEqual(data["processedCount"], 5)

    def test_04_download_csv_error_report(self):
        """실패 행 상세 리포트 CSV 다운로드 검증"""
        task_id = batch_task_manager.create_task("students", 2, "test.xlsx")
        batch_task_manager.complete_task(
            task_id,
            created_count=1,
            updated_count=0,
            skipped_count=0,
            parsing_errors=[{"row": 2, "name": "김철수", "reason": "NEIS 미인가 학급"}],
        )

        res = self.client.get(f"/api/admin/batch/tasks/{task_id}/report/download")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers["content-type"])
        content = res.text
        self.assertIn("행 번호,성명,실패 사유", content)
        self.assertIn("김철수", content)
        self.assertIn("NEIS 미인가 학급", content)
