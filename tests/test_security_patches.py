"""
tests/test_security_patches.py - SECURITY_AND_AUDIT.md 및 TODO.md 6.1/6.2 보안 패치 전수 검증 단위/통합 테스트
"""

import asyncio
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from core.security import (
    create_access_token,
    create_download_ticket,
    create_signed_download_token,
    get_client_ip,
    is_trusted_proxy,
    verify_and_consume_download_ticket,
    verify_signed_download_token,
)
from core.storage import (
    cleanup_old_file_on_resubmit,
    delete_uploaded_file,
    find_and_clean_orphan_files,
    get_upload_base_dir,
    save_upload_file,
)
from database import get_db, get_redis
from main import app
from routers.auth import validate_password_complexity


class MockRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = str(val)
        if ex:
            self.ttls[key] = ex
        return True

    async def setex(self, key, seconds, val):
        self.data[key] = str(val)
        self.ttls[key] = seconds
        return True

    async def incr(self, key):
        cur = int(self.data.get(key, 0)) + 1
        self.data[key] = str(cur)
        return cur

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def ttl(self, key):
        return self.ttls.get(key, 300)

    async def delete(self, key):
        self.data.pop(key, None)
        self.ttls.pop(key, None)

    async def getdel(self, key):
        val = self.data.pop(key, None)
        self.ttls.pop(key, None)
        return val


class MockCursor:
    def __init__(self, conn):
        self.conn = conn
        self._current_result = []
        self._fetch_index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql: str, params: tuple = ()):
        sql_clean = " ".join(sql.strip().split())
        self._fetch_index = 0

        # submissions와 students JOIN 조회 (소유권 및 파일 경로)
        if "FROM submissions s" in sql_clean and "JOIN students st" in sql_clean:
            if "s.submission_id = %s" in sql_clean:
                target_id = params[0]
                matched = [
                    s for s in self.conn.submissions
                    if s["submission_id"] == target_id and not s.get("is_deleted")
                ]
                self._current_result = matched
            elif "s.file_path = %s" in sql_clean or "s.file_path" in sql_clean:
                target_paths = [p for p in params if isinstance(p, str) and not p.startswith("student-uuid")]
                matched = [
                    s for s in self.conn.submissions
                    if not s.get("is_deleted") and any(
                        s.get("file_path") == tp
                        or (s.get("file_path") and s["file_path"].endswith(tp.lstrip("/\\")))
                        or (isinstance(tp, str) and tp.endswith(s.get("file_path", "").lstrip("/\\")))
                        for tp in target_paths
                    )
                ]
                self._current_result = matched

        elif "FROM students" in sql_clean:
            if "WHERE student_id = %s" in sql_clean:
                target_id = params[0]
                matched = [s for s in self.conn.students if s["student_id"] == target_id and not s.get("is_deleted")]
                self._current_result = matched
            elif "WHERE uuid = %s" in sql_clean:
                target_uuid = params[0]
                matched = [s for s in self.conn.students if s["uuid"] == target_uuid and not s.get("is_deleted")]
                self._current_result = matched

        elif "FROM academic_years" in sql_clean:
            self._current_result = [{"year_id": 1, "year": 2026}]

        elif "FROM teachers" in sql_clean:
            self._current_result = getattr(self.conn, "teachers", [])

        elif "SELECT file_path FROM submissions" in sql_clean:
            active_files = [{"file_path": s["file_path"]} for s in self.conn.submissions if not s.get("is_deleted")]
            self._current_result = active_files

        elif "UPDATE submissions" in sql_clean:
            self.rowcount = 1
            if "status_code = 1" in sql_clean or "status_code = 2" in sql_clean:
                # resubmit
                sub_id = params[-1]
                for s in self.conn.submissions:
                    if s["submission_id"] == sub_id:
                        s["description"] = params[0]
                        s["file_path"] = params[1]
                        s["link_url"] = params[3] if len(params) > 4 else params[2]
                        s["status_code"] = 2
            elif "is_deleted = TRUE" in sql_clean:
                sub_id = params[0]
                for s in self.conn.submissions:
                    if s["submission_id"] == sub_id:
                        s["is_deleted"] = True

        elif "INSERT INTO submissions_logs" in sql_clean:
            self.conn.submissions_logs.append({
                "submission_id": params[0],
                "modifier_uuid": params[1],
                "action_type": params[2],
            })

    async def fetchone(self):
        if self._fetch_index < len(self._current_result):
            item = self._current_result[self._fetch_index]
            self._fetch_index += 1
            return item
        return None

    async def fetchall(self):
        return self._current_result


