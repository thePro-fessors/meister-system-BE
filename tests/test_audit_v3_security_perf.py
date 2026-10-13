"""
tests/test_audit_v3_security_perf.py - 3차 전수 보안/성능 감사 신규 보완 검증 테스트

검증 항목:
1. NEIS(core/neis.py):
   - NEIS_API_KEY 환경변수 기본 연동
   - 비정상 학년도(2000 미만 / 2100 초과) 유효성 가드
   - 인메모리 캐시 MAX_NEIS_CACHE_ENTRIES 상한 및 LRU 메모리 보호
2. AI Multi-Provider(routers/ai.py & core/ai_provider.py):
   - OpenAI/Claude/Compatible 공급자 설정 시 LLM 호출 게이트 통과 검증
   - get_teacher_ai_config 및 get_admin_ai_config 공급자별 모델/키 분기 검증
3. 기준 복제(routers/admin.py copy_year_criteria):
   - 2순위 평가 담당 교과(subject) 보존 복제
   - 세부 점수 기준표(scoring_rules) 보존 복제
"""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.neis import (
    MAX_NEIS_CACHE_ENTRIES,
    _NEIS_CLASS_CACHE,
    get_classes_for_year,
    get_school_info,
    validate_class_against_neis,
)
from routers.ai import get_admin_ai_config, get_teacher_ai_config
from core.ai_provider import resolve_ai_config


class TestNEISSecurityAndCache(unittest.IsolatedAsyncioTestCase):
    """NEIS 모듈 보안 및 캐시 상한 검증"""

    def setUp(self):
        _NEIS_CLASS_CACHE.clear()

    def tearDown(self):
        _NEIS_CLASS_CACHE.clear()

    @patch("core.neis.httpx.AsyncClient")
    async def test_get_school_info_uses_env_api_key(self, mock_client_cls):
        """api_key 미전달 시 NEIS_API_KEY 환경변수 자동 적용 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "schoolInfo": [{}, {"row": [{"SCHUL_NM": "부산소프트웨어마이스터고등학교"}]}]
        }
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        with patch.dict(os.environ, {"NEIS_API_KEY": "test-env-key-1234"}, clear=True):
            res = await get_school_info()
            self.assertIsNotNone(res)
            self.assertEqual(res["SCHUL_NM"], "부산소프트웨어마이스터고등학교")
            # 요청 파라미터에 KEY가 주입되었는지 확인
            call_kwargs = mock_client.get.call_args[1]
            self.assertEqual(call_kwargs["params"]["KEY"], "test-env-key-1234")

    async def test_get_classes_for_year_invalid_range(self):
        """비정상 학년도 요청 시 네트워크 호출 없이 즉각 거부 검증"""
        res_low = await get_classes_for_year(1999)
        self.assertEqual(res_low, [])

        res_high = await get_classes_for_year(2101)
        self.assertEqual(res_high, [])

    async def test_validate_class_against_neis_invalid_year(self):
        """validate_class_against_neis 비정상 학년도 가드 검증"""
        ok, err = await validate_class_against_neis(1800, 1, 1)
        self.assertFalse(ok)
        self.assertIn("유효하지 않은", err)

    @patch("core.neis.httpx.AsyncClient")
    async def test_class_cache_max_entries_bounded(self, mock_client_cls):
        """MAX_NEIS_CACHE_ENTRIES를 초과할 경우 가장 오래된 엔트리 축출(LRU) 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "classInfo": [{}, {"row": [{"GRADE": "1", "CLASS_NM": "1"}]}]
        }
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        # 25개 학년도 연속 조회
        for y in range(2020, 2020 + MAX_NEIS_CACHE_ENTRIES + 5):
            await get_classes_for_year(y)

        # 캐시 크기가 MAX_NEIS_CACHE_ENTRIES 이하로 제한되는지 검증
        self.assertLessEqual(len(_NEIS_CLASS_CACHE), MAX_NEIS_CACHE_ENTRIES)
        # 최초 삽입된 2020년도는 축출되어 존재하지 않아야 함
        self.assertNotIn(2020, _NEIS_CLASS_CACHE)


