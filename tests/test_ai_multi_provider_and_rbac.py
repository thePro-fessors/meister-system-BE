import unittest
from unittest.mock import AsyncMock, patch, MagicMock
import os
import json
from fastapi.testclient import TestClient

from main import app
from database import get_db, get_redis
from routers.ai import get_ai_db
from core.security import create_access_token
from core.ai_provider import (
    resolve_ai_config,
    call_multi_provider_llm,
    AIConfig,
    LLMResult,
    _call_openai_rest,
    _call_claude_rest,
)


class TestAIMultiProviderUnit(unittest.IsolatedAsyncioTestCase):
    """Multi-Provider AI 기능 단위 테스트"""

    def test_resolve_ai_config_defaults(self):
        """환경변수가 없을 때 기본 Gemini 설정 확인"""
        with patch.dict(os.environ, {}, clear=True):
            cfg = resolve_ai_config("teacher")
            self.assertEqual(cfg.provider, "gemini")
            self.assertIn("gemini-3.8-flash", cfg.model)

    def test_resolve_ai_config_role_separation(self):
        """역할별 AI 설정 분리 검증 (ADMIN_AI_* vs TEACHER_AI_*)"""
        env = {
            "ADMIN_AI_PROVIDER": "openai",
            "ADMIN_AI_API_KEY": "sk-admin-key",
            "ADMIN_AI_MODEL": "gpt-4o",
            "TEACHER_AI_PROVIDER": "claude",
            "TEACHER_AI_API_KEY": "sk-ant-teacher-key",
            "TEACHER_AI_MODEL": "claude-3-5-sonnet-20241022",
        }
        with patch.dict(os.environ, env, clear=True):
            admin_cfg = resolve_ai_config("admin")
            self.assertEqual(admin_cfg.provider, "openai")
            self.assertEqual(admin_cfg.api_key, "sk-admin-key")
            self.assertEqual(admin_cfg.model, "gpt-4o")

            teacher_cfg = resolve_ai_config("teacher")
            self.assertEqual(teacher_cfg.provider, "claude")
            self.assertEqual(teacher_cfg.api_key, "sk-ant-teacher-key")
            self.assertEqual(teacher_cfg.model, "claude-3-5-sonnet-20241022")

    def test_resolve_ai_config_openai_compatible_base_url(self):
        """AI_BASE_URL 지정 시 openai_compatible 공급자 자동 인식"""
        env = {
            "AI_BASE_URL": "http://localhost:8000/v1",
            "OPENAI_API_KEY": "local-key",
            "OPENAI_MODEL": "meta-llama/Meta-Llama-3-8B-Instruct",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = resolve_ai_config("teacher")
            self.assertEqual(cfg.provider, "openai_compatible")
            self.assertEqual(cfg.base_url, "http://localhost:8000/v1")
            self.assertEqual(cfg.model, "meta-llama/Meta-Llama-3-8B-Instruct")

    @patch("core.ai_provider.httpx.AsyncClient")
    async def test_call_openai_compatible_api_success(self, mock_client_cls):
        """OpenAI-compatible 엔드포인트 비동기 호출 성공 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"category": "전공", "confidence": 0.95}'
                    }
                }
            ]
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        cfg = AIConfig(
            provider="openai_compatible",
            api_key="sk-test",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
        )
        res, err = await _call_openai_rest(
            prompt="테스트",
            system_instruction="지침",
            config=cfg,
            response_json=True,
            is_compatible=True,
        )
        self.assertIsNone(err)
        self.assertIsNotNone(res)
        self.assertIn("category", res)

    @patch("core.ai_provider.httpx.AsyncClient")
    async def test_call_claude_api_success(self, mock_client_cls):
        """Anthropic Claude 엔드포인트 비동기 호출 성공 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "content": [
                {
                    "type": "text",
                    "text": "클로드 분석 결과입니다."
                }
            ]
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        cfg = AIConfig(
            provider="claude",
            api_key="sk-ant-test",
            model="claude-3-5-sonnet",
        )
        res, err = await _call_claude_rest(
            prompt="테스트 프롬프트",
            system_instruction="지침",
            config=cfg,
        )
        self.assertIsNone(err)
        self.assertEqual(res, "클로드 분석 결과입니다.")

    async def test_call_multi_provider_llm_metadata_contract(self):
        """LLM 호출 결과의 메타데이터 표준 규격(to_metadata) 검증"""
        # 1. 성공 시
        res_success = LLMResult(
            text="정상 응답",
            provider="openai",
            model="gpt-4o-mini",
            is_fallback=False,
        )
        meta_s = res_success.to_metadata()
        self.assertEqual(meta_s["analysisSource"], "openai")
        self.assertFalse(meta_s["fallbackUsed"])
        self.assertEqual(meta_s["capabilityMode"], "live_llm")

        # 2. 폴백 시
        res_fallback = LLMResult(
            text=None,
            provider="gemini",
            model="gemini-2.5-flash",
            is_fallback=True,
            fallback_reason="API timeout",
        )
        meta_f = res_fallback.to_metadata()
        self.assertEqual(meta_f["analysisSource"], "rule_based_fallback")
        self.assertTrue(meta_f["fallbackUsed"])
        self.assertEqual(meta_f["capabilityMode"], "rule_based_engine")


class MockSimpleCursor:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql: str, params: tuple = ()):
        self.sql = sql
        self.params = params

    async def fetchone(self):
        if hasattr(self, "sql") and "FROM submissions" in self.sql:
            return {
                "submission_id": 1,
                "student_id": 10,
                "student_uuid": "st-uuid-10",
                "is_deleted": False,
                "status_code": 3,
                "item_id": 1,
            }
        return None

    async def fetchall(self):
        return []