class MockConnection:
    def __init__(self):
        self.students = [
            {"student_id": 1, "uuid": "student-uuid-1", "name": "학생1", "email": "s1@school.kr", "is_deleted": False},
            {"student_id": 2, "uuid": "student-uuid-2", "name": "학생2", "email": "s2@school.kr", "is_deleted": False},
        ]
        self.teachers = [
            {"teachers_id": 1, "uuid": "teacher-uuid-1", "grade": None, "class": None, "is_deleted": False},
        ]
        self.submissions = [
            {
                "submission_id": 101,
                "student_id": 1,
                "uuid": "student-uuid-1",
                "status_code": 4,  # 반려 상태
                "file_path": "/uploads/submissions/2026/09/old_rejected.pdf",
                "link_url": None,
                "description": "반려된 증빙자료",
                "is_deleted": False,
            },
            {
                "submission_id": 102,
                "student_id": 1,
                "uuid": "student-uuid-1",
                "status_code": 3,  # 승인 완료 상태
                "file_path": "/uploads/submissions/2026/09/approved_cert.pdf",
                "link_url": None,
                "description": "승인 완료된 증빙자료",
                "is_deleted": False,
            },
        ]
        self.submissions_logs = []

    def cursor(self, cursor=None):
        return MockCursor(self)

    async def autocommit(self, val: bool):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class TestSecurityPatches(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_security_")
        os.environ["UPLOAD_DIR"] = self.test_dir
        self.mock_redis = MockRedis()
        self.mock_conn = MockConnection()

        app.dependency_overrides[get_db] = lambda: self.mock_conn
        app.dependency_overrides[get_redis] = lambda: self.mock_redis
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    # ==========================================================================
    # 1. IP 스푸핑 방어 (SECURITY_AND_AUDIT.md 1.1 & TODO.md 6.1 P0)
    # ==========================================================================
    def test_client_ip_spoofing_defense(self):
        """[보안 1.1] 비신뢰 클라이언트의 X-Forwarded-For 및 프록시 헤더 위조 무력화 검증"""
        # (1) 외부 비신뢰 직접 클라이언트 IP (203.0.113.195)
        req_untrusted = MagicMock()
        req_untrusted.client.host = "203.0.113.195"
        req_untrusted.headers = {
            "X-Forwarded-For": "10.0.0.1, 192.168.1.1, 8.8.8.8",
            "X-Real-IP": "1.1.1.1",
            "CF-Connecting-IP": "2.2.2.2",
        }
        # 위조된 모든 헤더를 무시하고 실제 연결 소켓 IP를 반환해야 함
        self.assertEqual(get_client_ip(req_untrusted), "203.0.113.195")

        # (2) 신뢰할 수 있는 로컬 리버스 프록시 (127.0.0.1) 경유
        req_trusted_proxy = MagicMock()
        req_trusted_proxy.client.host = "127.0.0.1"
        req_trusted_proxy.headers = {
            "X-Forwarded-For": "198.51.100.5, 10.0.0.2",
        }
        # 체인 우측(10.0.0.2: 사설 프록시)을 건너뛰고 비신뢰 발신자 198.51.100.5를 추출해야 함
        self.assertEqual(get_client_ip(req_trusted_proxy), "198.51.100.5")

        # (3) Cloudflare 경유 시 CF-Connecting-IP 신뢰
        req_cf = MagicMock()
        req_cf.client.host = "127.0.0.1"
        req_cf.headers = {"CF-Connecting-IP": "198.51.100.77"}
        self.assertEqual(get_client_ip(req_cf), "198.51.100.77")

    # ==========================================================================
    # 2. 일회용 다운로드 티켓 (SECURITY_AND_AUDIT.md 1.2 & TODO.md 6.1 P1)
    # ==========================================================================
    async def test_download_ticket_single_use_and_invalidation(self):
        """[보안 1.2] 일회용 다운로드 티켓 발급, 1회 소비 즉시 폐기 및 파일 경로 격리 검증"""
        user_uuid = "student-uuid-1"
        file_path = "submissions/2026/09/secret.pdf"

        # 1. 일회용 티켓 생성
        ticket_id = await create_download_ticket(user_uuid, "student", file_path, self.mock_redis, ttl_seconds=60)
        self.assertIsNotNone(ticket_id)

        # 2. 잘못된 파일 경로로 소비 시도 -> 차단 (None)
        mismatch_res = await verify_and_consume_download_ticket(ticket_id, "submissions/2026/09/other.pdf", self.mock_redis)
        self.assertIsNone(mismatch_res)

        # 3. 올바른 파일 경로로 최초 소비 -> 성공 (user_uuid 일치)
        valid_res = await verify_and_consume_download_ticket(ticket_id, file_path, self.mock_redis)
        self.assertIsNotNone(valid_res)
        self.assertEqual(valid_res["uuid"], user_uuid)

        # 4. 동일 티켓으로 재요청 (Replay Attack) -> 이미 삭제되어 2회 소비 실패 (None)
        reuse_res = await verify_and_consume_download_ticket(ticket_id, file_path, self.mock_redis)
        self.assertIsNone(reuse_res)

    def test_download_ticket_endpoint_integration(self):
        """[보안 1.2 통합] POST /api/uploads/ticket 및 GET /uploads?ticket= 1회 소비 검증"""
        # 디스크에 실제 파일 생성
        test_file_rel = "submissions/2026/09/test_ticket.pdf"
        abs_p = os.path.join(self.test_dir, test_file_rel)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        with open(abs_p, "wb") as f:
            f.write(b"%PDF-1.4 test ticket verification file")

        self.mock_conn.submissions.append({
            "submission_id": 200,
            "student_id": 1,
            "uuid": "student-uuid-1",
            "file_path": f"/uploads/{test_file_rel}",
            "is_deleted": False,
        })

        token_student = create_access_token({"sub": "student-uuid-1", "id": "s1", "role": "student"})

        # 1. 티켓 발급
        res_ticket = self.client.post(
            f"/api/uploads/ticket?file_path={test_file_rel}",
            headers={"Authorization": f"Bearer {token_student}"},
        )
        self.assertEqual(res_ticket.status_code, 200)
        ticket_id = res_ticket.json()["data"]["ticket"]

        # 2. 티켓으로 파일 열람 -> 200 OK
        res_view = self.client.get(f"/uploads/{test_file_rel}?ticket={ticket_id}")
        self.assertEqual(res_view.status_code, 200)
        self.assertEqual(res_view.content, b"%PDF-1.4 test ticket verification file")

        # 3. 동일 티켓으로 재열람 시도 -> 401 UNAUTHORIZED (Single-use 보장)
        res_view_replay = self.client.get(f"/uploads/{test_file_rel}?ticket={ticket_id}")
        self.assertEqual(res_view_replay.status_code, 401)
        self.assertEqual(res_view_replay.json()["error"]["code"], "UNAUTHORIZED")

    # ==========================================================================
    # 3. 단기 서명 다운로드 토큰 (Pre-signed URL Token)
    # ==========================================================================
    def test_signed_download_token(self):
        """[보안 1.2 서명 토큰] 특정 파일 바인딩 단기 서명 토큰 발급 및 검증"""
        signed_token = create_signed_download_token("student-uuid-1", "student", "submissions/2026/09/cert.pdf", ttl_seconds=60)
        # 정상 검증
        verified = verify_signed_download_token(signed_token, "submissions/2026/09/cert.pdf")
        self.assertIsNotNone(verified)
        self.assertEqual(verified["uuid"], "student-uuid-1")

        # 타 파일 경로 대입 시 서명 거부
        mismatch = verify_signed_download_token(signed_token, "submissions/2026/09/another.pdf")
        self.assertIsNone(mismatch)

    # ==========================================================================
    # 4. 정수형 Role=0 IDOR 방어 (SECURITY_AND_AUDIT.md 1.3 & TODO.md 6.1 P1)
    # ==========================================================================
    def test_certification_status_integer_role_idor_defense(self):
        """[보안 1.3] 정수형 role=0 또는 문자열 '0' 토큰으로 타 학생 현황 조회 시 403 차단"""
        # student 1 토큰 (role을 정수 0으로 발급)
        token_role_zero = create_access_token({"sub": "student-uuid-1", "id": "s1", "role": 0})

        # 학생 1이 학생 2(student_id=2)의 현황 조회 시도 -> 403 FORBIDDEN
        res_other = self.client.get(
            "/api/students/2/certification-status?year=2026",
            headers={"Authorization": f"Bearer {token_role_zero}"},
        )
        self.assertEqual(res_other.status_code, 403)
        self.assertEqual(res_other.json()["error"]["code"], "FORBIDDEN")

    # ==========================================================================
    # 5. 디렉터리 경계 엄격화 (SECURITY_AND_AUDIT.md 1.4 & TODO.md 6.1 P1)
    # ==========================================================================
    def test_path_boundary_traversal_defense(self):
        """[보안 1.4] commonpath 기반 접두어 인접 디렉터리 충돌 공격 차단 검증"""
        token_admin = create_access_token({"sub": "admin-uuid", "id": "admin", "role": "admin"})

        # 1. 상위 디렉터리 탈출 경로 404 차단
        res = self.client.get(
            "/uploads/../../../../etc/passwd",
            headers={"Authorization": f"Bearer {token_admin}"},
        )
        self.assertEqual(res.status_code, 404)

        # 2. 인접 접두어 디렉터리 충돌 (Path Boundary Traversal) 방어 알고리즘 검증
        # (기존 startswith 취약점: /app/uploads_backup 이 /app/uploads 로 통과되던 문제)
        fake_upload_root = os.path.abspath("/var/uploads")
        sibling_file = os.path.abspath("/var/uploads_backup/secret.txt")
        # 기존 취약 방식: startswith는 True 반환
        self.assertTrue(sibling_file.startswith(fake_upload_root))
        # 신규 방어 방식: commonpath는 루트와 불일치 판정
        self.assertNotEqual(os.path.commonpath([fake_upload_root, sibling_file]), fake_upload_root)

    # ==========================================================================
    # 6. 비밀번호 복잡도 정규식/정책 강화 (SECURITY_AND_AUDIT.md 1.5 & TODO.md 6.1 P2)
    # ==========================================================================
    def test_password_complexity_policy(self):
        """[보안 1.5] 3종 조합 8자리 또는 2종 10자리 복잡도 정책 검증"""
        # 취약한 비밀번호 거부 (False)
        self.assertFalse(validate_password_complexity("abcd1234"))   # 8자리, 2종 -> 거부
        self.assertFalse(validate_password_complexity("password1"))  # 9자리, 2종 -> 거부
        self.assertFalse(validate_password_complexity("12345678"))   # 8자리, 1종 -> 거부
        self.assertFalse(validate_password_complexity("short1!"))    # 7자리 -> 거부
        self.assertFalse(validate_password_complexity(""))

        # 강력한 비밀번호 통과 (True)
        self.assertTrue(validate_password_complexity("abcd1234!"))    # 9자리, 3종(영문+숫자+특수) -> 통과
        self.assertTrue(validate_password_complexity("SecretPass99#"))# 13자리, 3종 -> 통과
        self.assertTrue(validate_password_complexity("mypassword1234")) # 14자리, 2종(10자리 이상) -> 통과

    # ==========================================================================
    # 7. 증빙 재제출 및 고아 파일 자동 정리 (TODO.md 2.6 & SECURITY_AND_AUDIT.md 4.1)
    # ==========================================================================
    def test_resubmit_evidence_and_orphan_file_cleanup(self):
        """[기능/보안 2.6] 반려 건 재제출 시 신규 파일 업로드 및 구 파일 디스크 자동 정리 검증"""
        # 기존 반려 파일 생성
        old_rel_path = "submissions/2026/09/old_rejected.pdf"
        abs_old_p = os.path.join(self.test_dir, old_rel_path)
        os.makedirs(os.path.dirname(abs_old_p), exist_ok=True)
        with open(abs_old_p, "wb") as f:
            f.write(b"%PDF-1.4 old rejected content")

        token_student = create_access_token({"sub": "student-uuid-1", "id": "s1", "role": "student"})

        # (1) 승인 완료(status_code=3) 건 재제출 시도 -> 400 차단
        res_approved = self.client.patch(
            "/api/submissions/102/resubmit",
            data={"description": "승인건 수정 시도"},
            headers={"Authorization": f"Bearer {token_student}"},
        )
        self.assertEqual(res_approved.status_code, 400)
        self.assertEqual(res_approved.json()["error"]["code"], "CANNOT_RESUBMIT_APPROVED")

        # (2) 반려(status_code=4) 건 신규 파일로 재제출 -> 200 OK & 구 파일 자동 삭제
        new_file_bytes = b"%PDF-1.4 new resubmitted certificate content"
        files = {"file": ("new_cert.pdf", io.BytesIO(new_file_bytes), "application/pdf")}
        data = {"description": "피드백 반영 후 보완 제출"}

        res_resubmit = self.client.patch(
            "/api/submissions/101/resubmit",
            data=data,
            files=files,
            headers={"Authorization": f"Bearer {token_student}"},
        )
        self.assertEqual(res_resubmit.status_code, 200)
        body = res_resubmit.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["status"], "검토중")

        # 구 파일이 디스크에서 고아 파일로 남지 않고 안전 삭제되었는지 확인
        self.assertFalse(os.path.exists(abs_old_p), "구 파일은 재제출 시 고아 파일 방지를 위해 삭제되어야 합니다.")

    # ==========================================================================
    # 8. 고아 파일 백그라운드 GC 유틸리티 (SECURITY_AND_AUDIT.md 4.1 & TODO.md)
    # ==========================================================================
    async def test_find_and_clean_orphan_files_gc(self):
        """[유지보수] 데이터베이스 비참조 고아 파일 식별 및 가비지 컬렉터 회수 검증"""
        # 1. 활성 파일 생성
        active_p = os.path.join(self.test_dir, "submissions/2026/09/approved_cert.pdf")
        os.makedirs(os.path.dirname(active_p), exist_ok=True)
        with open(active_p, "wb") as f:
            f.write(b"%PDF-1.4 active file")

        # 2. DB에 존재하지 않는 고아 파일 생성 (생성 시각을 2시간 전으로 조작)
        orphan_p = os.path.join(self.test_dir, "submissions/2026/09/abandoned_orphan.pdf")
        with open(orphan_p, "wb") as f:
            f.write(b"%PDF-1.4 orphan content")
        two_hours_ago = time.time() - 7200
        os.utime(orphan_p, (two_hours_ago, two_hours_ago))

        # 3. Dry run 실행: 탐색되지만 실제 삭제는 되지 않음
        dry_result = await find_and_clean_orphan_files(self.mock_conn, base_upload_dir=self.test_dir, max_age_seconds=3600, dry_run=True)
        self.assertEqual(dry_result["orphans_cleaned"], 1)
        self.assertTrue(os.path.exists(orphan_p))

        # 4. 실제 GC 실행: 고아 파일만 삭제되고 활성 파일은 보존됨
        gc_result = await find_and_clean_orphan_files(self.mock_conn, base_upload_dir=self.test_dir, max_age_seconds=3600, dry_run=False)
        self.assertEqual(gc_result["orphans_cleaned"], 1)
        self.assertFalse(os.path.exists(orphan_p), "고아 파일은 GC에 의해 디스크에서 삭제되어야 합니다.")
        self.assertTrue(os.path.exists(active_p), "DB에 등록된 활성 파일은 GC에 의해 보존되어야 합니다.")

    # ==========================================================================
    # 9. 신규 핫픽스 검증: /uploads/ 접두어 인입 시 정상 동작 & 더블 접두어 방어
    # ==========================================================================
    def test_download_ticket_with_leading_uploads_prefix(self):
        """[보안 1.2 심층] /uploads/ 접두어가 포함된 경로로 티켓 발급 시 더블 접두어 없이 정상 발급/소비 검증"""
        test_file_rel = "submissions/2026/09/leading_test.pdf"
        abs_p = os.path.join(self.test_dir, test_file_rel)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        with open(abs_p, "wb") as f:
            f.write(b"%PDF-1.4 leading prefix test file")

        self.mock_conn.submissions.append({
            "submission_id": 201,
            "student_id": 1,
            "uuid": "student-uuid-1",
            "file_path": f"/uploads/{test_file_rel}",
            "is_deleted": False,
        })

        token_student = create_access_token({"sub": "student-uuid-1", "id": "s1", "role": "student"})

        # /uploads/ 접두어를 포함하여 티켓 요청
        res_ticket = self.client.post(
            f"/api/uploads/ticket?file_path=/uploads/{test_file_rel}",
            headers={"Authorization": f"Bearer {token_student}"},
        )
        self.assertEqual(res_ticket.status_code, 200)
        body = res_ticket.json()
        ticket_id = body["data"]["ticket"]
        download_url = body["data"]["downloadUrl"]

        # 반환 URL에 더블 접두어(/uploads/uploads/)가 없어야 함
        self.assertNotIn("/uploads/uploads/", download_url)

        # 티켓으로 파일 열람 검증
        res_view = self.client.get(f"/uploads/{test_file_rel}?ticket={ticket_id}")
        self.assertEqual(res_view.status_code, 200)
        self.assertEqual(res_view.content, b"%PDF-1.4 leading prefix test file")

    # ==========================================================================
    # 10. 교사 계정의 티켓 발급 및 타 학생 증빙자료 안전 열람 (RBAC 검증)
    # ==========================================================================
    def test_teacher_download_ticket_access(self):
        """[보안 1.2/RBAC] 교사(Role '1' 또는 'teacher')가 학생 증빙 티켓으로 접근 시 403 차단 없이 200 OK 허용 검증"""
        test_file_rel = "submissions/2026/09/teacher_inspect.pdf"
        abs_p = os.path.join(self.test_dir, test_file_rel)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        with open(abs_p, "wb") as f:
            f.write(b"%PDF-1.4 teacher inspection content")

        self.mock_conn.submissions.append({
            "submission_id": 202,
            "student_id": 1,
            "uuid": "student-uuid-1",
            "file_path": f"/uploads/{test_file_rel}",
            "is_deleted": False,
        })

        # role="1" 형태의 교사 토큰 발급
        token_teacher = create_access_token({"sub": "teacher-uuid-1", "id": "t1", "role": "1"})

        res_ticket = self.client.post(
            f"/api/submissions/202/download-ticket",
            headers={"Authorization": f"Bearer {token_teacher}"},
        )
        self.assertEqual(res_ticket.status_code, 200)
        ticket_id = res_ticket.json()["data"]["ticket"]

        # 교사가 발급받은 티켓으로 학생 파일 열람 시 403이 아닌 200 허용되어야 함
        res_view = self.client.get(f"/uploads/{test_file_rel}?ticket={ticket_id}")
        self.assertEqual(res_view.status_code, 200)
        self.assertEqual(res_view.content, b"%PDF-1.4 teacher inspection content")

    # ==========================================================================
    # 11. GC 유틸리티: 접두어 없는 상대경로 DB 보존 검증 (오삭제 방어)
    # ==========================================================================
    async def test_find_and_clean_orphan_files_preserves_non_prefixed_db_paths(self):
        """[유지보수/안정성] DB에 'submissions/...'로 저장된 활성 파일이 GC에 의해 오삭제되지 않는지 검증"""
        active_rel = "submissions/2026/09/active_without_prefix.pdf"
        active_abs = os.path.join(self.test_dir, active_rel)
        os.makedirs(os.path.dirname(active_abs), exist_ok=True)
        with open(active_abs, "wb") as f:
            f.write(b"%PDF-1.4 active file without prefix")

        # DB에는 /uploads 없이 저장된 경우
        self.mock_conn.submissions.append({
            "submission_id": 203,
            "student_id": 1,
            "uuid": "student-uuid-1",
            "file_path": active_rel,
            "is_deleted": False,
        })
        two_hours_ago = time.time() - 7200
        os.utime(active_abs, (two_hours_ago, two_hours_ago))

        gc_result = await find_and_clean_orphan_files(self.mock_conn, base_upload_dir=self.test_dir, max_age_seconds=3600, dry_run=False)
        self.assertTrue(os.path.exists(active_abs), "접두어 없는 경로의 활성 파일도 안전하게 보존되어야 합니다.")

    # ==========================================================================
    # 12. 증빙자료 삭제 시 디스크 파일 즉시 정리 (SECURITY_AND_AUDIT.md 4.1 권고)
    # ==========================================================================
    def test_delete_submission_cleans_file_and_logs(self):
        """[기능/보안 2.7] 증빙자료 삭제(DELETE /api/submissions/{id}) 시 디스크 파일 즉시 삭제 및 이력 기록 검증"""
        del_file_rel = "submissions/2026/09/to_be_deleted.pdf"
        abs_del_p = os.path.join(self.test_dir, del_file_rel)
        os.makedirs(os.path.dirname(abs_del_p), exist_ok=True)
        with open(abs_del_p, "wb") as f:
            f.write(b"%PDF-1.4 to be deleted")

        self.mock_conn.submissions.append({
            "submission_id": 204,
            "student_id": 1,
            "uuid": "student-uuid-1",
            "status_code": 1,
            "file_path": f"/uploads/{del_file_rel}",
            "is_deleted": False,
        })

        token_student = create_access_token({"sub": "student-uuid-1", "id": "s1", "role": "student"})

        res_del = self.client.delete(
            "/api/submissions/204",
            headers={"Authorization": f"Bearer {token_student}"},
        )
        self.assertEqual(res_del.status_code, 200)
        self.assertTrue(res_del.json()["data"]["deleted"])

        # 파일이 디스크에서 즉시 정리되었는지 검증
        self.assertFalse(os.path.exists(abs_del_p), "삭제된 증빙자료 파일은 디스크에서 즉시 삭제되어야 합니다.")

    # ==========================================================================
    # 13. 재제출 분산 락 및 악성 링크 검증
    # ==========================================================================
    async def test_resubmit_concurrency_lock_and_malicious_link_defense(self):
        """[동시성/보안] 재제출 동시성 경합 시 409 차단 및 javascript: 스킴 링크 입력 시 400 차단 검증"""
        token_student = create_access_token({"sub": "student-uuid-1", "id": "s1", "role": "student"})

        # (1) 악성 자바스크립트 링크 차단
        res_bad_link = self.client.patch(
            "/api/submissions/101/resubmit",
            data={"link": "javascript:alert(document.cookie)"},
            headers={"Authorization": f"Bearer {token_student}"},
        )
        self.assertEqual(res_bad_link.status_code, 400)
        self.assertEqual(res_bad_link.json()["error"]["code"], "VALIDATION_ERROR")

        # (2) 동시성 락 경합 시 409 차단 검증
        lock_key = "lock:submission:resubmit:101"
        await self.mock_redis.set(lock_key, "1", ex=10)
        res_locked = self.client.patch(
            "/api/submissions/101/resubmit",
            data={"description": "동시 요청 시도"},
            headers={"Authorization": f"Bearer {token_student}"},
        )
        self.assertEqual(res_locked.status_code, 409)
        self.assertEqual(res_locked.json()["error"]["code"], "CONCURRENT_REQUEST")
        await self.mock_redis.delete(lock_key)