class TestAIMultiProviderGate(unittest.TestCase):
    """routers/ai.py의 Multi-Provider 게이트 정상 동작 검증"""

    def test_teacher_ai_config_openai_provider(self):
        """TEACHER_AI_PROVIDER=openai 및 OPENAI_API_KEY 설정 시 게이트 통과 검증"""
        env = {
            "TEACHER_AI_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-proj-test1234",
            "OPENAI_MODEL": "gpt-4o-mini",
        }
        with patch.dict(os.environ, env, clear=True):
            eff_key, model = get_teacher_ai_config()
            self.assertTrue(bool(eff_key))
            self.assertEqual(model, "gpt-4o-mini")

    def test_admin_ai_config_claude_provider(self):
        """ADMIN_AI_PROVIDER=claude 및 ANTHROPIC_API_KEY 설정 시 게이트 통과 검증"""
        env = {
            "ADMIN_AI_PROVIDER": "claude",
            "ANTHROPIC_API_KEY": "sk-ant-test1234",
            "CLAUDE_MODEL": "claude-3-5-sonnet-20241022",
        }
        with patch.dict(os.environ, env, clear=True):
            eff_key, model = get_admin_ai_config()
            self.assertTrue(bool(eff_key))
            self.assertEqual(model, "claude-3-5-sonnet-20241022")

    def test_admin_ai_config_compatible_custom_endpoint(self):
        """AI_BASE_URL 지정 시 API 키 없이도 로컬 LLM 호출 게이트 통과 검증"""
        env = {
            "AI_BASE_URL": "http://127.0.0.1:8000/v1",
            "AI_PROVIDER": "openai_compatible",
        }
        with patch.dict(os.environ, env, clear=True):
            eff_key, model = get_admin_ai_config()
            self.assertTrue(bool(eff_key))


