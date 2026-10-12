"""
tests/test_fe_audit_fixes.py - BE 검토 결과 및 수정 요구사항 (P0/P1/P2) 전수 검증 테스트
- B. P0: 학생 AI 대화 소유권 검증 및 IDOR 원자적 차단
- C. P1: 비활성 학년도 0, False, None DB 반환형 정규화 거부
- D. P1: 승인과 삭제 동시 실행 경합 방어(409) 및 증빙 파일 보존
- E. P1: 제출 상세/파일 다운로드/티켓 발급 권한 일치
- G. P1: 상벌점 PATCH DTO score alias 및 부호/범위 검증
- H. P2: 일회용 다운로드 티켓 Lua 스크립트 원자적 소비
"""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from core.security import (
    create_access_token,
    create_download_ticket,
    verify_and_consume_download_ticket,
)
from database import get_db, get_redis
from main import app
from sr_format import SrFormat


class MockAuditRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, seconds, val):
        self.data[key] = str(val)
        self.ttls[key] = seconds
        return True

    async def delete(self, key):
        self.data.pop(key, None)
        self.ttls.pop(key, None)

    async def getdel(self, key):
        val = self.data.pop(key, None)
        self.ttls.pop(key, None)
        return val

    async def eval(self, script, numkeys, *args):
        key = args[0]
        req_path = args[1] if len(args) > 1 else ""
        raw = self.data.get(key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            self.data.pop(key, None)
            return None
        expected = data.get("file_path", "").strip().lstrip("/\\")
        if expected.startswith("uploads/"):
            expected = expected[8:]
        expected = os.path.normpath(expected).replace("\\", "/")
        if expected == req_path:
            self.data.pop(key, None)
            self.ttls.pop(key, None)
            return raw
        else:
            return "PATH_MISMATCH"


class TestFEAuditFixes(unittest.TestCase):
    def setUp(self):
        self.mock_redis = MockAuditRedis()
        self.client = TestClient(app)

    # =========================================================================
    # B. P0: 대화 세션 소유권 원자적 검증 및 IDOR 격리
    # =========================================================================
    def test_student_chat_conversation_ownership_isolation(self):
        """[B. P0] 타 학생의 conversationId로 요청 시 403 FORBIDDEN 차단 검증"""
        # mock DB: 대화 conv-alice는 student-alice 소유
        class MockDBConv:
            def __init__(self):
                self.conversations = [
                    {"conversation_id": "conv-alice", "user_uuid": "student-alice", "title": "앨리스 대화"}
                ]
                self.messages = []

            def cursor(self, **kwargs):
                return MockDBCursor(self)

            async def commit(self):
                pass

        class MockDBCursor:
            def __init__(self, db):
                self.db = db
                self.result = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def execute(self, query, params=None):
                q = query.strip()
                if "SELECT user_uuid FROM chat_conversations" in q:
                    cid = params[0]
                    matched = [c for c in self.db.conversations if c["conversation_id"] == cid]
                    self.result = matched
                elif "SELECT student_id, name, email FROM students" in q:
                    self.result = [{"student_id": 1, "name": "밥", "email": "bob@bssm.hs.kr"}]
                elif "SELECT sender_role, content FROM chat_messages" in q:
                    self.result = []
                elif "INSERT INTO chat_messages" in q:
                    pass

            async def fetchone(self):
                return self.result[0] if self.result else None

            async def fetchall(self):
                return self.result

        mock_db = MockDBConv()
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        token_bob = create_access_token({"sub": "student-bob", "id": "bob", "role": "student"})

        # 학생 밥(bob)이 앨리스(alice) 소유의 conversationId 접근 시도
        res = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {token_bob}"},
            json={"message": "대화 내용 탈취 시도", "conversationId": "conv-alice"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "FORBIDDEN_CONVERSATION_ACCESS")

        app.dependency_overrides.clear()

    # =========================================================================
    # C. P1: 비활성 학년도 반환형 0, False, None 거부
    # =========================================================================
    def test_inactive_academic_year_normalization_rejection(self):
        """[C. P1] DB에서 year_is_activated가 0, False, None으로 반환될 때 400 거부 검증"""
        # MySQL TINYINT(1) 정수 0 및 False, None 케이스
        for inactive_val in [0, "0", False, None]:
            class MockDBInactiveYear:
                def cursor(self, **kwargs):
                    return self

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def execute(self, q, params=None):
                    self.q = q

                async def fetchone(self):
                    if "FROM students" in self.q:
                        return {"student_id": 1, "uuid": "student-1", "name": "학생1", "email": "s1@bssm.hs.kr", "is_deleted": 0}
                    if "FROM evaluation_items" in self.q:
                        return {
                            "item_id": 10,
                            "area_id": 1,
                            "item_name": "테스트 항목",
                            "target_grade": 1,
                            "item_max_score": 10.0,
                            "scoring_type": 1,
                            "requires_evidence": False,
                            "is_active": 1,
                            "academic_year": 2026,
                            "year_is_activated": inactive_val,
                        }
                    return None

            app.dependency_overrides[get_db] = lambda: MockDBInactiveYear()
            app.dependency_overrides[get_redis] = lambda: self.mock_redis

            token_student = create_access_token({"sub": "student-1", "id": "s1", "role": "student"})
            res = self.client.post(
                "/api/submissions",
                headers={"Authorization": f"Bearer {token_student}"},
                data={
                    "itemId": 10,
                    "detail": "상세 활동",
                    "activityDate": "2026-04-01",
                    "description": "비활성 학년도 제출 시도",
                },
            )
            self.assertEqual(res.status_code, 400, f"Failed for inactive_val={inactive_val}")
            self.assertEqual(res.json()["error"]["code"], "ACADEMIC_YEAR_INACTIVE")

        app.dependency_overrides.clear()

    # =========================================================================
    # D. P1: 승인과 삭제 동시 실행 경합 409 및 증빙 보존
    # =========================================================================
    def test_delete_submission_atomic_conflict_409(self):
        """[D. P1] 삭제 트랜잭션 시 이미 승인(status=3)된 행이면 UPDATE 0행 영향으로 409 CONFLICT 반환"""
        class MockDBConflict:
            def __init__(self):
                self.rowcount = 0  # 조건부 UPDATE 실패 시뮬레이션

            def cursor(self, **kwargs):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def autocommit(self, val):
                pass

            async def rollback(self):
                pass

            async def commit(self):
                pass

            async def execute(self, q, params=None):
                self.last_query = q
                if "SELECT s.submission_id" in q:
                    self.row = {
                        "submission_id": 100,
                        "student_id": 1,
                        "status_code": 1,  # 조회 시점에는 1이었으나
                        "granted_score": None,
                        "file_path": "/uploads/test.pdf",
                    }
                elif "UPDATE submissions" in q:
                    # UPDATE 시점에 다른 트랜잭션이 이미 승인완료(3)로 변경하여 rowcount = 0
                    self.rowcount = 0

            async def fetchone(self):
                return getattr(self, "row", None)

        mock_db = MockDBConflict()
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        token_student = create_access_token({"sub": "student-1", "id": "s1", "role": "student"})
        with patch("core.authorization.authorize_submission_access", new=AsyncMock(return_value=None)):
            res = self.client.delete(
                "/api/submissions/100",
                headers={"Authorization": f"Bearer {token_student}"},
            )
            self.assertEqual(res.status_code, 409)
            self.assertEqual(res.json()["error"]["code"], "CONFLICT")

        app.dependency_overrides.clear()

    # =========================================================================
    # G. P1: 상벌점 PATCH DTO score alias 및 부호/범위 정규화
    # =========================================================================
    def test_patch_point_score_alias_and_normalization(self):
        """[G. P1] PATCH /api/points/{pointId} 에서 score 필드 및 부호 처리 검증"""
        updated_points = {}

        class MockDBPoint:
            def cursor(self, **kwargs):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def autocommit(self, val):
                pass

            async def commit(self):
                pass

            async def rollback(self):
                pass

            async def execute(self, q, params=None):
                if "SELECT merits_point_id" in q:
                    self.row = {
                        "merits_point_id": 5,
                        "student_id": 1,
                        "type": "+",
                        "points": 5.0,
                        "reason": "원래 사유",
                        "related_area": "인성",
                        "occurred_at": "2026-04-01",
                    }
                elif "UPDATE merits" in q:
                    updated_points["type"] = params[0]
                    updated_points["points"] = params[1]
                    updated_points["reason"] = params[2]
                elif "INSERT INTO merits_log" in q:
                    pass

            async def fetchone(self):
                return getattr(self, "row", None)

        app.dependency_overrides[get_db] = lambda: MockDBPoint()
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        token_teacher = create_access_token({"sub": "teacher-1", "id": "t1", "role": "teacher"})
        with patch("core.authorization.authorize_student_access", new=AsyncMock(return_value=None)):
            # 1. FE score 필드로 양수 10점 수정
            res1 = self.client.patch(
                "/api/points/5",
                headers={"Authorization": f"Bearer {token_teacher}"},
                json={"score": 10.0, "reason": "우수 활동 점수 상향"},
            )
            self.assertEqual(res1.status_code, 200)
            self.assertEqual(updated_points["points"], 10.0)
            self.assertEqual(updated_points["type"], "+")

            # 2. FE score 필드로 음수 벌점(-15) 수정
            res2 = self.client.patch(
                "/api/points/5",
                headers={"Authorization": f"Bearer {token_teacher}"},
                json={"score": -15.0, "reason": "지각 벌점 반영"},
            )
            self.assertEqual(res2.status_code, 200)
            self.assertEqual(updated_points["points"], -15.0)
            self.assertEqual(updated_points["type"], "-")

            # 3. 절댓값 범위 초과(150점) -> 400 거부
            res3 = self.client.patch(
                "/api/points/5",
                headers={"Authorization": f"Bearer {token_teacher}"},
                json={"score": 150.0, "reason": "한도 초과 점수"},
            )
            self.assertEqual(res3.status_code, 400)
            self.assertEqual(res3.json()["error"]["code"], "VALIDATION_ERROR")

        app.dependency_overrides.clear()

    # =========================================================================
    # H. P2: 일회용 다운로드 티켓 Lua 스크립트 원자적 소비
    # =========================================================================
    def test_download_ticket_atomic_lua_single_use(self):
        """[H. P2] Lua 스크립트를 통한 다운로드 티켓 1회 원자적 소비 및 Replay Attack 차단"""
        async def run_ticket_test():
            ticket_id = await create_download_ticket(
                user_uuid="user-1",
                role="teacher",
                file_path="submissions/2026/09/sample.pdf",
                redis=self.mock_redis,
                ttl_seconds=60,
            )

            # 1. 경로 불일치 요청 -> 티켓 미소비 및 거부 (None)
            mismatch = await verify_and_consume_download_ticket(
                ticket_id=ticket_id,
                requested_file_path="submissions/2026/09/other.pdf",
                redis=self.mock_redis,
            )
            self.assertIsNone(mismatch)

            # 2. 올바른 경로로 최초 소비 -> 원자적 성공 및 삭제
            first_consume = await verify_and_consume_download_ticket(
                ticket_id=ticket_id,
                requested_file_path="submissions/2026/09/sample.pdf",
                redis=self.mock_redis,
            )
            self.assertIsNotNone(first_consume)
            self.assertEqual(first_consume["uuid"], "user-1")

            # 3. 2차 동시/재요청 시도 -> 이미 원자적으로 삭제되어 실패 (None)
            second_consume = await verify_and_consume_download_ticket(
                ticket_id=ticket_id,
                requested_file_path="submissions/2026/09/sample.pdf",
                redis=self.mock_redis,
            )
            self.assertIsNone(second_consume)

        asyncio.run(run_ticket_test())


if __name__ == "__main__":
    unittest.main()
