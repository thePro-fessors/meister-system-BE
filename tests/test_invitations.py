"""
tests/test_invitations.py - 관리자 난수 초대코드 발급, 조회, 폐기 및 회원가입 연동 테스트
"""

import glob
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sp in glob.glob(os.path.join(_project_root, ".venv", "lib", "python*", "site-packages")):
    if os.path.exists(_sp) and _sp not in sys.path:
        sys.path.append(_sp)

from fastapi.testclient import TestClient

from core.security import create_access_token
from database import get_db, get_redis
from main import app


class MockRedisForInvitations:
    def __init__(self):
        self.data = {}
        self.sets = {}
        self.ttls = {}

    async def get(self, key):
        return self.data.get(key)

    async def getdel(self, key):
        val = self.data.pop(key, None)
        self.ttls.pop(key, None)
        return val

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
        return self.ttls.get(key, -1)

    async def delete(self, *keys):
        deleted = 0
        for k in keys:
            if k in self.data:
                del self.data[k]
                deleted += 1
            self.ttls.pop(k, None)
        return deleted

    async def sadd(self, key, *members):
        if key not in self.sets:
            self.sets[key] = set()
        count = 0
        for m in members:
            if m not in self.sets[key]:
                self.sets[key].add(m)
                count += 1
        return count

    async def smembers(self, key):
        return self.sets.get(key, set())

    async def srem(self, key, *members):
        if key not in self.sets:
            return 0
        count = 0
        for m in members:
            if m in self.sets[key]:
                self.sets[key].remove(m)
                count += 1
        return count


