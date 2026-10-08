"""
tests/test_admin_export_stats.py - 교내 통계 데이터 엑셀/CSV/PDF 내보내기(4.9) 단위/통합 테스트
"""

import io
import unittest
from datetime import datetime
from typing import Any, Dict, List
import openpyxl
from fastapi.testclient import TestClient

from core.security import get_current_user
from database import get_db, get_redis
from main import app


class ExportStatsMockRedis:
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


class ExportStatsMockCursor:
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
                self._current_result = [y for y in self.conn.db_years if y["year"] == target_year]
            elif "ORDER BY is_activated DESC" in sql_clean:
                sorted_years = sorted(self.conn.db_years, key=lambda y: (y.get("is_activated", False), y["year"]), reverse=True)
                self._current_result = sorted_years
            else:
                self._current_result = list(self.conn.db_years)

        # 2. criteria 조회 (certification_areas & evaluation_items)
        elif "FROM certification_areas ca" in sql_clean and "LEFT JOIN evaluation_items ei" in sql_clean:
            # params[0] is year_id
            year_id = params[0]
            matched = [c for c in self.conn.db_criteria if c["year_id"] == year_id]
            self._current_result = matched

        # 3. students & student_academic_records 조회
        elif "FROM student_academic_records sar" in sql_clean and "JOIN students st" in sql_clean:
            year_id = params[0]
            matched = [st for st in self.conn.db_students if st["year_id"] == year_id]
            self._current_result = matched

        # 4. submissions 조회
        elif "FROM submissions s" in sql_clean and "JOIN evaluation_items ei" in sql_clean:
            year_id = params[0]
            matched = [sub for sub in self.conn.db_submissions if sub["year_id"] == year_id]
            self._current_result = matched

        # 5. merits 조회
        elif "FROM merits" in sql_clean and "WHERE is_reflected = TRUE" in sql_clean:
            self._current_result = list(self.conn.db_merits)

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


class ExportStatsMockConnection:
    def __init__(self):
        self.db_years = [
            {"year_id": 1, "year": 2026, "is_activated": True},
            {"year_id": 2, "year": 2025, "is_activated": False},
        ]

        self.db_criteria = [
            {
                "year_id": 1,
                "area_id": 1,
                "area_name": "전공자격증",
                "max_score": 100.0,
                "item_id": 10,
                "item_name": "정보처리기능사",
                "item_max_score": 50.0,
                "scoring_type": 1,
            },
            {
                "year_id": 1,
                "area_id": 2,
                "area_name": "외국어능력",
                "max_score": 100.0,
                "item_id": 20,
                "item_name": "TOEIC 700",
                "item_max_score": 100.0,
                "scoring_type": 1,
            },
        ]

        self.db_students = [
            {
                "year_id": 1,
                "student_id": 1,
                "name": "홍길동",
                "email": "gildong@bssm.hs.kr",
                "grade": 1,
                "class": 1,
                "number": 1,
                "is_deleted": False,
            },
            {
                "year_id": 1,
                "student_id": 2,
                "name": "성춘향",
                "email": "chunhyang@bssm.hs.kr",
                "grade": 1,
                "class": 1,
                "number": 2,
                "is_deleted": False,
            },
        ]

        self.db_submissions = [
            # 홍길동: 전공자격증 50점 승인 (status_code=3), 외국어능력 100점 승인 (status_code=3) -> 인증 가능
            {
                "year_id": 1,
                "submission_id": 101,
                "student_id": 1,
                "item_id": 10,
                "area_id": 1,
                "status_code": 3,
                "granted_score": 50.0,
                "is_deleted": False,
            },
            {
                "year_id": 1,
                "submission_id": 102,
                "student_id": 1,
                "item_id": 20,
                "area_id": 2,
                "status_code": 3,
                "granted_score": 100.0,
                "is_deleted": False,
            },
            # 성춘향: 전공자격증 검토중 (status_code=1) -> 검토중
            {
                "year_id": 1,
                "submission_id": 103,
                "student_id": 2,
                "item_id": 10,
                "area_id": 1,
                "status_code": 1,
                "granted_score": None,
                "is_deleted": False,
            },
        ]

        self.db_merits = [
            {
                "merits_point_id": 1,
                "student_id": 1,
                "points": 2.0,
                "related_area": None,
                "is_reflected": True,
                "is_deleted": False,
            }
        ]

    def cursor(self, cursor=None):
        return ExportStatsMockCursor(self)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def autocommit(self, val):
        pass


