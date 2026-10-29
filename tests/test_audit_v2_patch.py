"""
tests/test_audit_v2_patch.py - 2차 보안감사 및 교무분장 차등인가 패치 종합 테스트 스위트

검증 항목:
1. 복수 담임(정담임, 부담임, 임시담임) 일괄 등록 및 동일 직책 중복 차단
2. 학생-교사 이메일 교차 중복 차단 (Role Confusion 방지)
3. 학년부장(position='학년부장')의 학년 전체 과제 심사 허용 및 타 학년 차단(403)
4. 교과교사의 담당 교과/부서 과제 승인 허용 및 타 교과 과제 승인 차단(403)
5. 담임교사(position='정담임')의 타 학급 상벌점 부여 허용(201)
6. parse_xlsx_content Zip Bomb(50MB 압축해제 상한) 방어 및 read_only 파싱
7. 스프레드시트 수식 인젝션(_sanitize_spreadsheet_cell) 방어
8. send_otp_email 이메일 제목 내 평문 OTP 번호 미노출 검증
9. core/calculator.py 내 pointTotal 키 제공 검증
10. 교사 대시보드 scopeLabel 및 teacher DTO (position, department) 검증
"""

import glob
import io
import os
import sys
import unittest
import zipfile
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sp in glob.glob(os.path.join(_project_root, ".venv", "lib", "python*", "site-packages")):
    if os.path.exists(_sp) and _sp not in sys.path:
        sys.path.append(_sp)

from fastapi.testclient import TestClient

from core.authorization import authorize_student_access, authorize_submission_access
from core.calculator import calculate_student_certification
from core.email import send_otp_email
from core.excel_parser import parse_teacher_batch_file, parse_xlsx_content
from core.security import create_access_token
from database import get_db, get_redis
from main import app
from routers.admin import _sanitize_spreadsheet_cell


class AuditV2MockCursor:
    def __init__(self, conn):
        self.conn = conn
        self._current_result: List[Any] = []
        self._fetch_index = 0
        self.rowcount = 1
        self.lastrowid = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query: str, params: tuple = ()):
        q = " ".join(query.strip().split())
        self._fetch_index = 0

        if "FROM teachers" in q:
            if "WHERE uuid = %s" in q:
                u = params[0]
                self._current_result = [t for t in self.conn.teachers if t.get("uuid") == u and not t.get("is_deleted")]
            elif "WHERE email = %s" in q:
                em = params[0]
                self._current_result = [t for t in self.conn.teachers if t.get("email") == em and not t.get("is_deleted")]
            elif "WHERE email IN" in q:
                self._current_result = [t for t in self.conn.teachers if t.get("email") in params and not t.get("is_deleted")]
            else:
                self._current_result = list(self.conn.teachers)

        elif "FROM students" in q:
            if "WHERE email = %s" in q:
                em = params[0]
                self._current_result = [s for s in self.conn.students if s.get("email") == em and not s.get("is_deleted")]
            elif "WHERE email IN" in q:
                self._current_result = [s for s in self.conn.students if s.get("email") in params and not s.get("is_deleted")]
            elif "WHERE student_id = %s" in q:
                sid = params[0]
                self._current_result = [s for s in self.conn.students if s.get("student_id") == sid and not s.get("is_deleted")]
            else:
                self._current_result = list(self.conn.students)

        elif "FROM student_academic_records" in q:
            if "student_id IN" in q:
                st_ids = set(params[1:])
                self._current_result = [r for r in self.conn.records if r.get("student_id") in st_ids]
            else:
                sid = params[0]
                self._current_result = [r for r in self.conn.records if r.get("student_id") == sid]

        elif "FROM submissions s" in q:
            sub_id = params[0]
            self._current_result = [s for s in self.conn.submissions if s.get("submission_id") == sub_id and not s.get("is_deleted")]

        elif "FROM academic_years" in q:
            self._current_result = [{"year_id": 1, "year": 2026}]

        elif "INSERT INTO merits" in q:
            self.conn.merit_seq += 1
            self.lastrowid = self.conn.merit_seq
            self.conn.merits.append({
                "merits_point_id": self.lastrowid,
                "student_id": params[0],
                "teachers_id": params[1],
                "points": params[3],
            })
            self.rowcount = 1

        elif "INSERT INTO merits_log" in q:
            self.conn.merit_logs.append({"action_type": "CREATE"})
            self.rowcount = 1

        elif "SELECT LAST_INSERT_ID()" in q:
            self._current_result = [{"last_id": self.conn.merit_seq}]

        elif "UPDATE submissions" in q:
            self.rowcount = 1

        elif "INSERT INTO submissions_logs" in q:
            self.rowcount = 1

        elif "UPDATE teachers" in q:
            self.rowcount = 1

        elif "INSERT INTO teachers" in q:
            self.rowcount = 1

        elif "SELECT MAX(year) AS current_year" in q or "MAX(year)" in q:
            self._current_result = [{"current_year": 2026}]

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


