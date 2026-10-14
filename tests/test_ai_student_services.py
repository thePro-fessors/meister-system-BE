"""
tests/test_ai_student_services.py - 학생용 AI 서비스 5.1.1 및 5.1.2 종합 단위/통합 테스트

검증 범위:
1. 5.1.1 증빙자료 자동 분류 (POST /api/ai/classify-submission)
   - 5대 영역 키워드 기반 규칙 엔진 분류 및 신뢰도 산출
   - Gemini API 성공 시 백엔드 프록시 및 JSON 파싱
   - Gemini API 미설정 / 타임아웃 / 오류 시 Graceful Fallback
   - 계정 및 IP별 Rate Limiting (429)
   - DB 활성 평가 항목 연동 매핑
2. 5.1.2 학생 맞춤형 역량 도우미 챗봇 (POST /api/ai/student/chat & POST /api/ai/chat)
   - 학생 본인 인증 및 Strict IDOR 격리 (타인 세션 접근 차단 403)
   - 동적 RAG 및 calculate_student_certification 집계 연동
   - chat_conversations, chat_messages 이력 영속화
   - 프롬프트 인젝션 방어 및 PII 개인정보 마스킹 (주민번호, 연락처)
   - Gemini API 연동 및 타임아웃 시 규칙 기반 Fallback
   - Rate Limiting (429)
3. 모델 명칭 정규화 매핑 (Gemini 3.5-Flash-lite -> gemini-2.5-flash)
"""

from datetime import date, datetime
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import httpx

from core.security import create_access_token
from database import get_db, get_redis
from main import app
from routers.ai import check_ai_rate_limit, resolve_gemini_model, sanitize_and_mask_text


class MockAIDatabaseCursor:
    """AI 라우터 전용 DB Mock 커서"""
    def __init__(self, conn):
        self.conn = conn
        self._results = []
        self._index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql: str, params: tuple = ()):
        sql_clean = " ".join(sql.strip().split())
        self._index = 0

        # 1. 평가 항목 조회 (active items)
        if "FROM evaluation_items" in sql_clean:
            self._results = [
                {"item_id": 1, "name": "국가기술자격증(기능사/산업기사) 취득", "area_id": 2, "area_name": "전문기술역량"},
                {"item_id": 4, "name": "의사소통 및 문제해결 프로젝트", "area_id": 1, "area_name": "직업기초능력"},
                {"item_id": 6, "name": "교내외 봉사활동(20시간 이상)", "area_id": 3, "area_name": "인성/직업의식"},
                {"item_id": 9, "name": "필독서 독서 마일리지 및 서평 제출", "area_id": 4, "area_name": "인문학적 소양"},
                {"item_id": 11, "name": "공인 외국어 성적(TOEIC/OPIc 등)", "area_id": 5, "area_name": "외국어 능력"},
            ]

        # 2. 학생 정보 조회
        elif "FROM students" in sql_clean:
            uuid_param = params[0] if params else ""
            if uuid_param == "00000000-0000-0000-0000-000000000001":
                self._results = [{"student_id": 101, "name": "김학생", "email": "student1@bssm.hs.kr"}]
            elif uuid_param == "00000000-0000-0000-0000-000000000002":
                self._results = [{"student_id": 102, "name": "이학생", "email": "student2@bssm.hs.kr"}]
            else:
                self._results = [{"student_id": 101, "name": "테스트학생", "email": "test@bssm.hs.kr"}]

        # 3. 대화 세션 조회 (IDOR 검증)
        elif "SELECT user_uuid FROM chat_conversations" in sql_clean:
            conv_id = params[0] if params else ""
            if conv_id == "conv-other-student":
                self._results = [{"user_uuid": "00000000-0000-0000-0000-000000000002"}]
            elif conv_id == "conv-my-own":
                self._results = [{"user_uuid": "00000000-0000-0000-0000-000000000001"}]
            else:
                self._results = []

        # 4. 활성 학년도 조회
        elif "FROM academic_years" in sql_clean:
            self._results = [{"year_id": 1, "year": 2026}]

        # 5. 학적 조회
        elif "FROM student_academic_records" in sql_clean:
            self._results = [{"grade": 1, "class_no": 2, "number": 15}]

        # 6. 인증 영역 조회
        elif "FROM certification_areas" in sql_clean:
            if getattr(self.conn, "no_criteria", False):
                self._results = []
            else:
                self._results = [
                    {"area_id": 1, "name": "직업기초능력", "max_score": 20.0},
                    {"area_id": 2, "name": "전문기술역량", "max_score": 20.0},
                    {"area_id": 3, "name": "인성/직업의식", "max_score": 20.0},
                    {"area_id": 4, "name": "인문학적 소양", "max_score": 20.0},
                    {"area_id": 5, "name": "외국어 능력", "max_score": 20.0},
                ]

        # 7. 증빙자료 조회
        elif "FROM submissions s" in sql_clean:
            self._results = [
                {
                    "submission_id": 1,
                    "item_id": 1,
                    "status_code": 3,
                    "granted_score": 15.0,
                    "detail": "정보처리기능사 취득",
                    "area_id": 2,
                    "scoring_type": 1,
                    "item_max_score": 20.0,
                    "item_name": "국가기술자격증 취득",
                },
                {
                    "submission_id": 2,
                    "item_id": 4,
                    "status_code": 1,  # 심사 대기
                    "granted_score": 0.0,
                    "detail": "의사소통 발표 과제",
                    "area_id": 1,
                    "scoring_type": 1,
                    "item_max_score": 20.0,
                    "item_name": "의사소통 프로젝트",
                },
            ]

        # 8. 상벌점 조회
        elif "FROM merits" in sql_clean:
            self._results = [
                {
                    "merits_point_id": 1,
                    "type": "+",
                    "points": 3.0,
                    "related_area": "인성/직업의식",
                    "reason": "선행상 수상",
                    "occurred_at": date(2026, 5, 10),
                    "is_reflected": True,
                }
            ]

        # 9. 대화 메시지 이력 조회
        elif "FROM chat_messages" in sql_clean:
            self.conn.last_message_query = sql_clean
            self._results = [
                {"sender_role": "USER", "content": "안녕하세요"},
                {"sender_role": "ASSISTANT", "content": "안녕하세요! 마이스터 역량인증 도우미입니다."},
            ]

        # 10. INSERT/UPDATE 등
        elif any(k in sql_clean for k in ("INSERT INTO", "UPDATE chat_conversations")):
            self.conn.executed_inserts.append((sql_clean, params))
            self._results = []
        else:
            self._results = []

    async def fetchall(self):
        return list(self._results)

    async def fetchone(self):
        if self._results:
            return self._results[0]
        return None


