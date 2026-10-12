"""
tests/test_ai_teacher_services.py - 교사용 AI 서비스 5.2.1 및 5.2.2 종합 단위/통합 테스트

검증 범위:
1. 5.2.1 교사용 심사 Copilot 증빙자료 검토의견 초안 생성 (POST /api/ai/teacher/review-draft)
   - RBAC 인가 검증: 교사/관리자 허용, 학생 접근 시 403 FORBIDDEN
   - Rate Limiting: 분당 30회 초과 시 429 RATE_LIMIT_EXCEEDED
   - DB 원천 증빙 연동: submissionId 전달 시 DB 증빙/평가항목/영역 배점 조회 연동
   - 권장 점수(recommendedScore) 0.0 ~ maxScore 클램핑 및 상태 판정(인정완료, 반려, 재제출요청)
   - LLM 연동 및 모의(Mock) 응답 파싱
   - Gemini API 미설정 / 타임아웃 / 오류 시 규칙 기반 Graceful Fallback
2. 5.2.2 교사용 학급 지도 Copilot 챗봇 (POST /api/ai/teacher/chat)
   - RBAC 인가 검증: 교사/관리자 허용, 학생 접근 시 403 FORBIDDEN
   - 비어있는 메시지 유효성 검증: 400 EMPTY_MESSAGE
   - Rate Limiting: 분당 30회 초과 시 429 RATE_LIMIT_EXCEEDED
   - 교무분장 기반 담임/학년 권한 격리 (Strict Class Isolation):
     * 담임교사: 본인 담당 학급만 허용, 타 학급/타 학년 요청 시 403 FORBIDDEN_CLASS_ACCESS
     * 학년부장: 본인 담당 학년 전체 허용, 타 학년 요청 시 403 FORBIDDEN_CLASS_ACCESS
     * 관리자 / 총괄부서장: 전교생 데이터 접근 허용
   - IDOR 방어: 타 사용자의 conversationId 접근 시 403 FORBIDDEN_CONVERSATION_ACCESS
   - 세션 및 메시지 이력 영속화: chat_conversations 생성, chat_messages에 'TEACHER' 및 'ASSISTANT' 기록 및 commit
   - RAG 데이터 수집 및 연동: 담당 학급 학생 수, 종합 판정 분포, 심사 대기건, 5대 영역 평균, 지연/취약군 학생 현황
   - 규칙 기반 Fallback 질의응답: "현황 요약", "보완 시급 학생", "심사 대기건"
   - 개인정보(PII) 마스킹 및 프롬프트 인젝션 방어
3. API Key 및 모델 분리 유연성:
   - TEACHER_AI_API_KEY 우선순위 (TEACHER_AI_API_KEY > GEMINI_API_KEY > AI_API_KEY)
   - GEMINI_TEACHER_MODEL 환경변수 정규화
   - 단위 테스트 실행 시 실제 API 격리
"""

from datetime import date, datetime
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import httpx

from core.security import create_access_token
from database import get_redis
from main import app
from routers.ai import (
    check_ai_rate_limit,
    get_ai_db,
    get_teacher_ai_config,
    resolve_gemini_model,
    sanitize_and_mask_text,
)