class AuditV2MockConnection:
    def __init__(self):
        self.cursor_instance = AuditV2MockCursor(self)
        self.teachers = []
        self.students = []
        self.records = []
        self.submissions = []
        self.merits = []
        self.merit_logs = []
        self.merit_seq = 100

    def cursor(self, cursor=None):
        return self.cursor_instance

    async def autocommit(self, val):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class TestAuditV2SecurityAndPermissions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_redis = MagicMock()

        async def _fake_redis_get(key):
            return None

        self.mock_redis.get = _fake_redis_get
        self.mock_redis.set = MagicMock(return_value=True)
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        self.conn = AuditV2MockConnection()
        app.dependency_overrides[get_db] = lambda: self.conn

    def tearDown(self):
        app.dependency_overrides.clear()

    # =========================================================================
    # 1. 복수 담임(정담임, 부담임, 임시담임) 파싱 및 중복 직책 검증
    # =========================================================================
    def test_multi_homeroom_teachers_excel_parser(self):
        """한 학급(1-1)에 정담임, 부담임, 임시담임 복수 배정 허용 및 동일 학급-직책 중복 차단 검증"""
        valid_rows = [
            ["성명", "이메일", "담당과목", "직책", "소속부서", "학년", "반"],
            ["김정담임", "jung@bssm.hs.kr", "국어", "정담임", "교무기획부", "1", "1"],
            ["박부담임", "bu@bssm.hs.kr", "영어", "부담임", "외국어교육부", "1", "1"],
            ["최임시담임", "imsi@bssm.hs.kr", "수학", "임시담임", "마이스터부", "1", "1"],
            ["이교과", "gyogwa@bssm.hs.kr", "정보", "교과전담", "마이스터부", "", ""],
        ]
        csv_text = "\n".join([",".join(row) for row in valid_rows])
        result_records, result_errors = parse_teacher_batch_file(csv_text.encode("utf-8"), filename="teachers.csv")
        self.assertEqual(len(result_errors), 0)
        self.assertEqual(len(result_records), 4)

        # 직책 및 부서 정상 파싱 확인
        self.assertEqual(result_records[0]["position"], "정담임")
        self.assertEqual(result_records[1]["position"], "부담임")
        self.assertEqual(result_records[2]["position"], "임시담임")
        self.assertEqual(result_records[3]["position"], "교과전담")

        # 동일 학급에 동일 직책(정담임 2명) 중복 배정 시 차단 검증
        dup_rows = [
            ["성명", "이메일", "담당과목", "직책", "소속부서", "학년", "반"],
            ["김정담임1", "jung1@bssm.hs.kr", "국어", "정담임", "교무기획부", "1", "1"],
            ["김정담임2", "jung2@bssm.hs.kr", "수학", "정담임", "교무기획부", "1", "1"],
        ]
        dup_text = "\n".join([",".join(row) for row in dup_rows])
        dup_records, dup_errors = parse_teacher_batch_file(dup_text.encode("utf-8"), filename="teachers.csv")
        self.assertEqual(len(dup_errors), 1)
        self.assertIn("중복 배정", dup_errors[0]["reason"])

    # =========================================================================
    # 2. 학생-교사 이메일 교차 중복 차단 (Role Confusion 방어)
    # =========================================================================
    def test_student_teacher_cross_email_blocked(self):
        """학생 등록 시 기존 교사 이메일 사용 시 차단, 교사 등록 시 기존 학생 이메일 사용 시 차단"""
        admin_token = create_access_token({"sub": "admin-uuid", "role": "admin"})

        # (1) 교사 명단에 이미 등록된 이메일
        self.conn.teachers = [
            {"teachers_id": 1, "email": "teacher_exist@bssm.hs.kr", "is_deleted": False}
        ]
        resp_st = self.client.post(
            "/api/admin/students/batch",
            json={
                "year": 2026,
                "students": [
                    {"name": "가짜학생", "email": "teacher_exist@bssm.hs.kr", "grade": 1, "classNo": 1, "studentNo": 1}
                ],
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(resp_st.status_code, 200)
        st_data = resp_st.json()["data"]
        self.assertEqual(len(st_data["errors"]), 1)
        self.assertIn("교사 명단에 이미 등록된 이메일", st_data["errors"][0]["reason"])

        # (2) 학생 명단에 이미 등록된 이메일
        self.conn.students = [
            {"student_id": 1, "email": "student_exist@bssm.hs.kr", "is_deleted": False}
        ]
        resp_tc = self.client.post(
            "/api/admin/teachers/batch",
            json={
                "year": 2026,
                "teachers": [
                    {"name": "가짜교사", "email": "student_exist@bssm.hs.kr", "subject": "과학"}
                ],
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(resp_tc.status_code, 200)
        tc_data = resp_tc.json()["data"]
        self.assertEqual(len(tc_data["errors"]), 1)
        self.assertIn("학생 명단에 이미 등록된 이메일", tc_data["errors"][0]["reason"])

    # =========================================================================
    # 3. 학년부장(position='학년부장') 과제 심사 인가 검증
    # =========================================================================
    def test_grade_head_submission_approval_authorization(self):
        """1학년 학년부장은 1학년 학생 과제는 승인 가능(200), 2학년 학생 과제는 차단(403)"""
        # 1학년 학년부장 (uuid: 'gh-uuid-1', grade: 1, position: '학년부장')
        self.conn.teachers = [
            {"teachers_id": 10, "uuid": "gh-uuid-1", "name": "김학년부장", "grade": 1, "class": None, "position": "학년부장", "department": "교무기획부", "subject": "국어", "is_deleted": False}
        ]
        # 학생 1: 1학년 2반
        # 학생 2: 2학년 1반
        self.conn.records = [
            {"student_id": 1, "year_id": 1, "grade": 1, "class_no": 2},
            {"student_id": 2, "year_id": 1, "grade": 2, "class_no": 1},
        ]
        # 과제 1: 학생 1 제출
        # 과제 2: 학생 2 제출
        self.conn.submissions = [
            {"submission_id": 101, "student_id": 1, "student_uuid": "st-uuid-1", "status_code": 1, "item_department": "전공", "is_deleted": False, "year": 2026},
            {"submission_id": 102, "student_id": 2, "student_uuid": "st-uuid-2", "status_code": 1, "item_department": "전공", "is_deleted": False, "year": 2026},
        ]

        gh_token = create_access_token({"sub": "gh-uuid-1", "role": "teacher"})

        # 1학년 과제 승인 시도 -> 성공
        res1 = self.client.patch(
            "/api/submissions/101/approve",
            json={"score": 10.0, "comment": "1학년 부장 승인"},
            headers={"Authorization": f"Bearer {gh_token}"},
        )
        self.assertEqual(res1.status_code, 200)

        # 2학년 과제 승인 시도 -> 403 Forbidden
        res2 = self.client.patch(
            "/api/submissions/102/approve",
            json={"score": 10.0, "comment": "타학년 과제 승인 시도"},
            headers={"Authorization": f"Bearer {gh_token}"},
        )
        self.assertEqual(res2.status_code, 403)
        self.assertIn("1학년", res2.json()["error"]["message"])

    # =========================================================================
    # 4. 교과교사의 담당 교과/부서 매칭 과제 승인 허용 및 타 교과 차단(403)
    # =========================================================================
    def test_subject_teacher_submission_approval_authorization(self):
        """영어 교사는 영어 과제 승인 허용, 전공기술 과제는 403 차단"""
        # 영어 교사 (uuid: 'eng-uuid', position: '교과전담', department: '외국어교육부', subject: '영어')
        self.conn.teachers = [
            {"teachers_id": 20, "uuid": "eng-uuid", "name": "이영어", "grade": None, "class": None, "position": "교과전담", "department": "외국어교육부", "subject": "영어", "is_deleted": False}
        ]
        self.conn.records = [
            {"student_id": 1, "year_id": 1, "grade": 1, "class_no": 1},
        ]
        self.conn.submissions = [
            # 영어 과제
            {"submission_id": 201, "student_id": 1, "student_uuid": "st-uuid-1", "status_code": 1, "item_department": "영어", "item_name": "영어 원문 독해", "area_name": "외국어능력", "is_deleted": False, "year": 2026},
            # 전공 과제
            {"submission_id": 202, "student_id": 1, "student_uuid": "st-uuid-1", "status_code": 1, "item_department": "마이스터부", "item_name": "임베디드 프로젝트", "area_name": "전문기술역량", "is_deleted": False, "year": 2026},
        ]

        eng_token = create_access_token({"sub": "eng-uuid", "role": "teacher"})

        # 영어 과제 승인 -> 200
        res_eng = self.client.patch(
            "/api/submissions/201/approve",
            json={"score": 15.0, "comment": "영어 해석 우수"},
            headers={"Authorization": f"Bearer {eng_token}"},
        )
        self.assertEqual(res_eng.status_code, 200)

        # 전공 과제 승인 시도 -> 403
        res_major = self.client.patch(
            "/api/submissions/202/approve",
            json={"score": 15.0, "comment": "타 교과 과제 승인 시도"},
            headers={"Authorization": f"Bearer {eng_token}"},
        )
        self.assertEqual(res_major.status_code, 403)
        self.assertEqual(res_major.json()["error"]["code"], "FORBIDDEN")

    # =========================================================================
    # 5. 담임교사(position='정담임')의 타 학급 상벌점 부여 허용(201)
    # =========================================================================
    def test_homeroom_teacher_cross_class_points_creation_allowed(self):
        """1-1 정담임 교사가 2-3 학생에게 상벌점 부여 시 201 성공 검증 (교무분장 3.13 반영)"""
        # 1-1 정담임 교사
        self.conn.teachers = [
            {"teachers_id": 30, "uuid": "hr-uuid-1", "name": "박정담임", "grade": 1, "class": 1, "position": "정담임", "department": "교무기획부", "is_deleted": False}
        ]
        # 학생 2: 2학년 3반
        self.conn.students = [
            {"student_id": 2, "uuid": "st-uuid-2", "name": "이몽룡", "is_deleted": False}
        ]
        self.conn.records = [
            {"student_id": 2, "year_id": 1, "grade": 2, "class_no": 3}
        ]

        hr_token = create_access_token({"sub": "hr-uuid-1", "role": "teacher"})

        res = self.client.post(
            "/api/points",
            json={
                "studentId": 2,
                "type": "상점",
                "score": 3.0,
                "reason": "복도 보행 지도 준수",
            },
            headers={"Authorization": f"Bearer {hr_token}"},
        )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.json()["success"])
        self.assertEqual(res.json()["data"]["points"], 3.0)

    # =========================================================================
    # 6. parse_xlsx_content Zip Bomb(50MB 압축해제 상한) 방어 검증
    # =========================================================================
    def test_zip_bomb_uncompressed_limit_defense(self):
        """압축 해제 크기가 50MB를 초과하는 zip 파일 파싱 시 400 Bad Request 차단 검증"""
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", b"<Types/>" * 100)

        with patch("core.excel_parser.MAX_UNCOMPRESSED_XLSX_BYTES", 50):
            with self.assertRaises(ValueError) as ctx:
                parse_xlsx_content(zip_buf.getvalue())
            self.assertIn("50MB", str(ctx.exception))

    # =========================================================================
    # 7. 스프레드시트 수식 인젝션(_sanitize_spreadsheet_cell) 방어 검증
    # =========================================================================
    def test_spreadsheet_formula_injection_sanitization(self):
        """수식 기호(=, +, -, @, |) 및 선행 공백 뒤 수식 기호에 작은따옴표 접두사 부착 검증"""
        self.assertEqual(_sanitize_spreadsheet_cell("=CMD('calc')"), "'=CMD('calc')")
        self.assertEqual(_sanitize_spreadsheet_cell("+SUM(A1:A10)"), "'+SUM(A1:A10)")
        self.assertEqual(_sanitize_spreadsheet_cell("-5"), "'-5")
        self.assertEqual(_sanitize_spreadsheet_cell("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(_sanitize_spreadsheet_cell("|calc"), "'|calc")
        self.assertEqual(_sanitize_spreadsheet_cell("   =1+1"), "'   =1+1")
        self.assertEqual(_sanitize_spreadsheet_cell("\t=1+1"), "'\t=1+1")
        # 일반 문자열 및 수치형은 원본 유지
        self.assertEqual(_sanitize_spreadsheet_cell("홍길동"), "홍길동")
        self.assertEqual(_sanitize_spreadsheet_cell(123), 123)
        self.assertEqual(_sanitize_spreadsheet_cell(45.6), 45.6)
        self.assertIsNone(_sanitize_spreadsheet_cell(None))

    # =========================================================================
    # 8. send_otp_email 이메일 제목 내 평문 OTP 번호 노출 제거 검증
    # =========================================================================
    async def test_email_otp_subject_no_plaintext_otp(self):
        """이메일 발송 시 제목에 평문 OTP({otp_code})가 노출되지 않는지 검증"""
        with patch.dict(os.environ, {"EMAIL_PROVIDER": "smtp"}), \
             patch("core.email._send_via_smtp", new_callable=AsyncMock) as mock_send, \
             patch("core.email.EMAIL_PROVIDER", "smtp"):
            mock_send.return_value = True
            otp = "847291"
            await send_otp_email("test@bssm.hs.kr", otp)
            mock_send.assert_called_once()
            call_args = mock_send.call_args
            subject = call_args[0][1]
            self.assertNotIn(otp, subject, "이메일 제목에 평문 OTP 코드가 노출되어서는 안 됩니다.")
            self.assertIn("본인인증 번호 안내", subject)

    # =========================================================================
    # 9. core/calculator.py 내 pointTotal 키 호환성 검증
    # =========================================================================
    def test_calculator_point_total_key_compatibility(self):
        """calculate_student_certification 반환값에 totalMeritScore와 동일한 pointTotal 키 존재 검증"""
        areas = [{"area_id": 1, "name": "전공", "max_score": 100.0, "weight": 1.0, "is_essential": True, "min_score": 60.0}]
        subs = []
        merits = [{"type": "+", "points": 5.0, "is_reflected": True}]

        res = calculate_student_certification(areas, subs, merits)
        self.assertIn("pointTotal", res)
        self.assertIn("totalMeritScore", res)
        self.assertEqual(res["pointTotal"], res["totalMeritScore"])
        self.assertEqual(res["pointTotal"], 5.0)

    # =========================================================================
    # 10. 교사 대시보드 scopeLabel 및 teacher DTO (position, department)
    # =========================================================================
    def test_teacher_dashboard_scope_label_and_dto(self):
        """학년부장 및 부담임 대시보드 조회 시 scopeLabel 포맷과 teacher 객체 내 position, department 반환 검증"""
        self.conn.teachers = [
            {"teachers_id": 99, "uuid": "bu-uuid-1", "name": "이부담임", "grade": 1, "class": 2, "position": "부담임", "department": "외국어교육부", "subject": "영어", "is_deleted": False}
        ]
        token = create_access_token({"sub": "bu-uuid-1", "role": "teacher"})
        resp = self.client.get("/api/teacher/dashboard", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["scopeLabel"], "1학년 2반 부담임 (영어)")
        self.assertEqual(data["teacher"]["position"], "부담임")
        self.assertEqual(data["teacher"]["department"], "외국어교육부")

    # =========================================================================
    # 11. 정담임 교사 대시보드 scopeLabel 포맷 ("1학년 2반 정담임 (영어)") 검증
    # =========================================================================
    def test_teacher_dashboard_scope_label_jungdamim(self):
        """정담임 교사 대시보드 조회 시 '1학년 2반 정담임 (영어)' 포맷 생성 검증"""
        self.conn.teachers = [
            {"teachers_id": 98, "uuid": "jung-uuid-1", "name": "김정담임", "grade": 1, "class": 2, "position": "정담임", "department": "교무기획부", "subject": "영어", "is_deleted": False}
        ]
        token = create_access_token({"sub": "jung-uuid-1", "role": "teacher"})
        resp = self.client.get("/api/teacher/dashboard", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["scopeLabel"], "1학년 2반 정담임 (영어)")
        self.assertEqual(data["teacher"]["position"], "정담임")

    # =========================================================================
    # 12. 학년부장(position='학년부장') 반 미지정 일괄 등록 허용 및 학년 미지정 차단
    # =========================================================================
    def test_grade_head_batch_registration_without_class_allowed(self):
        """학년부장은 학년만 있고 반이 없는 경우 정상 등록(200) 허용 및 학년 미지정 시 차단 검증"""
        # (1) CSV 파싱 검증
        csv_rows = [
            ["성명", "이메일", "담당과목", "직책", "소속부서", "학년", "반"],
            ["강부장", "gradehead@bssm.hs.kr", "국어", "학년부장", "교무기획부", "2", ""],
        ]
        csv_text = "\n".join([",".join(r) for r in csv_rows])
        records, errors = parse_teacher_batch_file(csv_text.encode("utf-8"), filename="gh.csv")
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["grade"], 2)
        self.assertIsNone(records[0]["class_no"])
        self.assertEqual(records[0]["position"], "학년부장")

        # 학년부장인데 학년 누락 시 에러
        csv_no_grade = [
            ["성명", "이메일", "담당과목", "직책", "소속부서", "학년", "반"],
            ["강부장오류", "gh_err@bssm.hs.kr", "국어", "학년부장", "교무기획부", "", ""],
        ]
        csv_no_grade_text = "\n".join([",".join(r) for r in csv_no_grade])
        _, errs_no_grade = parse_teacher_batch_file(csv_no_grade_text.encode("utf-8"), filename="gh.csv")
        self.assertEqual(len(errs_no_grade), 1)
        self.assertIn("담당 학년이 필수", errs_no_grade[0]["reason"])

        # (2) JSON API 검증
        admin_token = create_access_token({"sub": "admin-uuid", "role": "admin"})
        resp_json = self.client.post(
            "/api/admin/teachers/batch",
            json={
                "year": 2026,
                "teachers": [
                    {"name": "강부장JSON", "email": "gh_json@bssm.hs.kr", "position": "학년부장", "department": "교무기획부", "grade": 2}
                ],
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(resp_json.status_code, 200)
        data = resp_json.json()["data"]
        self.assertEqual(data["createdTeachersCount"], 1)
        self.assertEqual(len(data["errors"]), 0)

    # =========================================================================
    # 13. 부서 매칭 토큰 확장(외국어교육부 -> 영어/외국어 과제 승인 허용)
    # =========================================================================
    def test_foreign_lang_department_teacher_approval_matching(self):
        """subject가 없더라도 department='외국어교육부'인 교사가 영어/외국어 과제 승인 허용 및 타 교과 차단 검증"""
        # 외국어교육부 교사 (subject 미기재)
        self.conn.teachers = [
            {"teachers_id": 55, "uuid": "fl-uuid-1", "name": "최외국어", "grade": None, "class": None, "position": "교과전담", "department": "외국어교육부", "subject": None, "is_deleted": False}
        ]
        self.conn.records = [
            {"student_id": 1, "year_id": 1, "grade": 1, "class_no": 1},
        ]
        self.conn.submissions = [
            # 영어 원문 독해 과제 (item_department 미지정이나 영역이 외국어능력)
            {"submission_id": 301, "student_id": 1, "student_uuid": "st-uuid-1", "status_code": 1, "item_department": None, "item_name": "학기별 영어 역량 강화를 위한 원문 해석본 제출", "area_name": "외국어능력", "is_deleted": False, "year": 2026},
            # 전공 프로젝트 과제
            {"submission_id": 302, "student_id": 1, "student_uuid": "st-uuid-1", "status_code": 1, "item_department": "전공", "item_name": "임베디드 프로젝트", "area_name": "전문기술역량", "is_deleted": False, "year": 2026},
        ]

        token = create_access_token({"sub": "fl-uuid-1", "role": "teacher"})

        # 외국어교육부 교사가 영어 원문 해석본 과제 승인 -> 200 허용
        res1 = self.client.patch(
            "/api/submissions/301/approve",
            json={"score": 20.0, "comment": "외국어교육부 승인"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(res1.status_code, 200)

        # 외국어교육부 교사가 전공 과제 승인 시도 -> 403 차단
        res2 = self.client.patch(
            "/api/submissions/302/approve",
            json={"score": 20.0, "comment": "외국어교육부 전공 승인 시도"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(res2.status_code, 403)

    # =========================================================================
    # 14. 학년부장의 all=True 타 학년 학생 검색 허용 검증
    # =========================================================================
    def test_grade_head_all_param_cross_grade_query(self):
        """1학년 학년부장이 all=true 파라미터로 2학년 학생 검색 시 403 차단 없이 200 허용 검증"""
        self.conn.teachers = [
            {"teachers_id": 77, "uuid": "gh-uuid-77", "name": "정학년부장", "grade": 1, "class": None, "position": "학년부장", "department": "교무기획부", "subject": "국어", "is_deleted": False}
        ]
        token = create_access_token({"sub": "gh-uuid-77", "role": "teacher"})

        # all=True 포함 조회 -> 200 허용
        resp = self.client.get(
            "/api/teacher/students?grade=2&all=true",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)

        # all 파라미터 없이 타 학년(2학년) 조회 시에도 전교생 열람 정책에 따라 200 허용
        resp_block = self.client.get(
            "/api/teacher/students?grade=2",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp_block.status_code, 200)


if __name__ == "__main__":
    unittest.main()
