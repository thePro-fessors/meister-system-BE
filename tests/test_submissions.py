"""
tests/test_submissions.py - 증빙자료 신규 제출 API 통합 및 엔드포인트 테스트

검증 대상:
1. POST /api/students/{studentId}/submissions
2. POST /api/submissions
3. 파일 업로드 + 메타데이터 수신 및 저장
4. snake_case / camelCase 필드명 호환성
5. RBAC 및 본인 소유권 검증 (403 Forbidden)
6. 평가 항목 존재/활성/학년 매칭 검증 (400/404)
7. 증빙자료 필수(requires_evidence) 검증 (400)
8. 중복 제출 방어 (409 DUPLICATE_SUBMISSION)
9. 트랜잭션 롤백 및 업로드 파일 안전 삭제
"""

import io
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from core.security import create_access_token, create_signed_download_token, get_current_user
from database import get_db, get_redis
from main import app


class MockRedis:
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


class MockCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = 0
        self.rowcount = 0
        self._current_result = []
        self._fetch_index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql: str, params: tuple = ()):
        sql_clean = " ".join(sql.strip().split())
        self._fetch_index = 0

        # 1. 학생 조회 (student_id 또는 uuid)
        if "FROM students" in sql_clean:
            if "WHERE student_id = %s" in sql_clean:
                target_id = params[0]
                matched = [s for s in self.conn.db_students if s["student_id"] == target_id and not s["is_deleted"]]
                self._current_result = matched
            elif "WHERE uuid = %s" in sql_clean:
                target_uuid = params[0]
                matched = [s for s in self.conn.db_students if s["uuid"] == target_uuid and not s["is_deleted"]]
                self._current_result = matched

        # 2. 교사 조회
        elif "FROM teachers" in sql_clean:
            self._current_result = getattr(self.conn, "db_teachers", [])

        # 3. 평가 항목 및 영역/학년도 조회
        elif "FROM evaluation_items ei" in sql_clean:
            item_id = params[0]
            matched = [item for item in self.conn.db_items if item["item_id"] == item_id]
            self._current_result = matched

        # 3. 학적 정보 조회 (student_academic_records)
        elif "FROM student_academic_records" in sql_clean:
            st_id, yr_id = params[0], params[1]
            matched = [
                r for r in self.conn.db_academic_records
                if r["student_id"] == st_id and r["year_id"] == yr_id
            ]
            self._current_result = matched

        # 4. 중복 제출 조회 (submissions)
        elif "SELECT submission_id" in sql_clean and "FROM submissions" in sql_clean:
            st_id, it_id, det, dt = params[0], params[1], params[2], params[3]
            matched = [
                {"submission_id": sub["submission_id"], "status_code": sub["status_code"]}
                for sub in self.conn.db_submissions
                if sub["student_id"] == st_id
                and sub["item_id"] == it_id
                and sub["detail"] == det
                and str(sub["activity_date"]) == str(dt)
                and not sub["is_deleted"]
                and sub["status_code"] in (1, 2, 3)
            ]
            self._current_result = matched


        # 5. 심사 로그 삽입 (INSERT INTO submissions_logs)
        elif "INSERT INTO submissions_logs" in sql_clean:
            self.conn.log_seq += 1
            self.lastrowid = self.conn.log_seq
            new_log = {
                "log_id": self.lastrowid,
                "submission_id": params[0],
                "modifier_uuid": params[1],
                "action_type": "SUBMIT",
                "new_status_code": 1,
                "comment": "증빙자료 최초 제출",
                "created_at": params[2],
            }
            self.conn.db_submissions_logs.append(new_log)

        # 6. 증빙자료 삽입 (INSERT INTO submissions)
        elif "INSERT INTO submissions (" in sql_clean or "INSERT INTO submissions\n" in sql_clean or "INSERT INTO submissions " in sql_clean:
            if self.conn.simulate_insert_error:
                raise RuntimeError("Simulated Database Failure during INSERT submissions")

            self.conn.sub_seq += 1
            self.lastrowid = self.conn.sub_seq
            new_sub = {
                "submission_id": self.lastrowid,
                "student_id": params[0],
                "item_id": params[1],
                "detail": params[2],
                "activity_date": params[3],
                "file_path": params[4],
                "original_filename": params[5],
                "link_url": params[6],
                "description": params[7],
                "status_code": 1,
                "created_at": params[8],
                "is_deleted": False,
            }
            self.conn.db_submissions.append(new_sub)

        elif "SELECT LAST_INSERT_ID()" in sql_clean:
            self._current_result = [{"last_id": self.lastrowid}]

        # 7. 파일 소유권 검증 조회 (submissions JOIN students)
        elif "FROM submissions s" in sql_clean and "JOIN students st" in sql_clean:
            target_path = params[0]
            matched = []
            for sub in self.conn.db_submissions:
                if sub.get("file_path") == target_path and not sub.get("is_deleted"):
                    student = next((s for s in self.conn.db_students if s["student_id"] == sub["student_id"]), None)
                    if student:
                        matched.append({
                            "submission_id": sub["submission_id"],
                            "student_id": sub["student_id"],
                            "uuid": student["uuid"],
                        })
            self._current_result = matched

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


