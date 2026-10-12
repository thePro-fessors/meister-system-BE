"""
tests/test_ai_admin_services.py - 관리자용 AI 서비스 5.3.1 및 5.3.2 종합 단위/통합 테스트

검증 범위:
1. 5.3.1 전교생 인증 현황 통계 분석 및 총괄 리포트 자동 생성 (POST /api/ai/admin/summary-report)
   - 관리자 전용 RBAC 인가: 관리자(admin, 2, '2') 허용, 학생/교사 접근 시 403 FORBIDDEN
   - 비인증(Unauthenticated) 요청 시 401 UNAUTHORIZED
   - Rate Limiting: 분당 30회 초과 시 429 RATE_LIMIT_EXCEEDED
   - 학년도 자동 식별: 미지정 시 활성/최신 학년도, 미존재 학년도 요청 시 404 NOT_FOUND
   - 전교생 및 학년별 역량인증 달성률, 5대 영역 평균, 상벌점 통계, 인증 판정 분포("인증 가능", "검토중", "보완 필요") 집계
   - 특정 학년 필터(grade) 지원
   - Gemini API 모의(Mock) 호출 및 정상 JSON 응답(summaryReport, keyInsights, recommendations) 파싱
   - Gemini API 미설정 / 타임아웃 / 오류 시 규칙 기반 Graceful Fallback 리포트 생성
   - 빈 데이터셋(학생 0명)에서의 안전한 Graceful 처리

2. 5.3.2 부정 의심 제출 탐지 및 감사 리포트 (POST /api/ai/admin/audit-anomalies)
   - 관리자 전용 RBAC 인가: 관리자(admin, 2, '2') 허용, 학생/교사 접근 시 403 FORBIDDEN
   - 비인증 요청 시 401 UNAUTHORIZED
   - Rate Limiting: 분당 30회 초과 시 429 RATE_LIMIT_EXCEEDED
   - 5대 이상 징후 탐지 전수 검증:
     1) 단기간 내 과도한 상벌점 부여 (EXCESSIVE_MERIT_POINTS / RAPID_MERIT_GRANT)
     2) 배점 한도 초과 및 음수 점수 제출건 (EXCEEDED_MAX_SCORE / NEGATIVE_SCORE)
     3) 비활성 항목 점수 부여 건 (INACTIVE_ITEM_SCORED)
     4) 초고속 승인 의심 건 (ULTRA_FAST_APPROVAL / REVIEWER_CONCENTRATION)
     5) 중복 의심 제출건 (DUPLICATE_SUBMISSION)
   - 이상 징후가 없는 정상 데이터셋: riskScore=0.0, riskLevel='NORMAL', 빈 anomalies
   - 임계치(threshold) 및 건수 제한(limit) 필터링
   - Gemini API 모의(Mock) 호출 및 감사 소견(auditAnalysis, summary, recommendations) 파싱
   - Gemini API 미설정 / 타임아웃 / 오류 시 규칙 기반 Graceful Fallback 감사 소견 생성

3. API Key 및 모델 분리 유연성:
   - ADMIN_AI_API_KEY 우선순위 (ADMIN_AI_API_KEY > GEMINI_API_KEY > AI_API_KEY)
   - GEMINI_ADMIN_MODEL 환경변수 정규화 및 get_admin_ai_config() 단위 검증
   - 단위 테스트 실행 시 실제 외부 API 격리 (setUp/tearDown)

4. 유틸리티 및 데이터 안전성:
   - parse_datetime_safe 동작 검증
   - 개인정보(PII) 마스킹 검증
"""

from datetime import date, datetime, timedelta
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from core.security import create_access_token
from database import get_redis
from main import app
from routers.ai import (
    check_ai_rate_limit,
    get_admin_ai_config,
    get_ai_db,
    parse_datetime_safe,
    resolve_gemini_model,
    sanitize_and_mask_text,
)