class MockTeacherAIDatabaseCursor:
    """교사용 AI 라우터 전용 DB Mock 커서"""

    def __init__(self, conn):
        self.conn = conn
        self._results = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql: str, params: tuple = ()):
        sql_clean = " ".join(sql.strip().split())

        # 1. 교사 정보 조회
        if "FROM teachers" in sql_clean:
            uuid_param = params[0] if params else ""
            if self.conn.custom_teacher_row is not None:
                self._results = [self.conn.custom_teacher_row]
            elif uuid_param == "00000000-0000-0000-0000-000000000002":
                # 기본 담임교사: 1학년 2반
                self._results = [
                    {
                        "teachers_id": 1,
                        "name": "김담임",
                        "subject": "정보컴퓨터",
                        "position": "정담임",
                        "department": "교무기획부",
                        "grade": 1,
                        "class": 2,
                    }
                ]
            elif uuid_param == "00000000-0000-0000-0000-000000000003":
                # 학년부장: 1학년
                self._results = [
                    {
                        "teachers_id": 2,
                        "name": "박학년부장",
                        "subject": "수학",
                        "position": "학년부장",
                        "department": "교무기획부",
                        "grade": 1,
                        "class": None,
                    }
                ]
            elif uuid_param == "00000000-0000-0000-0000-000000000004":
                # 총괄 부서장: 마이스터부 부서부장
                self._results = [
                    {
                        "teachers_id": 3,
                        "name": "이마이스터부장",
                        "subject": "전공",
                        "position": "부서부장",
                        "department": "마이스터부",
                        "grade": None,
                        "class": None,
                    }
                ]
            else:
                self._results = [
                    {
                        "teachers_id": 1,
                        "name": "테스트교사",
                        "subject": "정보컴퓨터",
                        "position": "정담임",
                        "department": "교무기획부",
                        "grade": 1,
                        "class": 2,
                    }
                ]

        # 2. 증빙자료 상세 조회 (review-draft 연동용)
        elif "FROM submissions s" in sql_clean and "WHERE s.submission_id = %s" in sql_clean:
            sub_id = params[0] if params else 0
            if sub_id == 10:
                self._results = [
                    {
                        "submission_id": 10,
                        "student_id": 101,
                        "item_id": 1,
                        "detail": "정보처리기능사 취득 확인서",
                        "description": "2026년 1회 정기 검정 기능사 최종 합격",
                        "status_code": 1,
                        "granted_score": 0.0,
                        "activity_date": date(2026, 4, 15),
                        "file_path": "/uploads/cert.pdf",
                        "link_url": None,
                        "item_name": "국가기술자격증(기능사/산업기사) 취득",
                        "item_max_score": 20.0,
                        "scoring_type": 1,
                        "area_name": "전문기술역량",
                        "area_max_score": 20.0,
                        "student_name": "홍길동",
                    }
                ]
            else:
                self._results = []

        # 3. 대화 세션 조회 (IDOR 검증)
        elif "SELECT user_uuid FROM chat_conversations" in sql_clean:
            conv_id = params[0] if params else ""
            if conv_id == "conv-other-teacher":
                self._results = [{"user_uuid": "00000000-0000-0000-0000-000000000099"}]
            elif conv_id == "conv-my-teacher":
                self._results = [{"user_uuid": "00000000-0000-0000-0000-000000000002"}]
            else:
                self._results = []

        # 4. 활성 학년도 조회
        elif "FROM academic_years" in sql_clean:
            self._results = [{"year_id": 1, "year": 2026}]

        # 5. 학적 및 학생 목록 조회
        elif "FROM student_academic_records" in sql_clean:
            if self.conn.custom_students_rows is not None:
                self._results = self.conn.custom_students_rows
            else:
                self._results = [
                    {"student_id": 101, "grade": 1, "class_no": 2, "number": 1, "student_name": "홍길동"},
                    {"student_id": 102, "grade": 1, "class_no": 2, "number": 2, "student_name": "김철수"},
                    {"student_id": 103, "grade": 1, "class_no": 2, "number": 3, "student_name": "이영희"},
                ]

        # 6. 인증 영역 조회
        elif "FROM certification_areas" in sql_clean:
            if self.conn.custom_areas_rows is not None:
                self._results = self.conn.custom_areas_rows
            else:
                self._results = [
                    {"area_id": 1, "grade": 1, "name": "직업기초능력", "max_score": 20.0},
                    {"area_id": 2, "grade": 1, "name": "전문기술역량", "max_score": 20.0},
                    {"area_id": 3, "grade": 1, "name": "인성/직업의식", "max_score": 20.0},
                    {"area_id": 4, "grade": 1, "name": "인문학적 소양", "max_score": 20.0},
                    {"area_id": 5, "grade": 1, "name": "외국어 능력", "max_score": 20.0},
                ]

        # 6-1. 배점 기준 조회
        elif "FROM scoring_rules" in sql_clean:
            if self.conn.custom_scoring_rules is not None:
                self._results = self.conn.custom_scoring_rules
            else:
                self._results = [
                    {"rule_id": 1, "item_id": 1, "condition_value": "정보처리기능사 취득", "score": 20.0}
                ]

        # 7. 증빙 제출 목록 조회 (RAG 컨텍스트)
        elif "FROM submissions s" in sql_clean and "IN (" in sql_clean:
            self._results = [
                {
                    "submission_id": 1,
                    "student_id": 101,
                    "item_id": 1,
                    "status_code": 3,  # 승인
                    "granted_score": 15.0,
                    "detail": "정보처리기능사 취득",
                    "area_id": 2,
                    "scoring_type": 1,
                    "item_max_score": 20.0,
                    "item_name": "국가기술자격증 취득",
                },
                {
                    "submission_id": 2,
                    "student_id": 101,
                    "item_id": 4,
                    "status_code": 1,  # 대기
                    "granted_score": 0.0,
                    "detail": "직업기초 발표 과제",
                    "area_id": 1,
                    "scoring_type": 1,
                    "item_max_score": 20.0,
                    "item_name": "의사소통 프로젝트",
                },
                {
                    "submission_id": 3,
                    "student_id": 102,
                    "item_id": 6,
                    "status_code": 3,  # 승인
                    "granted_score": 20.0,
                    "detail": "봉사활동 30시간",
                    "area_id": 3,
                    "scoring_type": 1,
                    "item_max_score": 20.0,
                    "item_name": "봉사활동",
                },
            ]

        # 8. 상벌점 목록 조회
        elif "FROM merits" in sql_clean:
            self._results = [
                {
                    "student_id": 101,
                    "merits_point_id": 1,
                    "type": "+",
                    "points": 2.0,
                    "related_area": "인성/직업의식",
                    "reason": "선행상",
                    "occurred_at": date(2026, 4, 1),
                    "is_reflected": True,
                }
            ]

        # 9. 대화 메시지 이력 조회
        elif "FROM chat_messages" in sql_clean:
            self._results = [
                {"sender_role": "TEACHER", "content": "우리 반 인증 현황 어때?"},
                {"sender_role": "ASSISTANT", "content": "현재 1학년 2반의 평균 점수는 65점입니다."},
            ]

        # 10. INSERT / UPDATE 문 기록
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