class MockConnection:
    def __init__(self):
        self.sub_seq = 100
        self.log_seq = 200
        self.simulate_insert_error = False
        self.committed = False
        self.rolled_back = False

        self.db_students = [
            {
                "student_id": 1,
                "uuid": "student-uuid-1",
                "name": "홍길동",
                "email": "student1@bssm.hs.kr",
                "is_deleted": False,
            },
            {
                "student_id": 2,
                "uuid": "student-uuid-2",
                "name": "이순신",
                "email": "student2@bssm.hs.kr",
                "is_deleted": False,
            },
        ]

        self.db_teachers = [
            {"teachers_id": 1, "uuid": "teacher-uuid-1", "grade": None, "class": None, "is_deleted": False},
        ]

        self.db_items = [
            {
                "item_id": 10,
                "area_id": 101,
                "item_name": "정보처리기능사 취득",
                "target_grade": 1,
                "item_max_score": 50.0,
                "scoring_type": 1,
                "requires_evidence": True,
                "is_active": True,
                "year_id": 1,
                "area_grade": 1,
                "area_name": "전공자격증",
                "academic_year": 2026,
            },
            {
                "item_id": 20,
                "area_id": 102,
                "item_name": "정보보안기사 취득 (3학년 전용)",
                "target_grade": 3,
                "item_max_score": 100.0,
                "scoring_type": 1,
                "requires_evidence": True,
                "is_active": True,
                "year_id": 1,
                "area_grade": 3,
                "area_name": "전문기술역량",
                "academic_year": 2026,
            },
            {
                "item_id": 30,
                "area_id": 103,
                "item_name": "비활성화된 예비 항목",
                "target_grade": 1,
                "item_max_score": 20.0,
                "scoring_type": 1,
                "requires_evidence": False,
                "is_active": False,
                "year_id": 1,
                "area_grade": 1,
                "area_name": "교양",
                "academic_year": 2026,
            },
            {
                "item_id": 40,
                "area_id": 104,
                "item_name": "독서활동 (증빙 비필수 항목)",
                "target_grade": 1,
                "item_max_score": 30.0,
                "scoring_type": 1,
                "requires_evidence": False,
                "is_active": True,
                "year_id": 1,
                "area_grade": 1,
                "area_name": "인성역량",
                "academic_year": 2026,
            },
        ]

        self.db_academic_records = [
            {
                "record_id": 1,
                "student_id": 1,
                "year_id": 1,
                "grade": 1,
                "class_no": 2,
                "number": 5,
            },
            {
                "record_id": 2,
                "student_id": 2,
                "year_id": 1,
                "grade": 2,
                "class_no": 1,
                "number": 10,
            },
        ]

        self.db_submissions = []
        self.db_submissions_logs = []

    def cursor(self, cursor=None):
        return MockCursor(self)

    async def autocommit(self, val: bool):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class TestSubmissionsAPI(unittest.TestCase):
    def setUp(self):
        self.test_upload_dir = tempfile.mkdtemp(prefix="meister_submissions_test_")
        os.environ["UPLOAD_DIR"] = self.test_upload_dir

        self.mock_conn = MockConnection()
        self.mock_user = {
            "uuid": "student-uuid-1",
            "id": "student1",
            "role": "student",
        }

        # FastAPI 의존성 오버라이드 등록
        self.mock_redis = MockRedis()
        app.dependency_overrides[get_db] = lambda: self.mock_conn
        app.dependency_overrides[get_current_user] = lambda: self.mock_user
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        if os.path.exists(self.test_upload_dir):
            shutil.rmtree(self.test_upload_dir)

    def test_submit_evidence_with_file_success(self):
        """
        정상 케이스 1: POST /api/students/{studentId}/submissions
        파일 첨부 + 필수 메타데이터 전송 시 200 SrFormat 성공 응답
        """
        file_bytes = b"%PDF-1.4 test certificate content"
        files = {
            "file": ("정보처리기능사_합격증.pdf", io.BytesIO(file_bytes), "application/pdf")
        }
        data = {
            "item_id": 10,
            "detail": "정보처리기능사 국가공인자격 취득",
            "activity_date": "2026-05-10",
            "description": "2026년 제1회 정기 검정 합격",
        }

        response = self.client.post("/api/students/1/submissions", data=data, files=files)
        self.assertEqual(response.status_code, 200)

        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["status_code"], 200)

        data_res = body["data"]
        self.assertEqual(data_res["studentId"], 1)
        self.assertEqual(data_res["itemId"], 10)
        self.assertEqual(data_res["itemName"], "정보처리기능사 취득")
        self.assertEqual(data_res["detail"], "정보처리기능사 국가공인자격 취득")
        self.assertEqual(data_res["activityDate"], "2026-05-10")
        self.assertEqual(data_res["status"], "제출완료")
        self.assertEqual(data_res["statusCode"], 1)
        self.assertEqual(data_res["originalFilename"], "정보처리기능사_합격증.pdf")
        self.assertTrue(data_res["filePath"].startswith("/uploads/submissions/"))

        # DB 트랜잭션 및 submissions_logs 감사 기록 확인
        self.assertTrue(self.mock_conn.committed)
        self.assertEqual(len(self.mock_conn.db_submissions), 1)
        self.assertEqual(len(self.mock_conn.db_submissions_logs), 1)
        self.assertEqual(self.mock_conn.db_submissions_logs[0]["action_type"], "SUBMIT")

    def test_submit_evidence_with_link_success(self):
        """
        정상 케이스 2: 파일 없이 외부 링크(link_url)로 증빙 제출
        """
        data = {
            "item_id": 10,
            "detail": "자격 취득 온라인 조회 결과",
            "activity_date": "2026-06-01",
            "description": "Q-Net 자격증 진위확인 시스템 링크 첨부",
            "link_url": "https://www.q-net.or.kr/verify/12345",
        }

        response = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(response.status_code, 200)

        body = response.json()
        self.assertTrue(body["success"])
        data_res = body["data"]
        self.assertIsNone(data_res["filePath"])
        self.assertIsNone(data_res["originalFilename"])
        self.assertEqual(data_res["linkUrl"], "https://www.q-net.or.kr/verify/12345")

    def test_submit_evidence_direct_route_and_camel_case(self):
        """
        정상 케이스 3: POST /api/submissions 직접 호출 + camelCase 필드명 지원
        """
        data = {
            "itemId": 40,
            "detail": "인문학 고전 독서 활동",
            "activityDate": "2026-07-15",
            "description": "정의란 무엇인가 완독 후 독서록 작성",
        }

        response = self.client.post("/api/submissions", data=data)
        self.assertEqual(response.status_code, 200)

        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["studentId"], 1)
        self.assertEqual(body["data"]["itemId"], 40)

    def test_submit_evidence_other_student_forbidden(self):
        """
        보안 1: 학생 1이 학생 2의 ID로 대리 제출 시도 -> 403 FORBIDDEN
        """
        data = {
            "item_id": 10,
            "detail": "비정상 제출",
            "activity_date": "2026-05-10",
            "description": "타인 자료 대리 제출",
            "link_url": "https://example.com",
        }

        response = self.client.post("/api/students/2/submissions", data=data)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "FORBIDDEN")

    def test_submit_evidence_non_student_role_forbidden(self):
        """
        보안 2: 교사/관리자 계정으로 제출 시도 -> 403 FORBIDDEN
        """
        self.mock_user["role"] = "teacher"
        data = {
            "item_id": 10,
            "detail": "교사의 직접 학생 자료 제출",
            "activity_date": "2026-05-10",
            "description": "테스트",
            "link_url": "https://example.com",
        }

        response = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "FORBIDDEN")

    def test_submit_evidence_item_not_found(self):
        """
        예외 1: 존재하지 않는 item_id -> 404 ITEM_NOT_FOUND
        """
        data = {
            "item_id": 99999,
            "detail": "테스트",
            "activity_date": "2026-05-10",
            "description": "설명",
            "link_url": "https://example.com",
        }
        response = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "ITEM_NOT_FOUND")

    def test_submit_evidence_inactive_item_rejected(self):
        """
        예외 2: 비활성화된 항목(item_id=30) -> 400 VALIDATION_ERROR
        """
        data = {
            "item_id": 30,
            "detail": "비활성 항목 제출",
            "activity_date": "2026-05-10",
            "description": "설명",
        }
        response = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_submit_evidence_grade_mismatch_rejected(self):
        """
        예외 3: 1학년 학생이 3학년 전용 항목(item_id=20) 제출 -> 400 VALIDATION_ERROR
        """
        data = {
            "item_id": 20,
            "detail": "보안기사 취득",
            "activity_date": "2026-05-10",
            "description": "설명",
            "link_url": "https://example.com",
        }
        response = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_submit_evidence_missing_evidence_when_required(self):
        """
        예외 4: 증빙 필수(requires_evidence=True) 항목에 파일/링크 모두 미제출 -> 400 VALIDATION_ERROR
        """
        data = {
            "item_id": 10,
            "detail": "증빙 누락 제출",
            "activity_date": "2026-05-10",
            "description": "파일 및 링크 누락",
        }
        response = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("증빙자료", response.json()["error"]["message"])

    def test_submit_evidence_invalid_date_format_or_future(self):
        """
        예외 5: 활동 일자 형식 오류 또는 미래 일자 -> 400 VALIDATION_ERROR
        """
        # 잘못된 형식
        res1 = self.client.post(
            "/api/students/1/submissions",
            data={"item_id": 40, "detail": "독서", "activity_date": "2026/05/10", "description": "설명"},
        )
        self.assertEqual(res1.status_code, 400)
        self.assertEqual(res1.json()["error"]["code"], "VALIDATION_ERROR")

        # 미래 일자
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        res2 = self.client.post(
            "/api/students/1/submissions",
            data={"item_id": 40, "detail": "독서", "activity_date": tomorrow, "description": "설명"},
        )
        self.assertEqual(res2.status_code, 400)
        self.assertEqual(res2.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("미래", res2.json()["error"]["message"])

    def test_submit_evidence_invalid_file_type(self):
        """
        보안 3: 허용되지 않는 파일 확장자(.exe) 업로드 시 400 INVALID_FILE_TYPE
        """
        files = {
            "file": ("malware.exe", io.BytesIO(b"binary"), "application/octet-stream")
        }
        data = {
            "item_id": 10,
            "detail": "해킹 시도",
            "activity_date": "2026-05-10",
            "description": "설명",
        }
        response = self.client.post("/api/students/1/submissions", data=data, files=files)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_FILE_TYPE")

    def test_submit_evidence_duplicate_submission_conflict(self):
        """
        보안 4: 동일 학생, 항목, 활동명, 일자로 심사 대기 중인 건이 이미 존재하는 경우 409 DUPLICATE_SUBMISSION
        """
        # 첫 번째 제출
        data = {
            "item_id": 10,
            "detail": "정보처리기능사 자격증",
            "activity_date": "2026-05-10",
            "description": "최초 제출",
            "link_url": "https://example.com",
        }
        res1 = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(res1.status_code, 200)

        # 동일 내용 두 번째 제출 시도 (중복 제출 방어)
        res2 = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(res2.status_code, 409)
        self.assertEqual(res2.json()["error"]["code"], "DUPLICATE_SUBMISSION")

    def test_database_error_rolls_back_and_cleans_file(self):
        """
        안정성 1: DB 트랜잭션 에러 발생 시 트랜잭션 롤백 및 업로드된 파일 디스크 자동 삭제 확인
        """
        self.mock_conn.simulate_insert_error = True
        file_bytes = b"%PDF-1.4 important test certificate"
        files = {
            "file": ("자격증.pdf", io.BytesIO(file_bytes), "application/pdf")
        }
        data = {
            "item_id": 10,
            "detail": "오류 시뮬레이션",
            "activity_date": "2026-05-10",
            "description": "설명",
        }

        response = self.client.post("/api/students/1/submissions", data=data, files=files)
        # main.py의 전역 예외 핸들러가 500 INTERNAL_SERVER_ERROR 반환
        self.assertEqual(response.status_code, 500)
        self.assertTrue(self.mock_conn.rolled_back)

        # 업로드 폴더에 고아 파일이 남지 않고 정리되었는지 확인
        all_files = []
        for root, _, files in os.walk(self.test_upload_dir):
            all_files.extend(files)
        self.assertEqual(len(all_files), 0, "Uploaded file must be cleaned up on DB rollback")

    def test_submit_evidence_detail_empty_or_too_long(self):
        """예외 6: 세부 활동명 누락 또는 공백, 또는 200자 초과 -> 400 VALIDATION_ERROR"""
        # 공백
        res1 = self.client.post(
            "/api/students/1/submissions",
            data={"item_id": 40, "detail": "   ", "activity_date": "2026-05-10", "description": "설명"},
        )
        self.assertEqual(res1.status_code, 400)
        self.assertEqual(res1.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("detail", res1.json()["error"]["message"])

        # 200자 초과
        res2 = self.client.post(
            "/api/students/1/submissions",
            data={"item_id": 40, "detail": "A" * 201, "activity_date": "2026-05-10", "description": "설명"},
        )
        self.assertEqual(res2.status_code, 400)
        self.assertEqual(res2.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("200자", res2.json()["error"]["message"])

    def test_submit_evidence_description_empty(self):
        """예외 7: 설명 누락 또는 공백 -> 400 VALIDATION_ERROR"""
        res = self.client.post(
            "/api/students/1/submissions",
            data={"item_id": 40, "detail": "활동명", "activity_date": "2026-05-10", "description": "   "},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("description", res.json()["error"]["message"])

    def test_submit_evidence_invalid_link_scheme_or_too_long(self):
        """예외 8: 링크 URL 스키마 오류(javascript:, ftp:) 또는 500자 초과 -> 400 VALIDATION_ERROR"""
        # 스키마 오류
        res1 = self.client.post(
            "/api/students/1/submissions",
            data={"item_id": 10, "detail": "활동명", "activity_date": "2026-05-10", "description": "설명", "link_url": "javascript:alert(1)"},
        )
        self.assertEqual(res1.status_code, 400)
        self.assertEqual(res1.json()["error"]["code"], "VALIDATION_ERROR")

        # 500자 초과
        long_url = "https://example.com/" + ("x" * 500)
        res2 = self.client.post(
            "/api/students/1/submissions",
            data={"item_id": 10, "detail": "활동명", "activity_date": "2026-05-10", "description": "설명", "link_url": long_url},
        )
        self.assertEqual(res2.status_code, 400)
        self.assertEqual(res2.json()["error"]["code"], "VALIDATION_ERROR")

    def test_submit_evidence_optional_evidence_success(self):
        """정상 케이스 4: 증빙 비필수 항목(requires_evidence=False)은 파일/링크 없이 제출 가능"""
        data = {
            "item_id": 40,
            "detail": "독서활동",
            "activity_date": "2026-05-10",
            "description": "독서 후 감상문 교내 제출 완료",
        }
        res = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        self.assertIsNone(res.json()["data"]["filePath"])
        self.assertIsNone(res.json()["data"]["linkUrl"])

    def test_submit_evidence_student_no_academic_record(self):
        """예외 9: 학년도 학적 정보가 없는 학생의 제출 -> 404 USER_NOT_FOUND"""
        self.mock_conn.db_academic_records = []  # 학적 기록 제거
        data = {
            "item_id": 40,
            "detail": "독서",
            "activity_date": "2026-05-10",
            "description": "설명",
        }
        res = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["error"]["code"], "USER_NOT_FOUND")

    def test_submit_evidence_empty_file_rejected(self):
        """보안 6: 0바이트 빈 파일 업로드 시 400 INVALID_FILE_TYPE 거부 및 디스크 정리"""
        files = {
            "file": ("empty.pdf", io.BytesIO(b""), "application/pdf")
        }
        data = {
            "item_id": 10,
            "detail": "빈 파일 제출 시도",
            "activity_date": "2026-05-10",
            "description": "설명",
        }
        res = self.client.post("/api/students/1/submissions", data=data, files=files)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "INVALID_FILE_TYPE")
        self.assertIn("0 byte", res.json()["error"]["message"])

    def test_submit_evidence_already_approved_duplicate_conflict(self):
        """보안 7: 이미 인정(승인, status_code=3) 완료된 자료와 동일한 세부활동/일자 제출 시 409 DUPLICATE_SUBMISSION"""
        # 기승인된 건 DB에 삽입
        self.mock_conn.db_submissions.append({
            "submission_id": 999,
            "student_id": 1,
            "item_id": 10,
            "detail": "기승인 정보처리기능사",
            "activity_date": "2026-05-10",
            "file_path": "/uploads/submissions/cert.pdf",
            "original_filename": "cert.pdf",
            "link_url": None,
            "description": "과거 승인 건",
            "status_code": 3,  # 인정완료 상태
            "created_at": datetime.now(),
            "is_deleted": False,
        })

        data = {
            "item_id": 10,
            "detail": "기승인 정보처리기능사",
            "activity_date": "2026-05-10",
            "description": "동일 자격증 재청구 시도",
            "link_url": "https://example.com/cert",
        }
        res = self.client.post("/api/students/1/submissions", data=data)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["error"]["code"], "DUPLICATE_SUBMISSION")
        self.assertIn("이미 인정(승인) 완료", res.json()["error"]["message"])

    def test_submit_evidence_integer_role_zero_success(self):
        """보안 8: DB에서 정수형 role=0(학생)으로 토큰이 발급된 경우에도 200 OK 정상 처리"""
        self.mock_user["role"] = 0  # integer 0
        data = {
            "itemId": 40,
            "detail": "독서활동_정수역할",
            "activityDate": "2026-05-10",
            "description": "정수 role=0 학생 제출 테스트",
        }
        res = self.client.post("/api/submissions", data=data)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])

    def test_submit_evidence_invalid_link_bypass_blocked(self):
        """보안 9: 'https://' 등 호스트 없는 불완전 URL로 증빙 우회 시도 차단 -> 400 VALIDATION_ERROR"""
        bad_urls = ["https://", "http://", "https://   ", "https://.com"]
        for bad_url in bad_urls:
            data = {
                "item_id": 10,
                "detail": "URL 우회 시도",
                "activity_date": "2026-05-10",
                "description": "설명",
                "link_url": bad_url,
            }
            res = self.client.post("/api/students/1/submissions", data=data)
            self.assertEqual(res.status_code, 400, f"URL '{bad_url}' should be rejected")
            self.assertEqual(res.json()["error"]["code"], "VALIDATION_ERROR")

    def test_submit_evidence_year_validation(self):
        """비즈니스 로직: 요청 year와 항목의 academic_year 불일치 시 400, 일치 시 200"""
        # 불일치 학년도 (2024 != 2026)
        data_bad = {
            "item_id": 40,
            "detail": "독서활동",
            "activity_date": "2026-05-10",
            "description": "설명",
            "year": 2024,
        }
        res_bad = self.client.post("/api/students/1/submissions", data=data_bad)
        self.assertEqual(res_bad.status_code, 400)
        self.assertEqual(res_bad.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("학년도", res_bad.json()["error"]["message"])

        # 일치 학년도 (2026 == 2026)
        data_good = {
            "item_id": 40,
            "detail": "독서활동_2026",
            "activity_date": "2026-05-10",
            "description": "설명",
            "year": 2026,
        }
        res_good = self.client.post("/api/students/1/submissions", data=data_good)
        self.assertEqual(res_good.status_code, 200)

    def test_submit_evidence_area_validation(self):
        """비즈니스 로직: 요청 area와 항목의 area_name 불일치 시 400, 일치 시 200"""
        # 불일치 영역 ("인성역량" != "전공자격증")
        data_bad = {
            "item_id": 10,
            "detail": "자격증",
            "activity_date": "2026-05-10",
            "description": "설명",
            "link_url": "https://example.com/cert",
            "area": "인성역량",
        }
        res_bad = self.client.post("/api/students/1/submissions", data=data_bad)
        self.assertEqual(res_bad.status_code, 400)
        self.assertEqual(res_bad.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("인증 영역", res_bad.json()["error"]["message"])

        # 일치 영역 ("전공자격증" == "전공자격증")
        data_good = {
            "item_id": 10,
            "detail": "자격증_영역일치",
            "activity_date": "2026-05-10",
            "description": "설명",
            "link_url": "https://example.com/cert",
            "area": "전공자격증",
        }
        res_good = self.client.post("/api/students/1/submissions", data=data_good)
        self.assertEqual(res_good.status_code, 200)

    def test_submit_evidence_oversized_file_rejected(self):
        """보안 5: 용량 초과 파일(50MB 초과) 업로드 시 400 FILE_SIZE_EXCEEDED"""
        import core.storage
        orig_max = core.storage.MAX_FILE_SIZE_BYTES
        core.storage.MAX_FILE_SIZE_BYTES = 50  # 50 바이트 제한으로 임시 축소
        try:
            files = {
                "file": ("huge_evidence.zip", io.BytesIO(b"PK\x03\x04" + b"A" * 100), "application/zip")
            }
            data = {
                "item_id": 10,
                "detail": "대용량 첨부",
                "activity_date": "2026-05-10",
                "description": "설명",
            }
            res = self.client.post("/api/students/1/submissions", data=data, files=files)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.json()["error"]["code"], "FILE_SIZE_EXCEEDED")
        finally:
            core.storage.MAX_FILE_SIZE_BYTES = orig_max

    def test_uploaded_file_unauthorized_blocked(self):
        """보안 10: 인증 토큰 없이 /uploads 파일 열람 시도 시 401 UNAUTHORIZED 차단"""
        res = self.client.get("/uploads/submissions/2026/09/sample.pdf")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["error"]["code"], "UNAUTHORIZED")

    def test_uploaded_file_path_traversal_blocked(self):
        """보안 11: 상위 디렉터리 탐색(Path Traversal) 공격 시도 시 404 차단"""
        from core.security import create_access_token
        token = create_access_token({"sub": "student-uuid-1", "id": "student1", "role": "student"})
        res = self.client.get("/uploads/../../etc/passwd", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 404)

    def test_uploaded_file_rbac_and_ownership(self):
        """보안 12: 학생 증빙자료 RBAC 및 타인 열람 차단(403) / 본인 및 교사 열람 허용(200)"""
        from core.security import create_access_token
        import core.storage

        # 1. 실제 파일 디스크 생성
        upload_base = core.storage.get_upload_base_dir()
        test_file_dir = os.path.join(upload_base, "submissions", "2026", "09")
        os.makedirs(test_file_dir, exist_ok=True)
        test_file_path = os.path.join(test_file_dir, "test_cert.pdf")
        with open(test_file_path, "wb") as f:
            f.write(b"%PDF-1.4 test certificate content for rbac")

        try:
            # DB에 학생 1(student-uuid-1)의 제출물로 등록
            self.mock_conn.db_submissions.append({
                "submission_id": 99,
                "student_id": 1,
                "file_path": "/uploads/submissions/2026/09/test_cert.pdf",
                "is_deleted": False,
            })

            token_student1 = create_access_token({"sub": "student-uuid-1", "id": "student1", "role": "student"})
            token_student2 = create_access_token({"sub": "student-uuid-2", "id": "student2", "role": "student"})
            token_teacher = create_access_token({"sub": "teacher-uuid-1", "id": "teacher1", "role": "teacher"})

            # 2. 타인(학생 2) 열람 시도 -> 403 FORBIDDEN
            res_other = self.client.get(
                "/uploads/submissions/2026/09/test_cert.pdf",
                headers={"Authorization": f"Bearer {token_student2}"},
            )
            self.assertEqual(res_other.status_code, 403)
            self.assertEqual(res_other.json()["error"]["code"], "FORBIDDEN")

            # 3. 본인(학생 1) 열람 -> 200 OK (Bearer 헤더)
            res_owner = self.client.get(
                "/uploads/submissions/2026/09/test_cert.pdf",
                headers={"Authorization": f"Bearer {token_student1}"},
            )
            self.assertEqual(res_owner.status_code, 200)
            self.assertEqual(res_owner.content, b"%PDF-1.4 test certificate content for rbac")

            # 4. [보안 SEC-02] 일반 장기 Access 토큰을 쿼리 스트링으로 전송 시 401 차단
            res_query_access = self.client.get(
                f"/uploads/submissions/2026/09/test_cert.pdf?token={token_student1}"
            )
            self.assertEqual(res_query_access.status_code, 401)

            # 4-1. [보안 SEC-02] 파일 바인딩 단기 다운로드 서명 토큰으로는 200 OK 정상 열람
            signed_dl_token = create_signed_download_token(
                user_uuid="student-uuid-1",
                file_path="submissions/2026/09/test_cert.pdf",
                role="student",
            )
            res_query = self.client.get(
                f"/uploads/submissions/2026/09/test_cert.pdf?token={signed_dl_token}"
            )
            self.assertEqual(res_query.status_code, 200)

            # 5. 교사 열람 -> 200 OK
            res_teacher = self.client.get(
                "/uploads/submissions/2026/09/test_cert.pdf",
                headers={"Authorization": f"Bearer {token_teacher}"},
            )
            self.assertEqual(res_teacher.status_code, 200)
        finally:
            if os.path.exists(test_file_path):
                os.remove(test_file_path)

    def test_submit_evidence_concurrent_toctou_lock(self):
        """동시성 13: 동일 건 동시 요청 시 Redis 락 획득 실패에 따른 409 DUPLICATE_SUBMISSION 차단"""
        original_set = self.mock_redis.set
        async def mock_set_fail(key, val, nx=False, ex=None):
            if nx and "lock:submission" in key:
                return False
            return True
        self.mock_redis.set = mock_set_fail

        try:
            data = {
                "item_id": 10,
                "detail": "동시성 테스트",
                "activity_date": "2026-05-10",
                "description": "설명",
                "link_url": "https://example.com/cert",
            }
            res = self.client.post("/api/students/1/submissions", data=data)
            self.assertEqual(res.status_code, 409)
            self.assertEqual(res.json()["error"]["code"], "DUPLICATE_SUBMISSION")
            self.assertIn("현재 처리 중", res.json()["error"]["message"])
        finally:
            self.mock_redis.set = original_set


if __name__ == "__main__":
    unittest.main()