class MockAdminAIDatabaseCursor:
    """관리자용 AI 라우터 전용 DB Mock 커서"""

    def __init__(self, conn):
        self.conn = conn
        self._results = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql: str, params: tuple = ()):
        sql_clean = " ".join(sql.strip().split())

        # 1. 학년도 조회
        if "FROM academic_years" in sql_clean:
            if self.conn.custom_years_rows is not None:
                self._results = self.conn.custom_years_rows
            elif "WHERE year = %s" in sql_clean:
                target_year = params[0] if params else 2026
                if target_year == 2026:
                    self._results = [{"year_id": 1, "year": 2026, "is_activated": 1}]
                elif target_year == 2025:
                    self._results = [{"year_id": 2, "year": 2025, "is_activated": 0}]
                else:
                    self._results = []
            else:
                self._results = [{"year_id": 1, "year": 2026, "is_activated": 1}]

        # 2. 평가 기준 조회 (certification_areas + evaluation_items)
        elif "FROM certification_areas ca" in sql_clean and "LEFT JOIN evaluation_items ei" in sql_clean:
            if self.conn.custom_criteria_rows is not None:
                self._results = self.conn.custom_criteria_rows
            else:
                self._results = [
                    # 1학년 영역 및 평가 항목
                    {"area_id": 1, "grade": 1, "area_name": "직업기초능력", "area_max_score": 20.0, "item_id": 1, "item_name": "직업기초 프로젝트", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                    {"area_id": 2, "grade": 1, "area_name": "전문기술역량", "area_max_score": 20.0, "item_id": 2, "item_name": "정보처리기능사 취득", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                    {"area_id": 3, "grade": 1, "area_name": "인성/직업의식", "area_max_score": 20.0, "item_id": 3, "item_name": "교내 봉사활동 20시간", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                    {"area_id": 4, "grade": 1, "area_name": "인문학적 소양", "area_max_score": 20.0, "item_id": 4, "item_name": "독서 마일리지 및 서평", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                    {"area_id": 5, "grade": 1, "area_name": "외국어 능력", "area_max_score": 20.0, "item_id": 5, "item_name": "공인 토익 500점 이상", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                    # 2학년 영역 및 평가 항목
                    {"area_id": 6, "grade": 2, "area_name": "직업기초능력", "area_max_score": 20.0, "item_id": 6, "item_name": "심화 직업기초 프로젝트", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                    {"area_id": 7, "grade": 2, "area_name": "전문기술역량", "area_max_score": 20.0, "item_id": 7, "item_name": "산업기사 필기/실기", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                    {"area_id": 8, "grade": 2, "area_name": "인성/직업의식", "area_max_score": 20.0, "item_id": 8, "item_name": "리더십 및 학생자치", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                    {"area_id": 9, "grade": 2, "area_name": "인문학적 소양", "area_max_score": 20.0, "item_id": 9, "item_name": "인문 고전 토론", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                    {"area_id": 10, "grade": 2, "area_name": "외국어 능력", "area_max_score": 20.0, "item_id": 10, "item_name": "영어 프레젠테이션", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
                ]

        # 3. 대상 학생 목록 조회 (student_academic_records + students)
        elif "FROM student_academic_records sar" in sql_clean:
            if self.conn.custom_students_rows is not None:
                self._results = self.conn.custom_students_rows
            else:
                self._results = [
                    {"student_id": 101, "name": "홍길동", "email": "gildong@bssm.hs.kr", "grade": 1, "class_no": 1, "number": 1},
                    {"student_id": 102, "name": "이순신", "email": "sunshin@bssm.hs.kr", "grade": 1, "class_no": 1, "number": 2},
                    {"student_id": 103, "name": "강감찬", "email": "gamchan@bssm.hs.kr", "grade": 2, "class_no": 1, "number": 1},
                ]

        # 4. 제출 증빙 목록 조회 (submissions + evaluation_items + certification_areas + students)
        elif "FROM submissions s" in sql_clean:
            if self.conn.custom_submissions_rows is not None:
                self._results = self.conn.custom_submissions_rows
            else:
                self._results = [
                    # 학생 101 (홍길동): 5대 영역 전원 달성 -> 인증 가능
                    {"submission_id": 1, "student_id": 101, "item_id": 1, "status_code": 3, "granted_score": 20.0, "created_at": datetime(2026, 4, 1, 10, 0), "reviewed_at": datetime(2026, 4, 1, 14, 0), "reviewer_id": 1, "detail": "기초프로젝트 완수", "description": "의사소통 프로젝트", "area_id": 1, "scoring_type": 1, "item_max_score": 20.0, "item_name": "직업기초 프로젝트", "area_name": "직업기초능력", "area_grade": 1, "student_name": "홍길동", "is_active": 1},
                    {"submission_id": 2, "student_id": 101, "item_id": 2, "status_code": 3, "granted_score": 20.0, "created_at": datetime(2026, 4, 2, 10, 0), "reviewed_at": datetime(2026, 4, 2, 15, 0), "reviewer_id": 2, "detail": "정보처리기능사 취득", "description": "기능사 합격", "area_id": 2, "scoring_type": 1, "item_max_score": 20.0, "item_name": "정보처리기능사 취득", "area_name": "전문기술역량", "area_grade": 1, "student_name": "홍길동", "is_active": 1},
                    {"submission_id": 3, "student_id": 101, "item_id": 3, "status_code": 3, "granted_score": 20.0, "created_at": datetime(2026, 4, 3, 10, 0), "reviewed_at": datetime(2026, 4, 3, 16, 0), "reviewer_id": 3, "detail": "교내 봉사 20시간", "description": "봉사활동 확인서", "area_id": 3, "scoring_type": 1, "item_max_score": 20.0, "item_name": "교내 봉사활동 20시간", "area_name": "인성/직업의식", "area_grade": 1, "student_name": "홍길동", "is_active": 1},
                    {"submission_id": 4, "student_id": 101, "item_id": 4, "status_code": 3, "granted_score": 20.0, "created_at": datetime(2026, 4, 4, 10, 0), "reviewed_at": datetime(2026, 4, 4, 17, 0), "reviewer_id": 4, "detail": "독서 10권 서평 제출", "description": "독서 마일리지", "area_id": 4, "scoring_type": 1, "item_max_score": 20.0, "item_name": "독서 마일리지 및 서평", "area_name": "인문학적 소양", "area_grade": 1, "student_name": "홍길동", "is_active": 1},
                    {"submission_id": 5, "student_id": 101, "item_id": 5, "status_code": 3, "granted_score": 20.0, "created_at": datetime(2026, 4, 5, 10, 0), "reviewed_at": datetime(2026, 4, 5, 18, 0), "reviewer_id": 5, "detail": "토익 650점 성적표", "description": "공인 토익", "area_id": 5, "scoring_type": 1, "item_max_score": 20.0, "item_name": "공인 토익 500점 이상", "area_name": "외국어 능력", "area_grade": 1, "student_name": "홍길동", "is_active": 1},

                    # 학생 102 (이순신): 심사 대기건 1건 보유 -> 검토중
                    {"submission_id": 6, "student_id": 102, "item_id": 2, "status_code": 1, "granted_score": 0.0, "created_at": datetime(2026, 4, 6, 10, 0), "reviewed_at": None, "reviewer_id": None, "detail": "정보처리기능사 응시표", "description": "심사 대기중", "area_id": 2, "scoring_type": 1, "item_max_score": 20.0, "item_name": "정보처리기능사 취득", "area_name": "전문기술역량", "area_grade": 1, "student_name": "이순신", "is_active": 1},

                    # 학생 103 (강감찬): 미달성 상태 -> 보완 필요
                    {"submission_id": 7, "student_id": 103, "item_id": 7, "status_code": 3, "granted_score": 10.0, "created_at": datetime(2026, 4, 7, 10, 0), "reviewed_at": datetime(2026, 4, 7, 11, 0), "reviewer_id": 2, "detail": "산업기사 필기 합격", "description": "필기만 합격", "area_id": 7, "scoring_type": 1, "item_max_score": 20.0, "item_name": "산업기사 필기/실기", "area_name": "전문기술역량", "area_grade": 2, "student_name": "강감찬", "is_active": 1},
                ]

        # 5. 상벌점 목록 조회 (merits)
        elif "FROM merits" in sql_clean:
            if self.conn.custom_merits_rows is not None:
                self._results = self.conn.custom_merits_rows
            else:
                self._results = [
                    {"merits_point_id": 1, "student_id": 101, "teachers_id": 1, "type": "+", "points": 2.0, "reason": "모범선행상", "related_area": "인성/직업의식", "occurred_at": date(2026, 4, 10), "created_at": datetime(2026, 4, 10, 9, 0), "is_reflected": 1, "student_name": "홍길동"},
                    {"merits_point_id": 2, "student_id": 103, "teachers_id": 2, "type": "-", "points": 1.0, "reason": "기숙사 정리 미흡", "related_area": "인성/직업의식", "occurred_at": date(2026, 4, 12), "created_at": datetime(2026, 4, 12, 10, 0), "is_reflected": 1, "student_name": "강감찬"},
                ]
        else:
            self._results = []

    async def fetchall(self):
        return list(self._results)

    async def fetchone(self):
        if self._results:
            return self._results[0]
        return None


class MockAdminAIDatabaseConnection:
    """Mock 비동기 DB 커넥션"""

    def __init__(self):
        self.custom_years_rows = None
        self.custom_criteria_rows = None
        self.custom_students_rows = None
        self.custom_submissions_rows = None
        self.custom_merits_rows = None

    def cursor(self, *args, **kwargs):
        return MockAdminAIDatabaseCursor(self)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def autocommit(self, val: bool):
        pass


class TestAIAdminServices(unittest.TestCase):
    """5.3 관리자용 AI 서비스 단위 및 통합 테스트"""

    def setUp(self):
        self.client = TestClient(app)
        self.mock_db = MockAdminAIDatabaseConnection()

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
        self.admin_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000099",
            "id": "admin1",
            "role": "admin",
            "name": "관리자",
        })
        self.admin_numeric_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000098",
            "id": "admin2",
            "role": 2,
            "name": "숫자관리자",
        })
        self.admin_str_numeric_token = create_access_token({
            "sub": "00000000-0000-0000-0000-000000000097",
            "id": "admin3",
            "role": "2",
            "name": "문자열숫자관리자",
        })

        # 환경변수 격리 (실제 API 호출 방지)
        self._orig_admin_api_key = os.environ.get("ADMIN_AI_API_KEY")
        self._orig_gemini_api_key = os.environ.get("GEMINI_API_KEY")
        self._orig_ai_api_key = os.environ.get("AI_API_KEY")
        self._orig_admin_model = os.environ.get("GEMINI_ADMIN_MODEL")

        os.environ.pop("ADMIN_AI_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("AI_API_KEY", None)
        os.environ.pop("GEMINI_ADMIN_MODEL", None)

    def tearDown(self):
        app.dependency_overrides.clear()
        if self._orig_admin_api_key is not None:
            os.environ["ADMIN_AI_API_KEY"] = self._orig_admin_api_key
        else:
            os.environ.pop("ADMIN_AI_API_KEY", None)

        if self._orig_gemini_api_key is not None:
            os.environ["GEMINI_API_KEY"] = self._orig_gemini_api_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)

        if self._orig_ai_api_key is not None:
            os.environ["AI_API_KEY"] = self._orig_ai_api_key
        else:
            os.environ.pop("AI_API_KEY", None)

        if self._orig_admin_model is not None:
            os.environ["GEMINI_ADMIN_MODEL"] = self._orig_admin_model
        else:
            os.environ.pop("GEMINI_ADMIN_MODEL", None)

    # ==========================================================================
    # 1. RBAC 인가 및 접근 제어 검증 (5.3.1 & 5.3.2)
    # ==========================================================================

    def test_summary_report_forbidden_for_student(self):
        """학생 계정으로 관리자 총괄 리포트 생성 호출 시 403 차단"""
        resp = self.client.post(
            "/api/ai/admin/summary-report",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "FORBIDDEN_ROLE")

    def test_summary_report_forbidden_for_teacher(self):
        """교사 계정으로 관리자 총괄 리포트 생성 호출 시 403 차단"""
        resp = self.client.post(
            "/api/ai/admin/summary-report",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "FORBIDDEN_ROLE")

    def test_summary_report_unauthorized(self):
        """비인증 요청 시 401 인증 오류 반환"""
        resp = self.client.post("/api/ai/admin/summary-report", json={"year": 2026})
        self.assertEqual(resp.status_code, 401)

    def test_summary_report_allowed_for_admin_variants(self):
        """관리자 role('admin', 2, '2')의 경우 모두 정상 허용"""
        for token in [self.admin_token, self.admin_numeric_token, self.admin_str_numeric_token]:
            resp = self.client.post(
                "/api/ai/admin/summary-report",
                headers={"Authorization": f"Bearer {token}"},
                json={"year": 2026},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["success"])

    def test_audit_anomalies_forbidden_for_student(self):
        """학생 계정으로 부정 의심 탐지 API 호출 시 403 차단"""
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.student_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "FORBIDDEN_ROLE")

    def test_audit_anomalies_forbidden_for_teacher(self):
        """교사 계정으로 부정 의심 탐지 API 호출 시 403 차단"""
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.teacher_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 403)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "FORBIDDEN_ROLE")

    def test_audit_anomalies_unauthorized(self):
        """비인증 요청 시 401 인증 오류 반환"""
        resp = self.client.post("/api/ai/admin/audit-anomalies", json={"year": 2026})
        self.assertEqual(resp.status_code, 401)

    def test_audit_anomalies_allowed_for_admin_variants(self):
        """관리자 role('admin', 2, '2')의 경우 모두 정상 허용"""
        for token in [self.admin_token, self.admin_numeric_token, self.admin_str_numeric_token]:
            resp = self.client.post(
                "/api/ai/admin/audit-anomalies",
                headers={"Authorization": f"Bearer {token}"},
                json={"year": 2026},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["success"])

    # ==========================================================================
    # 2. Rate Limiting 검증 (5.3.1 & 5.3.2)
    # ==========================================================================

    def test_summary_report_rate_limit_exceeded(self):
        """summary-report 분당 30회 초과 시 429 반환"""
        self.redis_store["rate_limit:ai:admin_report:ip:testclient"] = 31
        resp = self.client.post(
            "/api/ai/admin/summary-report",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 429)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "RATE_LIMIT_EXCEEDED")

    def test_audit_anomalies_rate_limit_exceeded(self):
        """audit-anomalies 분당 30회 초과 시 429 반환"""
        self.redis_store["rate_limit:ai:admin_audit:ip:testclient"] = 31
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 429)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "RATE_LIMIT_EXCEEDED")

    # ==========================================================================
    # 3. 5.3.1 전교생 인증 현황 통계 분석 및 총괄 리포트 상세 검증
    # ==========================================================================

    def test_summary_report_success_default_year(self):
        """연도 미지정 시 기본 활성 학년도(2026)로 통계 집계 및 리포트 생성"""
        resp = self.client.post(
            "/api/ai/admin/summary-report",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]

        # 연도 확인
        self.assertEqual(data["year"], 2026)

        # 핵심 지표(KPI) 확인
        kpi = data["kpi"]
        self.assertEqual(kpi["totalStudents"], 3)
        self.assertEqual(kpi["certifiedCount"], 1)  # 홍길동
        self.assertEqual(kpi["reviewingCount"], 1)  # 이순신
        self.assertEqual(kpi["supplementCount"], 1)  # 강감찬
        self.assertAlmostEqual(kpi["certRate"], 33.3, places=1)
        self.assertAlmostEqual(kpi["reviewRate"], 33.3, places=1)
        self.assertAlmostEqual(kpi["supplementRate"], 33.3, places=1)

        # 판정 분포 확인
        status_dist = data["statusDistribution"]
        self.assertEqual(status_dist["certified"]["count"], 1)
        self.assertEqual(status_dist["reviewing"]["count"], 1)
        self.assertEqual(status_dist["supplement"]["count"], 1)

        # 영역별 통계 확인
        area_stats = data["areaStats"]
        self.assertGreaterEqual(len(area_stats), 5)
        area_names = [a["area"] for a in area_stats]
        self.assertIn("전문기술역량", area_names)
        self.assertIn("외국어 능력", area_names)

        # 학년별 통계 확인
        grade_stats = data["gradeStats"]
        self.assertEqual(len(grade_stats), 3)  # 1학년, 2학년, 3학년
        g1 = next(g for g in grade_stats if g["grade"] == 1)
        self.assertEqual(g1["studentCount"], 2)
        self.assertEqual(g1["certifiedCount"], 1)
        self.assertAlmostEqual(g1["certRate"], 50.0, places=1)

        # 상벌점 통계 확인
        merit_stats = data["meritStats"]
        self.assertEqual(merit_stats["totalMeritPoints"], 2.0)
        self.assertEqual(merit_stats["totalDemeritPoints"], 1.0)
        self.assertEqual(merit_stats["netPoints"], 1.0)
        self.assertEqual(merit_stats["studentsWithDemerits"], 1)

        # 리포트 마크다운 및 권고사항 확인 (Fallback 렌더링 검증)
        self.assertIn("summaryReport", data)
        self.assertIn("reportMarkdown", data)
        self.assertIn("keyInsights", data)
        self.assertIn("recommendations", data)
        self.assertIn("2026학년도 마이스터 역량인증제", data["summaryReport"])
        self.assertGreater(len(data["keyInsights"]), 0)
        self.assertGreater(len(data["recommendations"]), 0)

    def test_summary_report_with_specific_year(self):
        """특정 학년도(2025) 지정 시 해당 학년도 조회"""
        resp = self.client.post(
            "/api/ai/admin/summary-report",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2025},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["year"], 2025)

    def test_summary_report_nonexistent_year_returns_404(self):
        """존재하지 않는 학년도 요청 시 404 NOT_FOUND"""
        resp = self.client.post(
            "/api/ai/admin/summary-report",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 1999},
        )
        self.assertEqual(resp.status_code, 404)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["code"], "YEAR_NOT_FOUND")

    def test_summary_report_with_grade_filter(self):
        """특정 학년(1학년) 필터링 시 해당 학년 통계 집중 반영"""
        resp = self.client.post(
            "/api/ai/admin/summary-report",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026, "grade": 1},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["grade"], 1)
        grade_stats = data["gradeStats"]
        self.assertEqual(len(grade_stats), 1)
        self.assertEqual(grade_stats[0]["grade"], 1)

    def test_summary_report_empty_dataset(self):
        """학생 데이터가 없는 경우 0명 기준으로 안전하게 통계 산출"""
        self.mock_db.custom_students_rows = []
        self.mock_db.custom_submissions_rows = []
        self.mock_db.custom_merits_rows = []

        resp = self.client.post(
            "/api/ai/admin/summary-report",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["kpi"]["totalStudents"], 0)
        self.assertEqual(data["kpi"]["certRate"], 0.0)
        self.assertEqual(data["kpi"]["averageTotalScore"], 0.0)

    @patch("routers.ai.call_gemini_api")
    def test_summary_report_with_mocked_gemini_success(self, mock_gemini):
        """Gemini API 정상 응답 시 LLM 생성 총괄 리포트 및 인사이트 파싱 반영"""
        mock_gemini.return_value = json.dumps({
            "summaryReport": "## Gemini 총괄 분석 리포트\n전교생 역량 성취도가 전년 대비 15% 향상되었습니다.",
            "keyInsights": ["전문기술역량 취득률 급상승", "외국어 영역 토익 취득 프로그램 성과 가시화"],
            "recommendations": ["산업기사 대비반 신설 권장", "원어민 회화 동아리 확대 운영"],
        })

        with patch.dict(os.environ, {"ADMIN_AI_API_KEY": "test-admin-key"}):
            resp = self.client.post(
                "/api/ai/admin/summary-report",
                headers={"Authorization": f"Bearer {self.admin_token}"},
                json={"year": 2026},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()["data"]
            self.assertIn("Gemini 총괄 분석 리포트", data["summaryReport"])
            self.assertEqual(len(data["keyInsights"]), 2)
            self.assertEqual(data["keyInsights"][0], "전문기술역량 취득률 급상승")
            self.assertEqual(len(data["recommendations"]), 2)

    @patch("routers.ai.call_gemini_api")
    def test_summary_report_fallback_on_gemini_failure(self, mock_gemini):
        """Gemini API 오류/타임아웃 발생 시 규칙 기반 Graceful Fallback 리포트 반환"""
        mock_gemini.return_value = None  # 타임아웃/오류 모의

        with patch.dict(os.environ, {"ADMIN_AI_API_KEY": "test-admin-key"}):
            resp = self.client.post(
                "/api/ai/admin/summary-report",
                headers={"Authorization": f"Bearer {self.admin_token}"},
                json={"year": 2026},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()["data"]
            # Fallback 텍스트에 실제 통계 수치 포함 확인
            self.assertIn("2026학년도 마이스터 역량인증제", data["summaryReport"])
            self.assertIn("33.3%", data["summaryReport"])
            self.assertGreater(len(data["recommendations"]), 0)

    # ==========================================================================
    # 4. 5.3.2 부정 의심 제출 탐지 및 감사 리포트 (Anomaly Detection) 전수 검증
    # ==========================================================================

    def test_audit_anomalies_clean_dataset(self):
        """이상 징후가 없는 정상 데이터셋인 경우 riskScore=0.0, NORMAL 반환"""
        # 정상적인 단일 승인건
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 10,
                "student_id": 101,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 1, 10, 0),
                "reviewed_at": datetime(2026, 4, 1, 14, 0),
                "detail": "정보처리기능사",
                "description": "기능사 합격",
                "file_path": "/uploads/cert1.pdf",
                "student_name": "홍길동",
            }
        ]
        self.mock_db.custom_merits_rows = [
            {
                "merits_point_id": 1,
                "student_id": 101,
                "teachers_id": 1,
                "type": "+",
                "points": 2.0,
                "reason": "선행상",
                "occurred_at": date(2026, 4, 10),
                "created_at": datetime(2026, 4, 10, 9, 0),
                "is_reflected": 1,
                "student_name": "홍길동",
            }
        ]

        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["totalAnomalies"], 0)
        self.assertEqual(len(data["anomalies"]), 0)
        self.assertEqual(data["riskScore"], 0.0)
        self.assertEqual(data["riskLevel"], "NORMAL")
        self.assertIn("부정 의심 제출 및 이상 징후가 감지되지 않았습니다", data["summary"])

    def test_audit_anomalies_detects_excessive_single_merit(self):
        """1) 단일 사안으로 10점 이상의 과도한 상벌점 부여 탐지 (EXCESSIVE_MERIT_POINTS)"""
        self.mock_db.custom_submissions_rows = []
        self.mock_db.custom_merits_rows = [
            {
                "merits_point_id": 101,
                "student_id": 101,
                "teachers_id": 1,
                "type": "+",
                "points": 15.0,  # 15점 대량 상점
                "reason": "특별 교내 기여 포상",
                "occurred_at": date(2026, 4, 10),
                "created_at": datetime(2026, 4, 10, 9, 0),
                "is_reflected": 1,
                "student_name": "홍길동",
            }
        ]

        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["totalAnomalies"], 1)
        anomaly = data["anomalies"][0]
        self.assertEqual(anomaly["issueType"], "EXCESSIVE_MERIT_POINTS")
        self.assertAlmostEqual(anomaly["riskScore"], 0.85, places=2)
        self.assertEqual(anomaly["studentId"], 101)
        self.assertIn("15.0점의 과도한 상점", anomaly["reason"])

    def test_audit_anomalies_detects_rapid_merit_grant(self):
        """1) 7일 이내 동일 학생에게 집중 상벌점 부여 탐지 (RAPID_MERIT_GRANT)"""
        self.mock_db.custom_submissions_rows = []
        base_dt = datetime(2026, 4, 10, 10, 0)
        self.mock_db.custom_merits_rows = [
            {"merits_point_id": 1, "student_id": 102, "points": 5.0, "type": "+", "reason": "1", "occurred_at": date(2026, 4, 10), "created_at": base_dt, "is_reflected": 1, "student_name": "이순신"},
            {"merits_point_id": 2, "student_id": 102, "points": 5.0, "type": "+", "reason": "2", "occurred_at": date(2026, 4, 11), "created_at": base_dt + timedelta(days=1), "is_reflected": 1, "student_name": "이순신"},
            {"merits_point_id": 3, "student_id": 102, "points": 6.0, "type": "+", "reason": "3", "occurred_at": date(2026, 4, 12), "created_at": base_dt + timedelta(days=2), "is_reflected": 1, "student_name": "이순신"},
        ]

        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        issue_types = [a["issueType"] for a in data["anomalies"]]
        self.assertIn("RAPID_MERIT_GRANT", issue_types)

    def test_audit_anomalies_detects_exceeded_max_score(self):
        """2) 배점 한도 초과 제출건 탐지 (EXCEEDED_MAX_SCORE)"""
        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 201,
                "student_id": 101,
                "item_id": 1,
                "status_code": 3,
                "granted_score": 35.0,  # 최대 배점 20점 초과
                "item_max_score": 20.0,
                "item_name": "직업기초 프로젝트",
                "is_active": 1,
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 1, 10, 0),
                "reviewed_at": datetime(2026, 4, 2, 10, 0),
                "detail": "프로젝트",
                "description": "설명",
                "student_name": "홍길동",
            }
        ]

        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["totalAnomalies"], 1)
        anomaly = data["anomalies"][0]
        self.assertEqual(anomaly["issueType"], "EXCEEDED_MAX_SCORE")
        self.assertAlmostEqual(anomaly["riskScore"], 0.95, places=2)
        self.assertIn("최대 배점(20.0점)을 초과", anomaly["reason"])

    def test_audit_anomalies_detects_negative_score(self):
        """2) 음수 점수 부여 비정상 건 탐지 (NEGATIVE_SCORE)"""
        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 202,
                "student_id": 101,
                "item_id": 1,
                "status_code": 3,
                "granted_score": -5.0,  # 음수 점수
                "item_max_score": 20.0,
                "item_name": "직업기초 프로젝트",
                "is_active": 1,
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 1, 10, 0),
                "reviewed_at": datetime(2026, 4, 2, 10, 0),
                "detail": "프로젝트",
                "description": "설명",
                "student_name": "홍길동",
            }
        ]

        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        anomaly = data["anomalies"][0]
        self.assertEqual(anomaly["issueType"], "NEGATIVE_SCORE")
        self.assertAlmostEqual(anomaly["riskScore"], 0.95, places=2)

    def test_audit_anomalies_detects_inactive_item_scored(self):
        """3) 비활성 항목 점수 부여 건 탐지 (INACTIVE_ITEM_SCORED)"""
        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 301,
                "student_id": 101,
                "item_id": 99,
                "status_code": 3,
                "granted_score": 15.0,
                "item_max_score": 20.0,
                "item_name": "폐지된 과거 평가 항목",
                "is_active": 0,  # 비활성 항목
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 1, 10, 0),
                "reviewed_at": datetime(2026, 4, 2, 10, 0),
                "detail": "과거 활동",
                "description": "설명",
                "student_name": "홍길동",
            }
        ]

        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        anomaly = data["anomalies"][0]
        self.assertEqual(anomaly["issueType"], "INACTIVE_ITEM_SCORED")
        self.assertAlmostEqual(anomaly["riskScore"], 0.85, places=2)
        self.assertIn("비활성화된 평가 항목", anomaly["reason"])

    def test_audit_anomalies_detects_ultra_fast_approval(self):
        """4) 제출 후 10초 이내 초고속 승인 의심 건 탐지 (ULTRA_FAST_APPROVAL)"""
        self.mock_db.custom_merits_rows = []
        c_time = datetime(2026, 4, 5, 14, 0, 0)
        r_time = datetime(2026, 4, 5, 14, 0, 4)  # 4초 만에 승인
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 401,
                "student_id": 101,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 1,
                "created_at": c_time,
                "reviewed_at": r_time,
                "detail": "기능사",
                "description": "설명",
                "student_name": "홍길동",
            }
        ]

        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        anomaly = data["anomalies"][0]
        self.assertEqual(anomaly["issueType"], "ULTRA_FAST_APPROVAL")
        self.assertAlmostEqual(anomaly["riskScore"], 0.80, places=2)
        self.assertIn("4초 만에 초고속 승인", anomaly["reason"])

    def test_audit_anomalies_detects_reviewer_concentration(self):
        """4) 특정 심사자가 1분 이내 5건 이상 집중 대량 승인한 건 탐지 (REVIEWER_CONCENTRATION)"""
        self.mock_db.custom_merits_rows = []
        base_rev_time = datetime(2026, 4, 15, 16, 0, 0)
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 500 + i,
                "student_id": 100 + i,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 88,  # 동일 심사자 88
                "created_at": datetime(2026, 4, 1, 10, 0),
                "reviewed_at": base_rev_time + timedelta(seconds=i * 5),  # 25초 이내에 6건 승인
                "detail": f"기능사_{i}",
                "description": f"설명_{i}",
                "student_name": f"학생_{i}",
            }
            for i in range(6)
        ]

        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        issue_types = [a["issueType"] for a in data["anomalies"]]
        self.assertIn("REVIEWER_CONCENTRATION", issue_types)

    def test_audit_anomalies_detects_duplicate_submission(self):
        """5) 동일 학생이 동일 항목에 동일 활동명으로 중복 승인된 건 탐지 (DUPLICATE_SUBMISSION)"""
        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 601,
                "student_id": 101,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 1, 10, 0),
                "reviewed_at": datetime(2026, 4, 2, 10, 0),
                "detail": "정보처리기능사 최종합격",
                "description": "합격증명서 제출",
                "file_path": "/uploads/cert1.pdf",
                "student_name": "홍길동",
            },
            {
                "submission_id": 602,  # 중복 제출건
                "student_id": 101,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 2,
                "created_at": datetime(2026, 4, 5, 10, 0),
                "reviewed_at": datetime(2026, 4, 6, 10, 0),
                "detail": "정보처리기능사 최종합격",  # 동일 활동명
                "description": "합격증명서 재제출",
                "file_path": "/uploads/cert1_copy.pdf",
                "student_name": "홍길동",
            },
        ]

        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        anomaly = data["anomalies"][0]
        self.assertEqual(anomaly["issueType"], "DUPLICATE_SUBMISSION")
        self.assertAlmostEqual(anomaly["riskScore"], 0.90, places=2)
        self.assertEqual(anomaly["submissionId"], 602)
        self.assertIn("중복 승인되었습니다", anomaly["reason"])

    def test_audit_anomalies_threshold_and_limit_filtering(self):
        """임계치(threshold) 및 limit 매개변수 필터링 정상 동작 검증"""
        self.mock_db.custom_merits_rows = []
        c_time = datetime(2026, 4, 5, 14, 0, 0)
        r_time = datetime(2026, 4, 5, 14, 0, 4)
        self.mock_db.custom_submissions_rows = [
            # 1. 0.95 위험도 (EXCEEDED_MAX_SCORE)
            {"submission_id": 1, "student_id": 101, "item_id": 1, "status_code": 3, "granted_score": 30.0, "item_max_score": 20.0, "item_name": "항목1", "is_active": 1, "reviewer_id": 1, "created_at": datetime(2026, 4, 1), "reviewed_at": datetime(2026, 4, 2), "detail": "1", "student_name": "홍"},
            # 2. 0.85 위험도 (INACTIVE_ITEM_SCORED)
            {"submission_id": 2, "student_id": 102, "item_id": 2, "status_code": 3, "granted_score": 10.0, "item_max_score": 20.0, "item_name": "항목2", "is_active": 0, "reviewer_id": 1, "created_at": datetime(2026, 4, 1), "reviewed_at": datetime(2026, 4, 2), "detail": "2", "student_name": "이"},
            # 3. 0.80 위험도 (ULTRA_FAST_APPROVAL)
            {"submission_id": 3, "student_id": 103, "item_id": 3, "status_code": 3, "granted_score": 10.0, "item_max_score": 20.0, "item_name": "항목3", "is_active": 1, "reviewer_id": 1, "created_at": c_time, "reviewed_at": r_time, "detail": "3", "student_name": "강"},
        ]

        # threshold = 0.90 테스트: 0.95 건만 1건 반환되어야 함
        resp_thresh = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026, "threshold": 0.90},
        )
        self.assertEqual(resp_thresh.status_code, 200)
        data_thresh = resp_thresh.json()["data"]
        self.assertEqual(len(data_thresh["anomalies"]), 1)
        self.assertEqual(data_thresh["anomalies"][0]["issueType"], "EXCEEDED_MAX_SCORE")

        # limit = 2 테스트: 상위 2건만 반환되어야 함
        resp_limit = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026, "limit": 2},
        )
        self.assertEqual(resp_limit.status_code, 200)
        data_limit = resp_limit.json()["data"]
        self.assertEqual(len(data_limit["anomalies"]), 2)

    @patch("routers.ai.call_gemini_api")
    def test_audit_anomalies_with_mocked_gemini_success(self, mock_gemini):
        """Gemini API 호출 성공 시 종합 감사 소견 및 권고사항 파싱 반영"""
        mock_gemini.return_value = json.dumps({
            "summary": "Gemini 감사 총평: 데이터 정합성 결함 1건 탐지",
            "auditAnalysis": "## 종합 감사 소견\n배점 한도 초과 건에 대한 즉시 시정이 요구됩니다.",
            "recommendations": ["관리자 재계산 배치 실행", "심사자 주의 조치"],
        })

        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {"submission_id": 1, "student_id": 101, "item_id": 1, "status_code": 3, "granted_score": 30.0, "item_max_score": 20.0, "item_name": "항목1", "is_active": 1, "reviewer_id": 1, "created_at": datetime(2026, 4, 1), "reviewed_at": datetime(2026, 4, 2), "detail": "1", "student_name": "홍"},
        ]

        with patch.dict(os.environ, {"ADMIN_AI_API_KEY": "test-admin-key"}):
            resp = self.client.post(
                "/api/ai/admin/audit-anomalies",
                headers={"Authorization": f"Bearer {self.admin_token}"},
                json={"year": 2026},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()["data"]
            self.assertEqual(data["summary"], "Gemini 감사 총평: 데이터 정합성 결함 1건 탐지")
            self.assertIn("종합 감사 소견", data["auditAnalysis"])
            self.assertEqual(len(data["recommendations"]), 2)

    # ==========================================================================
    # 5. API Key 및 모델 분리 유연성 검증
    # ==========================================================================

    def test_admin_ai_config_priority(self):
        """ADMIN_AI_API_KEY > GEMINI_API_KEY > AI_API_KEY 우선순위 검증"""
        # 1. ADMIN_AI_API_KEY 최우선
        with patch.dict(os.environ, {
            "ADMIN_AI_API_KEY": "admin-key-123",
            "GEMINI_API_KEY": "gemini-key-456",
            "AI_API_KEY": "ai-key-789",
        }):
            key, model = get_admin_ai_config()
            self.assertEqual(key, "admin-key-123")

        # 2. ADMIN_AI_API_KEY 없을 때 GEMINI_API_KEY 사용
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "gemini-key-456",
            "AI_API_KEY": "ai-key-789",
        }, clear=True):
            key, model = get_admin_ai_config()
            self.assertEqual(key, "gemini-key-456")

        # 3. AI_API_KEY만 있을 때 호환 사용
        with patch.dict(os.environ, {
            "AI_API_KEY": "ai-key-789",
        }, clear=True):
            key, model = get_admin_ai_config()
            self.assertEqual(key, "ai-key-789")

        # 4. 아무것도 없을 때 빈 문자열
        with patch.dict(os.environ, {}, clear=True):
            key, model = get_admin_ai_config()
            self.assertEqual(key, "")

    def test_admin_ai_model_normalization(self):
        """GEMINI_ADMIN_MODEL 환경변수 모델 정규화 검증"""
        with patch.dict(os.environ, {"GEMINI_ADMIN_MODEL": "gemini-3.5-flash-lite"}):
            key, model = get_admin_ai_config()
            self.assertEqual(model, "gemini-3.5-flash-lite")

        with patch.dict(os.environ, {"GEMINI_ADMIN_MODEL": "gemini-2.5-flash"}):
            key, model = get_admin_ai_config()
            self.assertEqual(model, "gemini-2.5-flash")

        with patch.dict(os.environ, {}, clear=True):
            key, model = get_admin_ai_config()
            self.assertEqual(model, "gemini-3.5-flash-lite")

    # ==========================================================================
    # 6. 유틸리티 및 안전성 검증
    # ==========================================================================

    def test_parse_datetime_safe_variants(self):
        """parse_datetime_safe 다양한 입력 타입 무결성 검증"""
        now = datetime.now()
        today = date.today()
        self.assertEqual(parse_datetime_safe(now), now)
        self.assertEqual(parse_datetime_safe(today), datetime.combine(today, datetime.min.time()))
        self.assertEqual(parse_datetime_safe("2026-04-10 12:30:00"), datetime(2026, 4, 10, 12, 30, 0))
        self.assertEqual(parse_datetime_safe("2026-04-10T12:30:00"), datetime(2026, 4, 10, 12, 30, 0))
        self.assertEqual(parse_datetime_safe("2026-04-10"), datetime(2026, 4, 10, 0, 0, 0))
        self.assertIsNone(parse_datetime_safe(None))
        self.assertIsNone(parse_datetime_safe("invalid-date-string"))

    def test_sanitize_and_mask_text_pii(self):
        """개인정보(주민등록번호, 전화번호) 마스킹 검증"""
        raw = "학생 홍길동(060101-3123456), 연락처 010-1234-5678 문의"
        masked = sanitize_and_mask_text(raw)
        self.assertNotIn("060101-3123456", masked)
        self.assertNotIn("010-1234-5678", masked)
        self.assertIn("[주민등록번호 마스킹]", masked)
        self.assertIn("[전화번호 마스킹]", masked)


    def test_audit_anomalies_schema_compliance_with_tech_spec(self):
        """Tech_spec.md 5.3.2 반환 규격(flaggedItems 내 type, targetId, riskLevel, reason) 전수 준수 검증"""
        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 1001,
                "student_id": 101,
                "item_id": 1,
                "status_code": 3,
                "granted_score": 50.0,
                "item_max_score": 20.0,
                "item_name": "직업기초 프로젝트",
                "is_active": 1,
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 1),
                "reviewed_at": datetime(2026, 4, 2),
                "detail": "프로젝트",
                "description": "설명",
                "student_name": "홍길동",
            }
        ]
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("flaggedItems", data)
        self.assertIn("anomalies", data)
        item = data["flaggedItems"][0]
        # Tech_spec.md 5.3.2 필수 키
        self.assertEqual(item["type"], "EXCEEDED_MAX_SCORE")
        self.assertEqual(item["targetId"], 1001)
        self.assertEqual(item["riskLevel"], "HIGH")
        self.assertTrue(len(item["reason"]) > 0)
        # TODO.md 5.3.2 호환 키
        self.assertEqual(item["submissionId"], 1001)
        self.assertEqual(item["studentId"], 101)
        self.assertEqual(item["issueType"], "EXCEEDED_MAX_SCORE")

    def test_audit_anomalies_detects_rapid_merit_grant_with_two_high_merits(self):
        """7일 이내 2건의 상벌점만으로 15점 이상 누적된 케이스 정상 탐지 (이전 구현의 len>=3 가드 결함 보완 검증)"""
        self.mock_db.custom_submissions_rows = []
        base_dt = datetime(2026, 4, 10, 10, 0)
        self.mock_db.custom_merits_rows = [
            {"merits_point_id": 11, "student_id": 102, "points": 8.0, "type": "+", "reason": "우수 활동", "occurred_at": date(2026, 4, 10), "created_at": base_dt, "is_reflected": 1, "student_name": "이순신"},
            {"merits_point_id": 12, "student_id": 102, "points": 8.0, "type": "+", "reason": "교내 기여", "occurred_at": date(2026, 4, 12), "created_at": base_dt + timedelta(days=2), "is_reflected": 1, "student_name": "이순신"},
        ]
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        issue_types = [a["issueType"] for a in data["anomalies"]]
        self.assertIn("RAPID_MERIT_GRANT", issue_types)

    def test_audit_anomalies_does_not_flag_three_small_merits(self):
        """7일 이내 3건이지만 각 1점으로 총점이 경미한 정상 상벌점은 RAPID_MERIT_GRANT로 오탐지하지 않음"""
        self.mock_db.custom_submissions_rows = []
        base_dt = datetime(2026, 4, 10, 10, 0)
        self.mock_db.custom_merits_rows = [
            {"merits_point_id": 21, "student_id": 103, "points": 1.0, "type": "+", "reason": "청소", "occurred_at": date(2026, 4, 10), "created_at": base_dt, "is_reflected": 1, "student_name": "강감찬"},
            {"merits_point_id": 22, "student_id": 103, "points": 1.0, "type": "+", "reason": "질문", "occurred_at": date(2026, 4, 11), "created_at": base_dt + timedelta(days=1), "is_reflected": 1, "student_name": "강감찬"},
            {"merits_point_id": 23, "student_id": 103, "points": 1.0, "type": "+", "reason": "인사", "occurred_at": date(2026, 4, 12), "created_at": base_dt + timedelta(days=2), "is_reflected": 1, "student_name": "강감찬"},
        ]
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        issue_types = [a["issueType"] for a in data["anomalies"]]
        self.assertNotIn("RAPID_MERIT_GRANT", issue_types)

    def test_audit_anomalies_detects_negative_review_time_diff(self):
        """심사일시가 제출일시보다 앞선 비정상 타임스탬프 승인건 탐지 (diff_sec < 0)"""
        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 1101,
                "student_id": 101,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 10, 15, 0, 0),
                "reviewed_at": datetime(2026, 4, 10, 14, 0, 0),  # 심사가 제출보다 1시간 앞섬
                "detail": "기능사",
                "description": "설명",
                "student_name": "홍길동",
            }
        ]
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        issue_types = [a["issueType"] for a in data["anomalies"]]
        self.assertIn("ULTRA_FAST_APPROVAL", issue_types)

    def test_audit_anomalies_detects_duplicate_submission_by_link_url(self):
        """동일 학생이 동일 항목에 동일 link_url로 중복 승인된 건 탐지"""
        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 1201,
                "student_id": 101,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 1),
                "reviewed_at": datetime(2026, 4, 2),
                "detail": "첫번째 제출",
                "description": "설명 1",
                "link_url": "https://github.com/student/repo-demo",
                "student_name": "홍길동",
            },
            {
                "submission_id": 1202,
                "student_id": 101,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 2,
                "created_at": datetime(2026, 4, 5),
                "reviewed_at": datetime(2026, 4, 6),
                "detail": "두번째 제출",
                "description": "설명 2",
                "link_url": "https://github.com/student/repo-demo",
                "student_name": "홍길동",
            },
        ]
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        issue_types = [a["issueType"] for a in data["anomalies"]]
        self.assertIn("DUPLICATE_SUBMISSION", issue_types)

    def test_audit_anomalies_detects_duplicate_submission_with_whitespace_and_punctuation(self):
        """활동명에 공백이나 마침표 등 특수문자가 미세하게 달라도 정규화하여 중복 탐지"""
        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 1301,
                "student_id": 101,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 1),
                "reviewed_at": datetime(2026, 4, 2),
                "detail": "정보처리기능사 실기 최종합격",
                "description": "설명 1",
                "student_name": "홍길동",
            },
            {
                "submission_id": 1302,
                "student_id": 101,
                "item_id": 2,
                "status_code": 3,
                "granted_score": 20.0,
                "item_max_score": 20.0,
                "item_name": "정보처리기능사 취득",
                "is_active": 1,
                "reviewer_id": 2,
                "created_at": datetime(2026, 4, 5),
                "reviewed_at": datetime(2026, 4, 6),
                "detail": "정보처리기능사  실기  최종합격!",  # 다중 공백 및 느낌표
                "description": "설명 2",
                "student_name": "홍길동",
            },
        ]
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        issue_types = [a["issueType"] for a in data["anomalies"]]
        self.assertIn("DUPLICATE_SUBMISSION", issue_types)

    def test_audit_anomalies_inactive_item_as_string_zero(self):
        """is_active가 문자열 '0'으로 전달된 경우에도 비활성 항목으로 정확히 판별"""
        self.mock_db.custom_merits_rows = []
        self.mock_db.custom_submissions_rows = [
            {
                "submission_id": 1401,
                "student_id": 101,
                "item_id": 99,
                "status_code": 3,
                "granted_score": 10.0,
                "item_max_score": 20.0,
                "item_name": "구버전 항목",
                "is_active": "0",  # 문자열 '0'
                "reviewer_id": 1,
                "created_at": datetime(2026, 4, 1),
                "reviewed_at": datetime(2026, 4, 2),
                "detail": "활동",
                "description": "설명",
                "student_name": "홍길동",
            }
        ]
        resp = self.client.post(
            "/api/ai/admin/audit-anomalies",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        issue_types = [a["issueType"] for a in data["anomalies"]]
        self.assertIn("INACTIVE_ITEM_SCORED", issue_types)

    def test_summary_report_with_common_areas_no_grade(self):
        """certification_areas의 grade가 NULL(공통 영역)인 경우 전 학년(1, 2, 3학년)에 모두 적용"""
        self.mock_db.custom_criteria_rows = [
            {"area_id": 91, "grade": None, "area_name": "직업기초능력", "area_max_score": 20.0, "item_id": 1, "item_name": "공통프로젝트", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
            {"area_id": 92, "grade": None, "area_name": "전문기술역량", "area_max_score": 20.0, "item_id": 2, "item_name": "공통자격증", "item_max_score": 20.0, "scoring_type": 1, "is_active": 1},
        ]
        self.mock_db.custom_students_rows = [
            {"student_id": 201, "name": "1학년생", "email": "s1@bssm.hs.kr", "grade": 1, "class_no": 1, "number": 1},
            {"student_id": 202, "name": "2학년생", "email": "s2@bssm.hs.kr", "grade": 2, "class_no": 1, "number": 1},
        ]
        self.mock_db.custom_submissions_rows = []
        self.mock_db.custom_merits_rows = []

        resp = self.client.post(
            "/api/ai/admin/summary-report",
            headers={"Authorization": f"Bearer {self.admin_token}"},
            json={"year": 2026},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        grade_stats = {g["grade"]: g for g in data["gradeStats"]}
        self.assertIn(1, grade_stats)
        self.assertIn(2, grade_stats)
        self.assertEqual(grade_stats[1]["studentCount"], 1)
        self.assertEqual(grade_stats[2]["studentCount"], 1)


if __name__ == "__main__":
    unittest.main()