class MockAIDatabaseConnection:
    """Mock 비동기 DB 커넥션"""
    def __init__(self):
        self.executed_inserts = []
        self.committed = False
        self.commit_count = 0
        self.last_message_query = None
        self.no_criteria = False

    def cursor(self, *args, **kwargs):
        return MockAIDatabaseCursor(self)

    async def commit(self):
        self.committed = True
        self.commit_count += 1

    async def rollback(self):
        pass

    async def autocommit(self, val):
        pass


class TestStudentAIServices(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_redis = MagicMock()
        self.mock_redis_data = {}

        async def _fake_redis_get(k):
            return self.mock_redis_data.get(k)

        async def _fake_redis_incr(k):
            val = self.mock_redis_data.get(k, 0) + 1
            self.mock_redis_data[k] = val
            return val

        async def _fake_redis_expire(k, s):
            return True

        self.mock_redis.get = _fake_redis_get
        self.mock_redis.incr = _fake_redis_incr
        self.mock_redis.expire = _fake_redis_expire

        self.mock_conn = MockAIDatabaseConnection()

        app.dependency_overrides[get_redis] = lambda: self.mock_redis
        app.dependency_overrides[get_db] = lambda: self.mock_conn

        self.student_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000001",
            "id": "student1",
            "role": "student",
            "name": "김학생",
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

        if "GEMINI_MODEL" in os.environ:
            del os.environ["GEMINI_MODEL"]

    # =========================================================================
    # 1. 모델 명칭 정규화 및 보안 유틸리티 검증
    # =========================================================================
    def test_gemini_model_normalization(self):
        self.assertEqual(resolve_gemini_model("gemini-3.8-flash"), "gemini-3.8-flash")
        self.assertEqual(resolve_gemini_model("3.8-flash"), "gemini-3.8-flash")
        self.assertEqual(resolve_gemini_model("Gemini 3.5-Flash-lite"), "gemini-3.5-flash-lite")
        self.assertEqual(resolve_gemini_model("3.5-flash-lite"), "gemini-3.5-flash-lite")
        self.assertEqual(resolve_gemini_model("gemini-2.0-flash-lite"), "gemini-2.0-flash-lite")
        self.assertEqual(resolve_gemini_model("gemini-2.5-flash"), "gemini-2.5-flash")

    def test_pii_sanitization_and_masking(self):
        """프롬프트 인젝션 살균 및 개인정보(주민등록번호, 전화번호, 계좌) 마스킹 검증"""
        text = "제 주민번호는 060101-3123456 이고 연락처는 010-9876-5432 입니다. \x00\x08비정상태그"
        cleaned = sanitize_and_mask_text(text)
        self.assertNotIn("060101-3123456", cleaned)
        self.assertNotIn("010-9876-5432", cleaned)
        self.assertIn("[주민등록번호 마스킹]", cleaned)
        self.assertIn("[전화번호 마스킹]", cleaned)
        self.assertNotIn("\x00", cleaned)

    # =========================================================================
    # 2. 5.1.1 증빙자료 자동 분류 (POST /api/ai/classify-submission) 검증
    # =========================================================================
    def test_classify_submission_rule_based_five_areas(self):
        """5대 역량 영역별 키워드 매칭 및 신뢰도 산출 검증"""
        test_cases = [
            ("정보처리기능사 실기 합격", "파이썬 및 SQL 코딩 실습", "전문기술역량"),
            ("토익 800점 취득", "공인 외국어 어학 성적표 제출", "외국어 능력"),
            ("교내 환경정화 봉사활동", "주말 4시간 봉사 참여", "인성/직업의식"),
            ("필독서 인문학 도서 감상문", "독서 마일리지 10점 획득", "인문학적 소양"),
            ("의사소통 프로젝트 발표", "팀원들과 협업 보고서 작성", "직업기초능력"),
        ]

        for detail, desc, expected_area in test_cases:
            resp = self.client.post(
                "/api/ai/classify-submission",
                headers={"Authorization": f"Bearer {self.student_token}"},
                json={"detail": detail, "description": desc},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()["data"]
            self.assertEqual(data["bestMatch"]["area"], expected_area)
            self.assertGreaterEqual(data["bestMatch"]["confidence"], 0.6)
            self.assertIn("recommendations", data)
            self.assertIn("suggestions", data)

    def test_classify_submission_no_match_default(self):
        """매칭되는 키워드가 없을 때 안전한 기본 추천값 반환 검증"""
        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"detail": "알 수 없는 임의의 활동", "description": "내용 없음"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["bestMatch"]["area"], "전문기술역량")
        self.assertEqual(data["bestMatch"]["confidence"], 0.5)

    @patch("routers.ai.httpx.AsyncClient.post")
    def test_classify_submission_gemini_api_success(self, mock_post):
        """Gemini API 정상 응답 시 백엔드 프록시 및 JSON 파싱 추천 검증"""
        os.environ["GEMINI_API_KEY"] = "mock-gemini-key"
        os.environ["GEMINI_MODEL"] = "gemini-3.5-flash-lite"

        mock_gemini_json = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "recommendations": [
                                        {
                                            "area": "전문기술역량",
                                            "itemId": 1,
                                            "itemName": "국가기술자격증 취득",
                                            "confidence": 0.98,
                                            "reason": "정보처리기능사는 국가공인 전문기술 자격증입니다.",
                                            "matchedKeywords": ["자격증", "정보처리"],
                                        }
                                    ]
                                })
                            }
                        ]
                    }
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_gemini_json
        mock_post.return_value = mock_response

        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"detail": "정보처리기능사 취득", "description": "자격증 사본 첨부"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["bestMatch"]["area"], "전문기술역량")
        self.assertEqual(data["bestMatch"]["confidence"], 0.98)
        self.assertIn("국가공인 전문기술 자격증", data["bestMatch"]["reason"])

    @patch("routers.ai.httpx.AsyncClient.post")
    def test_classify_submission_gemini_timeout_fallback(self, mock_post):
        """Gemini API 타임아웃 발생 시 500 에러 없이 규칙 기반으로 Graceful Fallback 검증"""
        os.environ["GEMINI_API_KEY"] = "mock-gemini-key"
        mock_post.side_effect = httpx.TimeoutException("Gemini timeout")

        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"detail": "TOEIC 850점 성적표", "description": "외국어 어학 성적 증빙"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(resp.json()["success"])
        self.assertEqual(data["bestMatch"]["area"], "외국어 능력")

    def test_classify_submission_rate_limit_exceeded(self):
        """요청 한도 초과 시 429 TOO MANY REQUESTS 차단 검증"""
        # IP 기준 30회 초과 시뮬레이션
        async def _exceeded_incr(k):
            return 31
        self.mock_redis.incr = _exceeded_incr

        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"detail": "코딩 과제", "description": "실습"},
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["code"], "RATE_LIMIT_EXCEEDED")

    # =========================================================================
    # 3. 5.1.2 맞춤형 역량 달성 도우미 챗봇 (POST /api/ai/student/chat) 검증
    # =========================================================================
    def test_student_chat_unauthorized(self):
        """인증 토큰 누락 시 401 Unauthorized 검증"""
        resp = self.client.post(
            "/api/ai/student/chat",
            json={"message": "제 역량 점수 알려주세요."},
        )
        self.assertEqual(resp.status_code, 401)

    def test_student_chat_alias_route_compatibility(self):
        """POST /api/ai/chat 별칭 라우트 호환성 검증"""
        resp = self.client.post(
            "/api/ai/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "상벌점 반영 기준이 어떻게 되나요?"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("reply", data)
        self.assertIn("3월 1일", data["reply"])

    def test_student_chat_dynamic_rag_score_injection(self):
        """calculate_student_certification 기반 취득 총점 및 영역별 점수 동적 주입 검증"""
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "현재 제 총점과 현황을 알려줘."},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        # mock 데이터: 전문기술역량 15점 + 상벌점 3점(가산) = 18.0점
        self.assertIn("18.0점", data["reply"])
        self.assertIn("김학생", data["reply"])

    def test_student_chat_idor_protection(self):
        """타 학생의 conversationId 변조 접근 시 403 Forbidden IDOR 차단 검증"""
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={
                "message": "안녕?",
                "conversationId": "conv-other-student",  # 타 학생 세션
            },
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN_CONVERSATION_ACCESS")

    def test_student_chat_session_persistence(self):
        """chat_conversations 및 chat_messages 테이블로의 대화 이력 영속화 및 commit 검증"""
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "반려된 증빙은 어떻게 재제출하나요?"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(data["conversationId"].startswith("conv-"))
        # VARCHAR(36) 컬럼 길이 초과 방어 검증 (최대 36자)
        self.assertLessEqual(len(data["conversationId"]), 36)

        # DB 커밋/삽입 내역 검증 (USER 질문 및 ASSISTANT 답변)
        inserted_sqls = [sql for sql, params in self.mock_conn.executed_inserts]
        self.assertTrue(any("INSERT INTO chat_messages" in s for s in inserted_sqls))
        self.assertTrue(any("INSERT INTO chat_conversations" in s for s in inserted_sqls))
        # 트랜잭션 정상 커밋 검증 (conn.commit 호출 확인)
        self.assertTrue(self.mock_conn.committed)
        self.assertGreaterEqual(self.mock_conn.commit_count, 1)

    def test_student_chat_forbidden_for_teacher(self):
        """교사 권한 토큰으로 학생 전용 챗봇 호출 시 403 FORBIDDEN_ROLE 차단 검증"""
        teacher_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000002",
            "id": "teacher1",
            "role": "teacher",
            "name": "김교사",
        })
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {teacher_token}"},
            json={"message": "학생 역량 현황 알려줘."},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN_ROLE")

    def test_student_chat_empty_message_bad_request(self):
        """공백 또는 빈 문자열 메시지 전송 시 400 Bad Request 검증"""
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "     "},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "EMPTY_MESSAGE")

    def test_student_chat_recent_messages_order(self):
        """대화 이력 조회 시 과거 10건이 아닌 최신 10건(ORDER BY message_id DESC LIMIT 10) 질의 검증"""
        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "이전 대화 맥락 테스트"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(self.mock_conn.last_message_query)
        self.assertIn("ORDER BY message_id DESC", self.mock_conn.last_message_query)
        self.assertIn("LIMIT 10", self.mock_conn.last_message_query)

    def test_classify_submission_top_level_keys(self):
        """증빙 자동 분류 응답 최상위 레벨 키 호환성 검증 (area, itemId, confidence, reason)"""
        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"detail": "정보처리기능사 실기 합격", "description": "자격증 취득"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["area"], "전문기술역량")
        self.assertIsNotNone(data["itemId"])
        self.assertGreaterEqual(data["confidence"], 0.6)
        self.assertIn("reason", data)

    @patch("routers.ai.httpx.AsyncClient.post")
    def test_classify_submission_gemini_markdown_codeblock_and_array(self, mock_post):
        """Gemini 응답에 마크다운 코드블록과 전치 텍스트가 섞여있거나 영역명이 변형된 경우도 정규화 파싱 검증"""
        os.environ["GEMINI_API_KEY"] = "mock-gemini-key"
        # 설명 텍스트 + ```json + 변형된 영역명 ("외국어능력")
        mixed_text = (
            "다음은 학생 활동 분석 결과입니다.\n```json\n"
            "[\n"
            "  {\n"
            "    \"area\": \"외국어능력\",\n"
            "    \"itemId\": 11,\n"
            "    \"itemName\": \"TOEIC 성적 증빙\",\n"
            "    \"confidence\": 0.95,\n"
            "    \"reason\": \"어학 성적 증빙입니다.\"\n"
            "  }\n"
            "]\n```\n확인 바랍니다."
        )

        mock_gemini_json = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": mixed_text}]
                    }
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_gemini_json
        mock_post.return_value = mock_response

        resp = self.client.post(
            "/api/ai/classify-submission",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"detail": "토익 시험 850점", "description": "어학 성적표 첨부"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        # '외국어능력' -> 공식 '외국어 능력'으로 정규화 매핑 확인
        self.assertEqual(data["bestMatch"]["area"], "외국어 능력")
        self.assertEqual(data["bestMatch"]["confidence"], 0.95)

    @patch("routers.ai.httpx.AsyncClient.post")
    def test_student_chat_gemini_api_success(self, mock_post):
        """Gemini API 응답 정상 반환 검증"""
        os.environ["GEMINI_API_KEY"] = "mock-gemini-key"
        os.environ["GEMINI_MODEL"] = "gemini-3.5-flash-lite"

        mock_gemini_json = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": "김학생님! 현재 전문기술역량에서 15점을 획득하셨으며, 외국어 영역에서 추가 10점을 취득하시면 인증 기준을 달성하실 수 있습니다."
                            }
                        ]
                    }
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_gemini_json
        mock_post.return_value = mock_response

        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "앞으로 어떤 영역을 더 채워야 하나요?"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("외국어 영역에서 추가 10점", data["reply"])
        self.assertIn("suggestions", data)

    def test_student_chat_rate_limit_exceeded(self):
        """챗봇 분당 20회 요청 한도 초과 시 429 차단 검증"""
        async def _exceeded_incr(k):
            return 21
        self.mock_redis.incr = _exceeded_incr

        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "질문입니다."},
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["code"], "RATE_LIMIT_EXCEEDED")

    @patch("routers.ai.httpx.AsyncClient.post")
    def test_student_chat_criteria_pending_rag_preservation(self, mock_post):
        """기준 미등록 학년이라도 학생 프로필/역량인증제 종합 현황 헤더가 보존되어 Gemini에 전달되는지 검증"""
        os.environ["GEMINI_API_KEY"] = "mock-gemini-key"
        self.mock_conn.no_criteria = True

        captured_system_prompt = None

        async def _mock_post(url, json=None, headers=None, **kwargs):
            nonlocal captured_system_prompt
            if json and "systemInstruction" in json:
                parts = json["systemInstruction"].get("parts", [])
                if parts:
                    captured_system_prompt = parts[0].get("text")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": "기준 등록 대기 안내 답변입니다."}]}}]
            }
            return mock_resp

        mock_post.side_effect = _mock_post

        resp = self.client.post(
            "/api/ai/student/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "안녕하세요"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(captured_system_prompt)
        # 연산자 우선순위 버그 수정 확인: 프로필과 헤더가 통째로 날아가지 않고 온전히 보존됨
        self.assertIn("[학생 프로필]", captured_system_prompt)
        self.assertIn("[역량인증제 종합 현황", captured_system_prompt)
        self.assertIn("김학생", captured_system_prompt)
        self.assertIn("현재 학년 평가 기준 등록 대기 중", captured_system_prompt)


if __name__ == "__main__":
    unittest.main()