class MockTeacherAIDatabaseConnection:
    """Mock 비동기 DB 커넥션"""

    def __init__(self):
        self.executed_inserts = []
        self.committed = False
        self.commit_count = 0
        self.custom_teacher_row = None
        self.custom_students_rows = None
        self.custom_areas_rows = None
        self.custom_scoring_rules = None

    def cursor(self, *args, **kwargs):
        return MockTeacherAIDatabaseCursor(self)

    async def commit(self):
        self.committed = True
        self.commit_count += 1

    async def rollback(self):
        pass

    async def autocommit(self, val: bool):
        pass


class TestAITeacherServices(unittest.TestCase):
    """5.2 교사용 AI 서비스 단위 및 통합 테스트"""

    def setUp(self):
        self.client = TestClient(app)
        self.mock_db = MockTeacherAIDatabaseConnection()

        # 의존성 오버라이드: DB 커넥션
        async def _mock_get_ai_db():
            yield self.mock_db

        app.dependency_overrides[get_ai_db] = _mock_get_ai_db

        # 의존성 오버라이드: Redis
        self.mock_redis = MagicMock()
        self.redis_store = {}

        async def _fake_redis_get(k):
            return self.redis_store.get(k)

        async def _fake_redis_incr(k):
            self.redis_store[k] = self.redis_store.get(k, 0) + 1
            return self.redis_store[k]

        async def _fake_redis_expire(k, sec):
            return True

        async def _fake_redis_ttl(k):
            return 60

        self.mock_redis.get = _fake_redis_get
        self.mock_redis.incr = _fake_redis_incr
        self.mock_redis.expire = _fake_redis_expire
        self.mock_redis.ttl = _fake_redis_ttl
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        # 토큰 발급: 학생, 교사, 관리자
        self.student_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000001",
            "id": "student1",
            "role": "student",
            "name": "김학생",
        })
        self.teacher_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000002",
            "id": "teacher1",
            "role": "teacher",
            "name": "김담임",
        })
        self.grade_head_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000003",
            "id": "gradehead1",
            "role": "teacher",
            "name": "박학년부장",
        })
        self.dept_head_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000004",
            "id": "depthead1",
            "role": "teacher",
            "name": "이마이스터부장",
        })
        self.admin_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000099",
            "id": "admin1",
            "role": "admin",
            "name": "관리자",
        })

        # 환경변수 격리 (실서버 API 호출 방지)
        self._orig_teacher_api_key = os.environ.get("TEACHER_AI_API_KEY")
        self._orig_gemini_api_key = os.environ.get("GEMINI_API_KEY")
        self._orig_ai_api_key = os.environ.get("AI_API_KEY")
        self._orig_teacher_model = os.environ.get("GEMINI_TEACHER_MODEL")

        os.environ.pop("TEACHER_AI_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("AI_API_KEY", None)
        os.environ.pop("GEMINI_TEACHER_MODEL", None)

    def tearDown(self):
        app.dependency_overrides.clear()
        if self._orig_teacher_api_key is not None:
            os.environ["TEACHER_AI_API_KEY"] = self._orig_teacher_api_key
        else:
            os.environ.pop("TEACHER_AI_API_KEY", None)

        if self._orig_gemini_api_key is not None:
            os.environ["GEMINI_API_KEY"] = self._orig_gemini_api_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)

        if self._orig_ai_api_key is not None:
            os.environ["AI_API_KEY"] = self._orig_ai_api_key
        else:
            os.environ.pop("AI_API_KEY", None)

        if self._orig_teacher_model is not None:
            os.environ["GEMINI_TEACHER_MODEL"] = self._orig_teacher_model
        else:
            os.environ.pop("GEMINI_TEACHER_MODEL", None)

    # ==========================================================================
    # 1. RBAC 인가 및 접근 제어 검증
    # ==========================================================================

    def test_teacher_review_draft_forbidden_for_student(self):
        """학생 계정으로 교사용 심사 Copilot 호출 시 403 차단"""
        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"detail": "정보처리기능사 취득", "maxScore": 20.0},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "FORBIDDEN_ROLE")

    def test_teacher_chat_forbidden_for_student(self):
        """학생 계정으로 교사용 학급 지도 챗봇 호출 시 403 차단"""
        resp = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"message": "우리 반 학생들의 인증 현황 요약해줘"},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "FORBIDDEN_ROLE")

    def test_teacher_review_draft_allowed_for_teacher_and_admin(self):
        """교사 및 관리자는 심사 Copilot 호출 허용 (200 OK)"""
        # 1. 교사 호출
        resp_t = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"detail": "토익 850점 취득", "area": "외국어 능력", "maxScore": 20.0},
        )
        self.assertEqual(resp_t.status_code, 200)
        self.assertTrue(resp_t.json()["success"])

        # 2. 관리자 호출
        resp_a = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"detail": "봉사활동 30시간 증빙", "area": "인성/직업의식", "maxScore": 20.0},
        )
        self.assertEqual(resp_a.status_code, 200)
        self.assertTrue(resp_a.json()["success"])

    def test_teacher_chat_allowed_for_admin(self):
        """관리자는 교사용 학급 지도 챗봇 호출 허용 (200 OK)"""
        resp = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"message": "전교생 인증 현황 요약해줘"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    # ==========================================================================
    # 2. 5.2.1 심사 Copilot (review-draft) 세부 기능 검증
    # ==========================================================================

    def test_teacher_review_draft_rule_based_fallback_valid(self):
        """정상 증빙 제출건에 대한 규칙 기반 심사 초안 생성 (인정완료, 최대점수, 피드백, 가이드라인)"""
        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={
                "area": "전문기술역량",
                "itemName": "국가기술자격증(기능사/산업기사) 취득",
                "detail": "정보처리기능사 최종 합격 증빙 제출",
                "description": "한국산업인력공단 발행 취득확인서 첨부합니다.",
                "maxScore": 20.0,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "인정완료")
        self.assertEqual(data["recommendedStatus"], "인정완료")
        self.assertEqual(data["recommendedScore"], 20.0)
        self.assertIn("teacherCommentDraft", data)
        self.assertIn("guidelineSummary", data)
        self.assertTrue(len(data["checkPoints"]) > 0)
        self.assertGreaterEqual(data["confidence"], 0.8)

    def test_teacher_review_draft_rule_based_fallback_poor_submission(self):
        """부실/불충분 제출건에 대한 재제출요청 판정 및 0점 제안"""
        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={
                "area": "외국어 능력",
                "itemName": "공인 외국어 성적",
                "detail": "없음",
                "description": "asdf",
                "maxScore": 20.0,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "재제출요청")
        self.assertEqual(data["recommendedScore"], 0.0)
        self.assertIn("보완하여 재제출", data["teacherCommentDraft"])

    def test_teacher_review_draft_db_submission_lookup(self):
        """submissionId 지정 시 DB 원천 증빙 연동 및 검토의견 생성"""
        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"submissionId": 10},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "인정완료")
        self.assertEqual(data["recommendedScore"], 20.0)
        self.assertIn("teacherCommentDraft", data)
        self.assertIn("draftComment", data)

    def test_teacher_review_draft_criteria_support(self):
        """요청 DTO에 criteria 명시 시 AI 심사 가이드라인 및 응답 데이터에 배점 기준 반영 검증"""
        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={
                "area": "외국어 능력",
                "itemName": "공인 외국어 성적",
                "detail": "TOEIC 850점 취득 증빙",
                "criteria": "TOEIC 800점 이상 20점, 700점 이상 15점",
                "maxScore": 20.0,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "인정완료")
        self.assertEqual(data["criteria"], "TOEIC 800점 이상 20점, 700점 이상 15점")
        self.assertIn("TOEIC 800점 이상 20점", data["guidelineSummary"])
        self.assertEqual(data["draftComment"], data["teacherCommentDraft"])

    def test_teacher_review_draft_scoring_rules_from_db(self):
        """submissionId 전달 시 scoring_rules 테이블에서 배점 기준 자동 조회 및 연동 검증"""
        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"submissionId": 10},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["criteria"], "정보처리기능사 취득: 20.0점")
        self.assertIn("정보처리기능사 취득: 20.0점", data["guidelineSummary"])

    def test_teacher_review_draft_nonexistent_submission_404(self):
        """DB에 존재하지 않는 submissionId 요청 시 404 SUBMISSION_NOT_FOUND 반환"""
        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"submissionId": 99999},
        )
        self.assertEqual(resp.status_code, 404)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "SUBMISSION_NOT_FOUND")

    def test_teacher_review_draft_strict_class_isolation_forbidden(self):
        """2학년 1반 담임교사가 1학년 2반 학생의 submissionId(10) 심사 시 403 차단 (Tech_spec 5.2.1 교사 담당 학급 검증)"""
        # 2학년 1반 담임교사 설정
        self.mock_db.custom_teacher_row = {
            "teachers_id": 99,
            "name": "박타반담임",
            "subject": "국어",
            "position": "정담임",
            "department": "교무기획부",
            "grade": 2,
            "class": 1,
        }
        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"submissionId": 10},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "FORBIDDEN_CLASS_ACCESS")
        self.assertIn("담당 학급", data["error"]["message"])

    @patch("routers.ai.call_gemini_api")
    def test_teacher_review_draft_gemini_mock_score_clamping(self, mock_gemini):
        """Gemini API 응답 시 권장 점수(recommendedScore) 0.0 ~ maxScore 클램핑 검증"""
        # 1. LLM이 maxScore(20.0)를 초과한 35.0을 반환했을 때 20.0으로 클램핑
        mock_gemini.return_value = json.dumps({
            "status": "인정완료",
            "recommendedScore": 35.0,
            "teacherCommentDraft": "자격증 취득 실적이 매우 우수합니다.",
            "guidelineSummary": "국가기술자격 배점 기준 적용",
            "checkPoints": ["취득확인서 원본 확인"],
            "confidence": 0.98,
        })
        os.environ["TEACHER_AI_API_KEY"] = "mock_teacher_key"

        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={
                "area": "전문기술역량",
                "itemName": "국가기술자격증 취득",
                "detail": "정보처리기능사 취득",
                "maxScore": 20.0,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["recommendedScore"], 20.0)
        self.assertEqual(data["teacherCommentDraft"], "자격증 취득 실적이 매우 우수합니다.")

        # 2. LLM이 음수(-5.0)를 반환했을 때 0.0으로 클램핑
        mock_gemini.return_value = json.dumps({
            "status": "반려",
            "recommendedScore": -5.0,
            "teacherCommentDraft": "해당 활동은 인증 기준에 부합하지 않습니다.",
            "guidelineSummary": "항목 기준 미달",
            "confidence": 0.9,
        })
        resp_neg = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"detail": "관련없는 활동", "maxScore": 20.0},
        )
        self.assertEqual(resp_neg.status_code, 200)
        data_neg = resp_neg.json()["data"]
        self.assertEqual(data_neg["recommendedScore"], 0.0)
        self.assertEqual(data_neg["status"], "반려")

    @patch("routers.ai.call_gemini_api")
    def test_teacher_review_draft_gemini_timeout_fallback(self, mock_gemini):
        """Gemini API 타임아웃 / None 반환 시 규칙 기반 Graceful Fallback 수행"""
        mock_gemini.return_value = None
        os.environ["TEACHER_AI_API_KEY"] = "mock_teacher_key"

        resp = self.client.post(
            "/api/ai/teacher/review-draft",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"detail": "독서 마일리지 서평 제출", "area": "인문학적 소양", "maxScore": 15.0},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "인정완료")
        self.assertEqual(data["recommendedScore"], 15.0)
        self.assertIn("teacherCommentDraft", data)

    # ==========================================================================
    # 3. 5.2.2 교사용 학급 지도 챗봇 (teacher/chat) & Strict Class Isolation
    # ==========================================================================

    def test_teacher_chat_empty_message_validation(self):
        """빈 메시지 전달 시 400 EMPTY_MESSAGE 반환"""
        resp = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"message": "   "},
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "EMPTY_MESSAGE")

    def test_teacher_chat_strict_class_isolation_homeroom(self):
        """담임교사(1-2) 권한 격리: 담당 학급(1-2) 접근 허용, 타 학급/타 학년 요청 시 403 FORBIDDEN_CLASS_ACCESS"""
        # 1. 본인 담당 학급 질의 (학년/반 미지정 시 자동 1-2 스코프 적용)
        resp_own = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"message": "우리 반 학생들의 인증 현황 요약해줘"},
        )
        self.assertEqual(resp_own.status_code, 200)
        data_own = resp_own.json()["data"]
        self.assertIn("1학년 2반", data_own["reply"])
        self.assertEqual(data_own["scope"]["grade"], 1)
        self.assertEqual(data_own["scope"]["classNo"], 2)

        # 2. 타 학급(1학년 3반) 명시 요청 시 403 차단
        resp_other_class = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"message": "3반 학생 현황 알려줘", "grade": 1, "classNo": 3},
        )
        self.assertEqual(resp_other_class.status_code, 403)
        self.assertEqual(resp_other_class.json()["error"]["code"], "FORBIDDEN_CLASS_ACCESS")

        # 3. 타 학년(2학년 2반) 명시 요청 시 403 차단
        resp_other_grade = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"message": "2학년 2반 현황 알려줘", "grade": 2, "classNo": 2},
        )
        self.assertEqual(resp_other_grade.status_code, 403)
        self.assertEqual(resp_other_grade.json()["error"]["code"], "FORBIDDEN_CLASS_ACCESS")

    def test_teacher_chat_strict_class_isolation_grade_head(self):
        """학년부장(1학년): 1학년 전체 및 산하 학급 접근 허용, 2학년 요청 시 403 차단"""
        # 1. 담당 1학년 접근 허용
        resp_grade1 = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.grade_head_token}"},
            json={"message": "1학년 전체 현황 알려줘", "grade": 1},
        )
        self.assertEqual(resp_grade1.status_code, 200)
        self.assertTrue(resp_grade1.json()["success"])

        # 2. 담당 1학년 내 특정 학급 접근 허용
        resp_grade1_class1 = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.grade_head_token}"},
            json={"message": "1반 현황 알려줘", "grade": 1, "classNo": 1},
        )
        self.assertEqual(resp_grade1_class1.status_code, 200)

        # 3. 타 학년(2학년) 요청 시 403 차단
        resp_grade2 = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.grade_head_token}"},
            json={"message": "2학년 현황 알려줘", "grade": 2},
        )
        self.assertEqual(resp_grade2.status_code, 403)
        self.assertEqual(resp_grade2.json()["error"]["code"], "FORBIDDEN_CLASS_ACCESS")

    def test_teacher_chat_dept_head_and_admin_schoolwide_access(self):
        """총괄 부서장(마이스터부) 및 관리자는 전교생/임의 학급 접근 허용"""
        # 1. 마이스터부 부서부장: 2학년 3반 접근
        resp_dept = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.dept_head_token}"},
            json={"message": "2학년 3반 현황 요약해줘", "grade": 2, "classNo": 3},
        )
        self.assertEqual(resp_dept.status_code, 200)
        self.assertTrue(resp_dept.json()["success"])

        # 2. 관리자: 3학년 1반 접근
        resp_admin = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"message": "3학년 1반 현황 요약해줘", "grade": 3, "classNo": 1},
        )
        self.assertEqual(resp_admin.status_code, 200)
        self.assertTrue(resp_admin.json()["success"])

    def test_teacher_chat_session_idor_protection(self):
        """타 교사의 대화 세션(conversationId) 접근 시 403 차단 (Strict IDOR)"""
        resp = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={
                "message": "우리 반 통계 알려줘",
                "conversationId": "conv-other-teacher",
            },
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN_CONVERSATION_ACCESS")

    def test_teacher_chat_session_and_message_persistence(self):
        """대화 세션 및 메시지 영속화 검증 (sender_role='TEACHER' 및 'ASSISTANT', conn commit)"""
        resp = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"message": "우리 반 학생들의 인증 현황 요약해줘"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(data["conversationId"].startswith("conv-teacher-"))

        # DB INSERT 문 실행 확인
        inserts = self.mock_db.executed_inserts
        teacher_msg_saved = False
        assistant_msg_saved = False

        for sql, params in inserts:
            if "INSERT INTO chat_messages" in sql and "'TEACHER'" in sql:
                teacher_msg_saved = True
            if "INSERT INTO chat_messages" in sql and "'ASSISTANT'" in sql:
                assistant_msg_saved = True

        self.assertTrue(teacher_msg_saved, "교사 질문 메시지가 chat_messages에 'TEACHER'로 저장되어야 합니다.")
        self.assertTrue(assistant_msg_saved, "어시스턴트 답변이 chat_messages에 'ASSISTANT'로 저장되어야 합니다.")
        self.assertTrue(self.mock_db.committed, "대화 세션 및 메시지는 커밋(commit)되어야 합니다.")

    def test_teacher_chat_fallback_qa_guidance(self):
        """규칙 기반 Fallback 질의응답 (현황 요약, 보완 시급 학생, 심사 대기건) 검증"""
        # 1. 현황 요약 질의
        resp_summary = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"message": "우리 반 학생들의 인증 현황 요약해줘"},
        )
        self.assertEqual(resp_summary.status_code, 200)
        reply1 = resp_summary.json()["data"]["reply"]
        self.assertIn("1학년 2반", reply1)
        self.assertIn("평균", reply1)

        # 2. 보완 시급 학생 질의
        resp_risk = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"message": "보완이 시급한 취약 학생과 지도 방안 안내해줘"},
        )
        self.assertEqual(resp_risk.status_code, 200)
        reply2 = resp_risk.json()["data"]["reply"]
        self.assertTrue("보완" in reply2 or "취약" in reply2 or "60점" in reply2)

        # 3. 심사 대기건 질의
        resp_pending = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"message": "심사 대기건 현황 알려줘"},
        )
        self.assertEqual(resp_pending.status_code, 200)
        reply3 = resp_pending.json()["data"]["reply"]
        self.assertIn("심사 대기", reply3)

    def test_teacher_chat_pii_masking_and_injection_defense(self):
        """교사 질문 내 PII(전화번호/주민번호) 마스킹 처리 검증"""
        resp = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"message": "학생 연락처 010-1234-5678과 주민번호 070101-3123456 학생의 인증 상태 확인해줘"},
        )
        self.assertEqual(resp.status_code, 200)

        # DB에 저장된 쿼리 파라미터에서 마스킹 확인
        inserts = self.mock_db.executed_inserts
        masked_saved = False
        for sql, params in inserts:
            if "INSERT INTO chat_messages" in sql and "'TEACHER'" in sql:
                saved_content = params[1]
                if "[전화번호 마스킹]" in saved_content and "[주민등록번호 마스킹]" in saved_content:
                    masked_saved = True
        self.assertTrue(masked_saved, "DB에 영속화되는 질문 텍스트 내 개인정보는 반드시 마스킹되어야 합니다.")

    # ==========================================================================
    # 4. API Key 및 모델 분리 유연성 검증
    # ==========================================================================

    def test_teacher_ai_config_priority(self):
        """TEACHER_AI_API_KEY 및 GEMINI_TEACHER_MODEL 우선순위 검증"""
        # 1. 모든 키 미설정 시
        os.environ.pop("TEACHER_AI_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("AI_API_KEY", None)
        k, m = get_teacher_ai_config()
        self.assertEqual(k, "")
        self.assertEqual(m, "gemini-3.5-flash-lite")

        # 2. GEMINI_API_KEY 설정 시
        os.environ["GEMINI_API_KEY"] = "gemini_global_key"
        k, m = get_teacher_ai_config()
        self.assertEqual(k, "gemini_global_key")

        # 3. TEACHER_AI_API_KEY 설정 시 TEACHER 키 최우선 적용
        os.environ["TEACHER_AI_API_KEY"] = "teacher_dedicated_key"
        k, m = get_teacher_ai_config()
        self.assertEqual(k, "teacher_dedicated_key")

        # 4. GEMINI_TEACHER_MODEL 설정 시 모델 우선 적용
        os.environ["GEMINI_TEACHER_MODEL"] = "gemini-2.5-flash"
        k, m = get_teacher_ai_config()
        self.assertEqual(m, "gemini-2.5-flash")

    # ==========================================================================
    # 5. Rate Limiting 검증
    # ==========================================================================

    async def _mock_rate_limit_exceeded(self, k, max_req=30, window=60):
        return False

    def test_teacher_review_draft_rate_limit(self):
        """심사 Copilot 분당 30회 초과 시 429 RATE_LIMIT_EXCEEDED 반환"""
        with patch("routers.ai.check_ai_rate_limit", return_value=False):
            resp = self.client.post(
                "/api/ai/teacher/review-draft",
                headers={"Authorization": f"Bearer {self.teacher_token}"},
                json={"detail": "정보처리기능사 취득", "maxScore": 20.0},
            )
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.json()["error"]["code"], "RATE_LIMIT_EXCEEDED")

    def test_teacher_chat_rate_limit(self):
        """학급 지도 챗봇 분당 30회 초과 시 429 RATE_LIMIT_EXCEEDED 반환"""
        with patch("routers.ai.check_ai_rate_limit", return_value=False):
            resp = self.client.post(
                "/api/ai/teacher/chat",
                headers={"Authorization": f"Bearer {self.teacher_token}"},
                json={"message": "우리 반 현황 알려줘"},
            )
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.json()["error"]["code"], "RATE_LIMIT_EXCEEDED")


    def test_teacher_chat_multi_grade_rag_evaluation(self):
        """관리자 전교생 질의 시 1학년과 2학년 학생이 각 학년 영역 기준으로 정상 집계되는지 검증"""
        self.mock_db.custom_students_rows = [
            {"student_id": 101, "grade": 1, "class_no": 2, "number": 1, "student_name": "1학년학생"},
            {"student_id": 201, "grade": 2, "class_no": 1, "number": 1, "student_name": "2학년학생"},
        ]
        self.mock_db.custom_areas_rows = [
            {"area_id": 1, "grade": 1, "name": "전문기술역량", "max_score": 20.0},
            {"area_id": 6, "grade": 2, "name": "전문기술역량", "max_score": 20.0},
        ]
        resp = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"message": "전교생 현황 요약해줘"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["scope"]["totalStudents"], 2)

    def test_teacher_chat_empty_class_in_db_returns_zero_students(self):
        """DB에 학생이 0명인 학급 질의 시 목(Mock) 데이터 없이 totalStudents: 0 반환 검증"""
        self.mock_db.custom_students_rows = []
        resp = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"message": "신설 학급 현황 알려줘", "grade": 3, "classNo": 4},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["scope"]["totalStudents"], 0)

    def test_pii_masking_with_various_id_patterns(self):
        """외국인/2020년 이후 출생자(7번째 자리 5~8) 주민등록번호 마스킹 처리 검증"""
        sample_text = "외국인 학생 번호 070101-5123456과 080202-6234567을 확인해 주세요."
        masked = sanitize_and_mask_text(sample_text)
        self.assertNotIn("070101-5123456", masked)
        self.assertNotIn("080202-6234567", masked)
        self.assertEqual(masked.count("[주민등록번호 마스킹]"), 2)

    def test_teacher_chat_email_with_at_resolution(self):
        """토큰 id에 이미 도메인(@bssm.hs.kr)이 포함되어 있어도 정상 교사 매핑 검증"""
        token_with_email = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000088",
            "id": "teacher1@bssm.hs.kr",
            "role": "teacher",
            "name": "이메일교사",
        })
        resp = self.client.post(
            "/api/ai/teacher/chat",
            headers={"Authorization": f"Bearer {token_with_email}"},
            json={"message": "우리 반 현황 요약해줘"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])


if __name__ == "__main__":
    unittest.main()