class MockCopyCursor:
    def __init__(self, data_store, simulate_zero_lastrowid=False):
        self.ds = data_store
        self.simulate_zero_lastrowid = simulate_zero_lastrowid
        self.lastrowid = 1
        self._res = []
        self._idx = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql, params=()):
        sql_clean = " ".join(sql.strip().split())
        self._idx = 0

        if "SELECT year_id FROM academic_years WHERE year = %s" in sql_clean:
            yr = params[0]
            if yr in self.ds["years"]:
                self._res = [{"year_id": self.ds["years"][yr]}]
            else:
                self._res = []
        elif "SELECT COUNT(*) AS cnt FROM certification_areas WHERE year_id = %s" in sql_clean:
            cnt = sum(1 for a in self.ds["areas"] if a["year_id"] == params[0])
            self._res = [{"cnt": cnt}]
        elif "SELECT area_id, grade, name, max_score FROM certification_areas WHERE year_id = %s" in sql_clean:
            self._res = [a for a in self.ds["areas"] if a["year_id"] == params[0]]
        elif "INSERT INTO certification_areas" in sql_clean:
            new_aid = len(self.ds["areas"]) + 100
            self.ds["areas"].append({
                "area_id": new_aid,
                "year_id": params[0],
                "grade": params[1],
                "name": params[2],
                "max_score": params[3],
            })
            self.lastrowid = 0 if self.simulate_zero_lastrowid else new_aid
            self._last_aid = new_aid
        elif "SELECT item_id, name, subject, department, max_score, scoring_type, target_grade, requires_evidence FROM evaluation_items" in sql_clean:
            self._res = [it for it in self.ds["items"] if it["area_id"] == params[0]]
        elif "INSERT INTO evaluation_items" in sql_clean:
            new_iid = len(self.ds["items"]) + 200
            self.ds["items"].append({
                "item_id": new_iid,
                "area_id": params[0],
                "name": params[1],
                "department": params[2],
                "max_score": params[3],
                "scoring_type": params[4],
                "target_grade": params[5],
                "requires_evidence": params[6],
                "subject": params[7],
            })
            self.lastrowid = 0 if self.simulate_zero_lastrowid else new_iid
            self._last_iid = new_iid
        elif "SELECT condition_value, score FROM scoring_rules" in sql_clean:
            self._res = [r for r in self.ds["rules"] if r["item_id"] == params[0]]
        elif "INSERT INTO scoring_rules" in sql_clean:
            self.ds["rules"].append({
                "item_id": params[0],
                "condition_value": params[1],
                "score": params[2],
            })
        elif "SELECT eit.teacher_id FROM evaluation_item_teachers" in sql_clean:
            self._res = [t for t in self.ds["teachers"] if t["item_id"] == params[0]]
        elif "INSERT INTO evaluation_item_teachers" in sql_clean:
            self.ds["teachers"].append({
                "item_id": params[0],
                "teacher_id": params[1],
            })
        elif "SELECT LAST_INSERT_ID()" in sql_clean:
            val = getattr(self, "_last_iid", getattr(self, "_last_aid", 1))
            self._res = [{"last_id": val}]
        elif "SELECT area_id FROM certification_areas WHERE year_id = %s" in sql_clean:
            matched = [a for a in self.ds["areas"] if a["year_id"] == params[0] and a["grade"] == params[1] and a["name"] == params[2]]
            self._res = matched[:1]
        elif "SELECT item_id FROM evaluation_items WHERE area_id = %s" in sql_clean:
            matched = [it for it in self.ds["items"] if it["area_id"] == params[0] and it["name"] == params[1]]
            self._res = matched[:1]
        else:
            self._res = []

    async def executemany(self, sql, params_list):
        for p in params_list:
            await self.execute(sql, p)

    async def fetchone(self):
        if self._idx < len(self._res):
            row = self._res[self._idx]
            self._idx += 1
            return row
        return None

    async def fetchall(self):
        rows = self._res[self._idx:]
        self._idx = len(self._res)
        return rows


class MockCopyConnection:
    def __init__(self, data_store, simulate_zero_lastrowid=False):
        self.ds = data_store
        self.simulate_zero_lastrowid = simulate_zero_lastrowid

    def cursor(self, cursor=None):
        return MockCopyCursor(self.ds, self.simulate_zero_lastrowid)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def autocommit(self, val):
        pass