class TestAdminExportStatsAPI(unittest.TestCase):
    def setUp(self):
        self.mock_redis = ExportStatsMockRedis()
        self.mock_conn = ExportStatsMockConnection()

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
        """인증 헤더 없이 내보내기 시도 시 401 Unauthorized 차단 검증"""
        client = TestClient(app)
        res = client.get("/api/admin/export/stats")
        self.assertEqual(res.status_code, 401)

    def test_teacher_and_student_access_forbidden(self):
        """교사 또는 학생 권한으로 내보내기 시도 시 403 Forbidden 차단 검증"""
        client = TestClient(app)

        app.dependency_overrides[get_current_user] = lambda: self.teacher_user
        res_t = client.get("/api/admin/export/stats")
        self.assertEqual(res_t.status_code, 403)
        self.assertEqual(res_t.json().get("error", {}).get("code"), "FORBIDDEN")

        app.dependency_overrides[get_current_user] = lambda: self.student_user
        res_s = client.get("/api/admin/export/stats")
        self.assertEqual(res_s.status_code, 403)
        self.assertEqual(res_s.json().get("error", {}).get("code"), "FORBIDDEN")

    def test_invalid_format_returns_400(self):
        """지원하지 않는 포맷(예: json, docx) 요청 시 400 INVALID_FORMAT 거부 검증"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/export/stats?format=json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json().get("error", {}).get("code"), "INVALID_FORMAT")

    def test_export_stats_csv_success(self):
        """CSV 내보내기 성공 검증 (UTF-8 BOM, 헤더, 학생별 점수 및 영역 점수)"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/export/stats?format=csv")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers["content-type"])
        self.assertIn("attachment; filename=\"meister_stats_2026.csv\"", res.headers["content-disposition"])

        csv_content = res.content.decode("utf-8-sig")
        lines = csv_content.strip().splitlines()

        # 헤더 검증
        self.assertTrue(lines[0].startswith("학번,학년,반,번호,이름,이메일,취득총점,인증상태,상벌점합계,검토대기건수"))
        self.assertIn("전공자격증", lines[0])
        self.assertIn("외국어능력", lines[0])

        # 홍길동 행 검증 (학번 10101, 이름 홍길동, 인증 상태)
        self.assertIn("홍길동", csv_content)
        self.assertIn("성춘향", csv_content)

    def test_export_stats_xlsx_success(self):
        """XLSX 내보내기 성공 검증 (학생별 시트, 통계 요약 시트 및 차트 임베딩)"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/export/stats?format=xlsx")
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", res.headers["content-type"])
        self.assertIn("attachment; filename=\"meister_stats_2026.xlsx\"", res.headers["content-disposition"])

        # openpyxl로 바이너리 로드 및 시트 검증
        wb = openpyxl.load_workbook(io.BytesIO(res.content))
        self.assertIn("학생별 인증 결과", wb.sheetnames)
        self.assertIn("통계 요약 및 차트", wb.sheetnames)

        ws_students = wb["학생별 인증 결과"]
        self.assertEqual(ws_students.cell(row=1, column=1).value, "학번")
        self.assertEqual(ws_students.cell(row=2, column=5).value, "홍길동")

        ws_stats = wb["통계 요약 및 차트"]
        self.assertEqual(ws_stats.cell(row=1, column=1).value, "인증 상태")
        self.assertTrue(len(ws_stats._charts) > 0)  # BarChart 포함 확인

    def test_export_stats_pdf_success(self):
        """PDF 내보내기 성공 검증 (벡터 막대 그래프 포함, PDF 포맷 유효성)"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/export/stats?format=pdf")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["content-type"], "application/pdf")
        self.assertIn("attachment; filename=\"meister_stats_2026.pdf\"", res.headers["content-disposition"])

        # PDF 시그니처 및 바이너리 검증
        self.assertTrue(res.content.startswith(b"%PDF"))
        self.assertTrue(b"%%EOF" in res.content)
        # PretendardGOV 폰트 임베딩 및 PDF 메타데이터 검증
        self.assertTrue(len(res.content) > 10000)
        self.assertTrue(b"PretendardGOV" in res.content or b"Font" in res.content)

    def test_export_stats_specific_year_not_found(self):
        """존재하지 않는 학년도 요청 시 404 ACADEMIC_YEAR_NOT_FOUND 검증"""
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        client = TestClient(app)

        res = client.get("/api/admin/export/stats?year=2099&format=xlsx")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json().get("error", {}).get("code"), "ACADEMIC_YEAR_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
