"""
tests/test_fe_compatibility.py - FE 연동 호환성 및 AI 서비스 엔드포인트 단위 테스트
"""

import os
import unittest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from main import app
from core.security import create_access_token
from database import get_redis


class TestFECompatibility(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_redis = MagicMock()
        async def _fake_redis_get(k):
            return None
        self.mock_redis.get = _fake_redis_get
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        self.teacher_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000002",
            "id": "teacher1",
            "role": "teacher",
        })
        self.student_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000001",
            "id": "student1",
            "role": "student",
        })

        self._orig_gemini_key = os.environ.get("GEMINI_API_KEY")
        self._orig_ai_key = os.environ.get("AI_API_KEY")
        if "GEMINI_API_KEY" in os.environ:
            del os.environ["GEMINI_API_KEY"]
        if "AI_API_KEY" in os.environ:
            del os.environ["AI_API_KEY"]

    def tearDown(self):
        app.dependency_overrides.clear()
        if self._orig_gemini_key is not None:
            os.environ["GEMINI_API_KEY"] = self._orig_gemini_key
        elif "GEMINI_API_KEY" in os.environ:
            del os.environ["GEMINI_API_KEY"]

        if self._orig_ai_key is not None:
            os.environ["AI_API_KEY"] = self._orig_ai_key
        elif "AI_API_KEY" in os.environ:
            del os.environ["AI_API_KEY"]

    def test_ai_classify_submission(self):
        """5.1.1 FE 증빙자료 제출 AI 자동 분류 엔드포인트 검증"""
        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={
                "detail": "정보처리기능사 필기 및 실기 취득",
                "description": "파이썬 및 SQL 코딩 실습 증빙",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertIn("recommendations", data["data"])
        self.assertEqual(data["data"]["bestMatch"]["area"], "전문기술역량")

    def test_ai_teacher_review_draft(self):
        """5.2.1 FE AiServicePanel 교사용 심사 Copilot 초안 엔드포인트 검증"""
        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={
                "submissionId": 10,
                "area": "외국어 능력",
                "detail": "TOEIC 850점 취득 증빙",
                "maxScore": 20.0,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["status"], "인정완료")
        self.assertEqual(data["data"]["recommendedScore"], 20.0)
        self.assertIn("teacherCommentDraft", data["data"])

    def test_ai_student_chat(self):
        """5.1.2 FE StudentChatbot 학생 챗봇 엔드포인트 검증"""
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "상벌점 반영 기준이 어떻게 되나요?"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertIn("reply", data["data"])
        self.assertIn("3월 1일", data["data"]["reply"])


if __name__ == "__main__":
    unittest.main()