class TestCriteriaCopyPreservation(unittest.IsolatedAsyncioTestCase):
    """routers/admin.py copy_year_criteria 정합성 보존 단위 테스트"""

    def setUp(self):
        from routers.admin import copy_year_criteria
        self.copy_func = copy_year_criteria
        self.admin_user = {"role": "admin", "uuid": "admin-uuid-001"}

    async def test_copy_same_year_rejected(self):
        """동일 학년도 간 복제 요청 시 400 반환 검증"""
        conn = MockCopyConnection({})
        res = await self.copy_func(target_year=2026, source_year=2026, conn=conn, current_user=self.admin_user)
        self.assertEqual(res.status_code, 400)

    async def test_copy_nonexistent_year_rejected(self):
        """존재하지 않는 학년도 요청 시 404 반환 검증"""
        ds = {"years": {2025: 1}}  # 2026 없음
        conn = MockCopyConnection(ds)
        res = await self.copy_func(target_year=2026, source_year=2025, conn=conn, current_user=self.admin_user)
        self.assertEqual(res.status_code, 404)

    async def test_copy_target_already_has_criteria_conflict(self):
        """복제 대상 학년도에 이미 기준이 설정되어 있을 시 409 반환 검증"""
        ds = {
            "years": {2025: 1, 2026: 2},
            "areas": [{"area_id": 99, "year_id": 2, "grade": 1, "name": "기존영역", "max_score": 100}],
            "items": [],
            "rules": [],
            "teachers": [],
        }
        conn = MockCopyConnection(ds)
        res = await self.copy_func(target_year=2026, source_year=2025, conn=conn, current_user=self.admin_user)
        self.assertEqual(res.status_code, 409)

    async def test_copy_preserves_subject_scoring_rules_and_teachers(self):
        """평가 기준 복제 시 subject, department, scoring_rules, evaluation_item_teachers 전수 보존 복제 검증"""
        ds = {
            "years": {2025: 10, 2026: 20},
            "areas": [
                {"area_id": 1, "year_id": 10, "grade": 1, "name": "전문기술역량", "max_score": 100.0}
            ],
            "items": [
                {
                    "item_id": 11,
                    "area_id": 1,
                    "name": "정보처리기능사 취득",
                    "subject": "소프트웨어공학",
                    "department": "마이스터기획부",
                    "max_score": 50.0,
                    "scoring_type": 1,
                    "target_grade": 1,
                    "requires_evidence": True,
                    "is_active": True,
                }
            ],
            "rules": [
                {"item_id": 11, "condition_value": "합격", "score": 50.0}
            ],
            "teachers": [
                {"item_id": 11, "teacher_id": 7}
            ],
        }
        conn = MockCopyConnection(ds)
        res = await self.copy_func(target_year=2026, source_year=2025, conn=conn, current_user=self.admin_user)

        self.assertEqual(res.get("status_code"), 200)
        self.assertTrue(res.get("success"))
        self.assertEqual(res["data"]["copiedAreas"], 1)
        self.assertEqual(res["data"]["copiedItems"], 1)

        # 복제된 항목 검증
        copied_items = [it for it in ds["items"] if it["item_id"] != 11]
        self.assertEqual(len(copied_items), 1)
        c_item = copied_items[0]
        self.assertEqual(c_item["name"], "정보처리기능사 취득")
        self.assertEqual(c_item["subject"], "소프트웨어공학")
        self.assertEqual(c_item["department"], "마이스터기획부")

        # 복제된 배점 조건표 검증
        copied_rules = [r for r in ds["rules"] if r["item_id"] == c_item["item_id"]]
        self.assertEqual(len(copied_rules), 1)
        self.assertEqual(copied_rules[0]["condition_value"], "합격")
        self.assertEqual(copied_rules[0]["score"], 50.0)

        # 복제된 전담 교사 검증
        copied_teachers = [t for t in ds["teachers"] if t["item_id"] == c_item["item_id"]]
        self.assertEqual(len(copied_teachers), 1)
        self.assertEqual(copied_teachers[0]["teacher_id"], 7)

    async def test_copy_null_lastrowid_fallback_safe(self):
        """lastrowid가 0이 반환되는 환경에서도 LAST_INSERT_ID 안전 폴백 및 정상 완료 검증"""
        ds = {
            "years": {2025: 10, 2026: 20},
            "areas": [
                {"area_id": 1, "year_id": 10, "grade": 1, "name": "직업기초능력", "max_score": 100.0}
            ],
            "items": [
                {
                    "item_id": 12,
                    "area_id": 1,
                    "name": "토익스피킹",
                    "subject": "실무영어",
                    "department": "외국어교육부",
                    "max_score": 30.0,
                    "scoring_type": 1,
                    "target_grade": 0,
                    "requires_evidence": True,
                    "is_active": True,
                }
            ],
            "rules": [],
            "teachers": [],
        }
        conn = MockCopyConnection(ds, simulate_zero_lastrowid=True)
        res = await self.copy_func(target_year=2026, source_year=2025, conn=conn, current_user=self.admin_user)
        self.assertEqual(res.get("status_code"), 200)
        self.assertTrue(res.get("success"))


if __name__ == "__main__":
    unittest.main()