class TestInvitationCodes(unittest.TestCase):
    def setUp(self):
        self.mock_redis = MockRedisForInvitations()
        self.mock_conn = MagicMock()
        self.mock_cur = AsyncMock()

        self.mock_conn.cursor.return_value.__aenter__.return_value = self.mock_cur
        self.mock_conn.cursor.return_value.__aexit__.return_value = None
        self.mock_conn.autocommit = AsyncMock()
        self.mock_conn.commit = AsyncMock()
        self.mock_conn.rollback = AsyncMock()

        app.dependency_overrides[get_redis] = lambda: self.mock_redis
        app.dependency_overrides[get_db] = lambda: self.mock_conn

        self.client = TestClient(app)

        # 관리자 토큰
        self.admin_token = create_access_token(
            {"id": "admin", "role": "admin", "uuid": "admin-uuid-1", "user_id": "admin"}
        )
        # 교사 토큰
        self.teacher_token = create_access_token(
            {"id": "teacher1", "role": "teacher", "uuid": "teacher-uuid-1", "user_id": "teacher1"}
        )

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_create_invitation_code_admin_success(self):
        """관리자가 관리자(ADM), 2026학년도, 1일 유효, 1회 사용 초대코드 발급 성공 검증"""
        # mock current year 2026
        self.mock_cur.fetchone.return_value = {"year": 2026}

        res = self.client.post(
            "/api/admin/invitations",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"role": "admin", "validDays": 1, "maxUses": 1, "year": 2026},
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]

        # 규격: XXX-XXXX-XXXXXX 검증
        code = data["code"]
        parts = code.split("-")
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], "ADM")
        self.assertEqual(parts[1], "07EA")  # 2026 = 0x07EA
        self.assertEqual(len(parts[2]), 6)
        self.assertEqual(data["role"], "admin")
        self.assertEqual(data["max_uses"], 1)
        self.assertEqual(data["remaining_uses"], 1)

    def test_create_invitation_code_teacher_and_student(self):
        """교사(TCH) 및 학생(STU) 접두사 및 학년도 16진수 검증"""
        # 교사 코드 생성
        res_tch = self.client.post(
            "/api/admin/invitations",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"role": "교사", "valid_days": 3, "max_uses": 5, "year": 2026},
        )
        self.assertEqual(res_tch.status_code, 201)
        code_tch = res_tch.json()["data"]["code"]
        self.assertTrue(code_tch.startswith("TCH-07EA-"))
        self.assertEqual(res_tch.json()["data"]["max_uses"], 5)

        # 학생 코드 생성 (2025학년도 -> 07E9)
        res_stu = self.client.post(
            "/api/admin/invitations",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"role": "student", "year": 2025},
        )
        self.assertEqual(res_stu.status_code, 201)
        code_stu = res_stu.json()["data"]["code"]
        self.assertTrue(code_stu.startswith("STU-07E9-"))

    def test_create_invitation_forbidden_for_teacher(self):
        """교사 권한으로 초대코드 발급 시도 시 403 Forbidden 차단 검증"""
        res = self.client.post(
            "/api/admin/invitations",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"role": "admin"},
        )
        self.assertEqual(res.status_code, 403)

    def test_list_and_revoke_invitation_codes(self):
        """발급된 초대코드 목록 조회 및 취소(삭제) 검증"""
        # 1. 2개 발급
        res1 = self.client.post(
            "/api/admin/invitations",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"role": "teacher", "year": 2026},
        )
        res2 = self.client.post(
            "/api/admin/invitations",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"role": "student", "year": 2026},
        )
        code1 = res1.json()["data"]["code"]
        code2 = res2.json()["data"]["code"]

        # 2. 목록 조회
        list_res = self.client.get(
            "/api/admin/invitations",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        self.assertEqual(list_res.status_code, 200)
        invs = list_res.json()["data"]["invitations"]
        codes = [i["code"] for i in invs]
        self.assertIn(code1, codes)
        self.assertIn(code2, codes)

        # 3. code1 취소
        del_res = self.client.delete(
            f"/api/admin/invitations/{code1}",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        self.assertEqual(del_res.status_code, 200)
        self.assertEqual(del_res.json()["data"]["revokedCode"], code1)

        # 4. 취소 후 목록 재확인
        list_res2 = self.client.get(
            "/api/admin/invitations",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        codes2 = [i["code"] for i in list_res2.json()["data"]["invitations"]]
        self.assertNotIn(code1, codes2)
        self.assertIn(code2, codes2)

    def test_registration_with_invitation_code_and_decrement(self):
        """초대 코드를 이용한 회원가입 성공 및 사용 횟수 원자적 차감/소진 검증"""
        # 1. 관리자가 1회용 교사 초대 코드 발급
        res_create = self.client.post(
            "/api/admin/invitations",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"role": "teacher", "max_uses": 1, "year": 2026},
        )
        code = res_create.json()["data"]["code"]

        # 2. OTP 발송 (초대 코드 사용)
        self.mock_cur.fetchone.return_value = None  # 중복 없음
        otp_res = self.client.post(
            "/auth/send-otp",
            json={"email": "newteacher@school.kr", "admin_code": code, "is_it_student": False},
        )
        self.assertEqual(otp_res.status_code, 200)

        # 3. OTP 검증 (OTP 번호는 Redis에서 직접 획득)
        otp_val = self.mock_redis.data[f"otp:newteacher@school.kr"]
        verify_res = self.client.post(
            "/auth/verify-otp",
            json={"email": "newteacher@school.kr", "verify_code": otp_val},
        )
        self.assertEqual(verify_res.status_code, 200)
        reg_token = verify_res.json()["data"]["registerToken"]

        # 4. 회원가입 (초대 코드 전달)
        self.mock_cur.fetchone.return_value = None  # users 중복 없음
        self.mock_cur.rowcount = 1
        reg_res = self.client.post(
            "/auth/register",
            json={
                "email": "newteacher@school.kr",
                "id": "newteacher1",
                "password": "Password123!",
                "register_token": reg_token,
                "admin_code": code,
            },
        )
        self.assertEqual(reg_res.status_code, 200)
        self.assertEqual(reg_res.json()["data"]["role"], "teacher")

        # 5. 1회 사용 후 코드 소진 확인 (Redis에서 삭제되어야 함)
        self.assertNotIn(f"invitation_code:{code}", self.mock_redis.data)


if __name__ == "__main__":
    unittest.main()