class MockSimpleDB:
    def cursor(self, cursor=None):
        return MockSimpleCursor(self)

    async def commit(self):
        pass


class TestRBACExcelDecisions(unittest.TestCase):
    """Excel 0_의심항목_체크리스트_요약 결정사항 검증"""

    def setUp(self):
        self.mock_db = MockSimpleDB()
        self.mock_redis = AsyncMock()
        self.mock_redis.get.return_value = None
        self.mock_redis.incr.return_value = 1
        self.mock_redis.expire.return_value = True
        self.mock_redis.ttl.return_value = 60

        app.dependency_overrides[get_db] = lambda: self.mock_db
        app.dependency_overrides[get_ai_db] = lambda: self.mock_db
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_suspicious_12_teacher_blocked_on_student_classify(self):
        """[의심-12 결정 B] 학생 전용 AI 자동분류 API에 교사 접근 시 403 FORBIDDEN 차단"""
        teacher_token = create_access_token({"sub": "t-1", "role": "teacher"})
        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {teacher_token}"},
            json={"title": "정보처리기능사 취득", "content": "합격증 제출"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN_ROLE")

    def test_suspicious_12_student_allowed_on_student_classify(self):
        """[의심-12 결정 B] 학생 계정은 AI 자동분류 호출 성공 (폴백 또는 AI)"""
        student_token = create_access_token({"sub": "s-1", "role": "student"})
        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {student_token}"},
            json={"title": "정보처리기능사 취득", "content": "합격증 제출"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        data = resp.json()["data"]
        self.assertIn("analysisSource", data)
        self.assertIn("fallbackUsed", data)

    def test_suspicious_10_teacher_cannot_delete_submission(self):
        """[의심-10 결정 A] 교사는 증빙자료 직접 삭제 불가 (403 FORBIDDEN)"""
        teacher_token = create_access_token({"sub": "t-1", "role": "teacher"})
        resp = self.client.delete(
            "/api/submissions/1",
            headers={"Authorization": f"Bearer {teacher_token}"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

    def test_suspicious_12_teacher_blocked_on_student_chat(self):
        """[의심-12 결정 B] 학생 맞춤형 챗봇 API에 교사 접근 시 403 FORBIDDEN 차단"""
        teacher_token = create_access_token({"sub": "t-1", "role": "teacher"})
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {teacher_token}"},
            json={"message": "역량인증제 기준 안내해줘"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN_ROLE")

    def test_suspicious_12_admin_allowed_on_student_chat(self):
        """[의심-12 결정 B] 관리자는 테스트 목적으로 학생 맞춤형 챗봇 API 호출 허용 (200 OK)"""
        admin_token = create_access_token({"sub": "admin-1", "role": "admin"})
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"message": "역량인증제 기준 안내해줘"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.assertIn("reply", resp.json()["data"])
        self.assertIn("analysisSource", resp.json()["data"])

    def test_suspicious_12_admin_allowed_on_student_classify(self):
        """[의심-12 결정 B] 관리자는 테스트 목적으로 AI 자동분류 호출 허용 (200 OK)"""
        admin_token = create_access_token({"sub": "admin-1", "role": "admin"})
        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"title": "정보처리기능사 취득", "content": "합격증 제출"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    def test_ai_fallback_metadata_consistency_on_error(self):
        """AI 에러 또는 타임아웃 시 analysisSource가 rule_based_fallback 및 fallbackUsed=True로 일치하는지 검증"""
        student_token = create_access_token({"sub": "s-1", "role": "student"})
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {student_token}"},
            json={"message": "미달성 항목 조회"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["analysisSource"], "rule_based_fallback")
        self.assertTrue(data["fallbackUsed"])
        self.assertEqual(data["capabilityMode"], "rule_based_engine")


if __name__ == "__main__":
    unittest.main()
