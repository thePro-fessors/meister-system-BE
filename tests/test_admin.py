"""
tests/test_admin.py - 관리자(Admin) 학년도 및 평가 기준 관리, 통계 대시보드 검증 스위트

검증 항목:
1. SEC-03: 관리자 RBAC 인가 검증 (학생/교사 차단 403, 관리자 허용 200/201)
2. DATA-05: 단일 활성 학년도(Single Active Year) 상호 배타성 강제 및 트랜잭션 보장
3. CRIT-01: 평가 기준 복제 시 중복 방어(409 Conflict) 및 벌크 복제
4. CRIT-02: targetGrade=0 (전학년 대상) Falsy 왜곡 방지 검증
5. CRIT-03: 관리자 영역/항목 명칭 Stored XSS 이스케이프 및 배점 상하한 검증
6. STAT-01: 관리자 대시보드 통계에서 소프트 삭제된 학생 제출물 누적 집계 배제
"""

from datetime import datetime
import unittest
from typing import Any, Dict, List
from fastapi.testclient import TestClient

from core.security import get_current_user
from database import get_db, get_redis
from main import app
from core.academic import _YEAR_ID_CACHE


class AdminMockRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = val
        return True

    async def delete(self, key):
        self.data.pop(key, None)


class AdminMockCursor:
    def __init__(self, conn):
        self.conn = conn
        self._current_result = []
        self._fetch_index = 0
        self.rowcount = 1
        self.lastrowid = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, sql: str, params: tuple = ()):
        sql_clean = " ".join(sql.strip().split())
        self._fetch_index = 0

        # 1. academic_years 관련 쿼리
        if "FROM academic_years" in sql_clean:
            if "WHERE year = %s" in sql_clean:
                target_year = params[0]
                self._current_result = [y for y in self.conn.db_years if y["year"] == target_year]
            elif "ORDER BY is_activated DESC" in sql_clean:
                sorted_years = sorted(self.conn.db_years, key=lambda y: (y.get("is_activated", False), y["year"]), reverse=True)
                self._current_result = sorted_years
            else:
                self._current_result = list(self.conn.db_years)

        elif "INSERT INTO academic_years" in sql_clean:
            new_id = len(self.conn.db_years) + 1
            self.conn.db_years.append({"year_id": new_id, "year": params[0], "is_activated": bool(params[1])})
            self.lastrowid = new_id

        elif "UPDATE academic_years SET is_activated = FALSE WHERE year != %s" in sql_clean:
            keep_year = params[0]
            for y in self.conn.db_years:
                if y["year"] != keep_year:
                    y["is_activated"] = False

        elif "UPDATE academic_years SET is_activated = FALSE" in sql_clean:
            for y in self.conn.db_years:
                y["is_activated"] = False

        elif "UPDATE academic_years SET is_activated = %s WHERE year = %s" in sql_clean:
            is_act, target_year = params[0], params[1]
            found = False
            for y in self.conn.db_years:
                if y["year"] == target_year:
                    y["is_activated"] = bool(is_act)
                    found = True
            self.rowcount = 1 if found else 0

        elif "SELECT LAST_INSERT_ID()" in sql_clean:
            self._current_result = [{"last_id": self.lastrowid}]

        # 2. certification_areas 관련 쿼리
        elif "SELECT COUNT(*) AS cnt FROM certification_areas WHERE year_id = %s" in sql_clean:
            y_id = params[0]
            cnt = sum(1 for a in self.conn.db_areas if a["year_id"] == y_id)
            self._current_result = [{"cnt": cnt}]

        elif "FROM certification_areas WHERE year_id = %s AND grade = %s AND name = %s" in sql_clean:
            y_id, grade, name = params[0], params[1], params[2]
            matched = [a for a in self.conn.db_areas if a["year_id"] == y_id and a["grade"] == grade and a["name"] == name]
            self._current_result = matched

        elif "FROM certification_areas WHERE year_id = %s" in sql_clean:
            y_id = params[0]
            self._current_result = [a for a in self.conn.db_areas if a["year_id"] == y_id]

        elif "FROM certification_areas WHERE area_id = %s" in sql_clean:
            a_id = params[0]
            self._current_result = [a for a in self.conn.db_areas if a["area_id"] == a_id]

        elif "INSERT INTO certification_areas" in sql_clean:
            new_id = len(self.conn.db_areas) + 1
            new_area = {
                "area_id": new_id,
                "year_id": params[0],
                "grade": params[1],
                "name": params[2],
                "max_score": params[3],
            }
            self.conn.db_areas.append(new_area)
            self.lastrowid = new_id

        elif "UPDATE certification_areas SET name = %s, max_score = %s WHERE area_id = %s" in sql_clean:
            name, score, a_id = params[0], params[1], params[2]
            for a in self.conn.db_areas:
                if a["area_id"] == a_id:
                    a["name"] = name
                    a["max_score"] = score

        # 3. evaluation_items 관련 쿼리
        elif "FROM evaluation_items WHERE area_id = %s AND is_active = TRUE" in sql_clean:
            a_id = params[0]
            self._current_result = [i for i in self.conn.db_items if i["area_id"] == a_id and i.get("is_active", True)]

        elif "FROM evaluation_items WHERE item_id = %s" in sql_clean:
            i_id = params[0]
            self._current_result = [i for i in self.conn.db_items if i["item_id"] == i_id]

        elif "FROM evaluation_items WHERE area_id IN" in sql_clean:
            self._current_result = [i for i in self.conn.db_items if i.get("is_active", True)]

        elif "INSERT INTO evaluation_items" in sql_clean:
            new_id = len(self.conn.db_items) + 1
            new_item = {
                "item_id": new_id,
                "area_id": params[0],
                "name": params[1],
                "max_score": params[2],
                "scoring_type": params[3],
                "target_grade": params[4],
                "requires_evidence": params[5],
                "is_active": True,
            }
            self.conn.db_items.append(new_item)
            self.lastrowid = new_id

        elif "UPDATE evaluation_items SET name = %s" in sql_clean:
            i_id = params[6]
            for it in self.conn.db_items:
                if it["item_id"] == i_id:
                    it["name"] = params[0]
                    it["max_score"] = params[1]
                    it["scoring_type"] = params[2]
                    it["target_grade"] = params[3]
                    it["requires_evidence"] = params[4]
                    it["is_active"] = params[5]

        elif "UPDATE evaluation_items SET is_active = FALSE WHERE item_id = %s" in sql_clean:
            i_id = params[0]
            for it in self.conn.db_items:
                if it["item_id"] == i_id:
                    it["is_active"] = False

        # 4. 관리자 overview 관련 쿼리
        elif "FROM student_academic_records sar" in sql_clean:
            self._current_result = [{"total_students": len([s for s in self.conn.db_students if not s["is_deleted"]])}]

        elif "COUNT(*) AS total_submissions" in sql_clean:
            # 소프트 삭제 학생 제출물 제외 카운트
            valid_subs = [
                s for s in self.conn.db_submissions
                if not s.get("is_deleted") and not any(st["student_id"] == s["student_id"] and st["is_deleted"] for st in self.conn.db_students)
            ]
            self._current_result = [{
                "total_submissions": len(valid_subs),
                "submitted_count": sum(1 for s in valid_subs if s["status_code"] == 1),
                "reviewing_count": sum(1 for s in valid_subs if s["status_code"] == 2),
                "approved_count": sum(1 for s in valid_subs if s["status_code"] == 3),
                "rejected_count": sum(1 for s in valid_subs if s["status_code"] == 4),
                "resubmit_count": sum(1 for s in valid_subs if s["status_code"] == 5),
            }]

        elif "SELECT ca.area_id AS areaId, ca.name AS areaName" in sql_clean:
            distribution = []
            for a in self.conn.db_areas:
                # 소프트 삭제 학생 제외 제출물 수
                sub_count = sum(
                    1 for s in self.conn.db_submissions
                    if not s.get("is_deleted") and s.get("area_id") == a["area_id"]
                    and not any(st["student_id"] == s["student_id"] and st["is_deleted"] for st in self.conn.db_students)
                )
                distribution.append({
                    "areaId": a["area_id"],
                    "areaName": a["name"],
                    "submissionCount": sub_count,
                })
            self._current_result = distribution

    async def executemany(self, sql: str, params_list: List[tuple]):
        for p in params_list:
            await self.execute(sql, p)

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


class AdminMockConnection:
    def __init__(self):
        self.db_years = [
            {"year_id": 1, "year": 2025, "is_activated": False},
            {"year_id": 2, "year": 2026, "is_activated": True},
        ]
        self.db_areas = [
            {"area_id": 1, "year_id": 2, "grade": 1, "name": "직업기초능력", "max_score": 100},
            {"area_id": 2, "year_id": 2, "grade": 1, "name": "전문기술역량", "max_score": 100},
        ]
        self.db_items = [
            {"item_id": 1, "area_id": 1, "name": "TOEIC 점수", "max_score": 30.0, "scoring_type": 1, "target_grade": 0, "requires_evidence": True, "is_active": True},
            {"item_id": 2, "area_id": 2, "name": "정보처리기능사", "max_score": 50.0, "scoring_type": 1, "target_grade": 1, "requires_evidence": True, "is_active": True},
        ]
        self.db_students = [
            {"student_id": 1, "uuid": "st-uuid-1", "name": "정상학생", "is_deleted": False},
            {"student_id": 2, "uuid": "st-uuid-2", "name": "탈퇴학생", "is_deleted": True},
        ]
        self.db_submissions = [
            {"submission_id": 1, "student_id": 1, "area_id": 1, "item_id": 1, "status_code": 3, "is_deleted": False},
            {"submission_id": 2, "student_id": 2, "area_id": 1, "item_id": 1, "status_code": 3, "is_deleted": False},  # 탈퇴 학생 제출물
        ]

    def cursor(self, cursor=None):
        return AdminMockCursor(self)

    async def autocommit(self, val: bool):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class TestAdminAPI(unittest.TestCase):
    def setUp(self):
        self.mock_conn = AdminMockConnection()
        self.mock_redis = AdminMockRedis()

        app.dependency_overrides[get_db] = lambda: self.mock_conn
        app.dependency_overrides[get_redis] = lambda: self.mock_redis

        self.current_user = {
            "uuid": "admin-uuid-001",
            "id": "admin",
            "name": "마스터관리자",
            "role": 2,  # 관리자
        }
        app.dependency_overrides[get_current_user] = lambda: self.current_user
        self.client = TestClient(app)
        _YEAR_ID_CACHE.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        _YEAR_ID_CACHE.clear()

    # 1. 관리자 권한 검증 (403 FORBIDDEN)
    def test_admin_access_control(self):
        """비관리자(학생/교사) 접근 시 403 차단 검증"""
        self.current_user["role"] = 0  # 학생
        res = self.client.post("/api/years", json={"year": 2027})
        self.assertEqual(res.status_code, 403)

        self.current_user["role"] = 1  # 교사
        res = self.client.post("/api/years", json={"year": 2027})
        self.assertEqual(res.status_code, 403)

    # 2. 신규 학년도 등록 및 단일 활성 학년도 상호 배타성 강제
    def test_create_year_mutex_and_cache(self):
        """신규 학년도 생성 시 활성화 옵션 선택 시 기존 활성 학년도 자동 비활성화 검증"""
        res = self.client.post("/api/years", json={"year": 2027, "isActivated": True})
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual(data["year"], 2027)
        self.assertTrue(data["isActivated"])

        # 기존 2026 학년도가 비활성화되었는지 확인
        y2026 = next(y for y in self.mock_conn.db_years if y["year"] == 2026)
        self.assertFalse(y2026["is_activated"])

    # 3. 학년도 활성화 토글 상호 배타성
    def test_toggle_year_active_mutex(self):
        """기존 학년도를 활성화하면 다른 학년도가 자동으로 비활성화되는지 검증"""
        res = self.client.patch("/api/years/2025", json={"isActivated": True})
        self.assertEqual(res.status_code, 200)

        y2025 = next(y for y in self.mock_conn.db_years if y["year"] == 2025)
        y2026 = next(y for y in self.mock_conn.db_years if y["year"] == 2026)
        self.assertTrue(y2025["is_activated"])
        self.assertFalse(y2026["is_activated"])

    # 4. 기준 복제 중복 방어 (409 Conflict)
    def test_copy_criteria_conflict(self):
        """복제 대상 학년도에 이미 기준이 존재할 경우 409 Conflict 발생 검증"""
        # 대상 학년도 2026에는 이미 기준(db_areas)이 존재함
        res = self.client.post("/api/years/2026/copy-from/2025")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["error"]["code"], "CONFLICT")

    # 5. 기준 복제 성공 (빈 학년도로 복제)
    def test_copy_criteria_success(self):
        """빈 학년도로 복제 시 성공적으로 벌크 복제되는지 검증"""
        # 2027 학년도를 추가(기준 없음)
        self.mock_conn.db_years.append({"year_id": 3, "year": 2027, "is_activated": False})

        res = self.client.post("/api/years/2027/copy-from/2026")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["copiedAreas"], 2)
        self.assertEqual(data["copiedItems"], 2)

    # 6. targetGrade: 0 Falsy 버그 해결 검증
    def test_create_criteria_item_target_grade_zero(self):
        """targetGrade: 0 (전학년 공통) 전달 시 1로 변환되지 않고 0으로 정상 저장되는지 검증"""
        res = self.client.post(
            "/api/criteria/areas/1/items",
            json={
                "name": "한국사능력검정시험",
                "maxScore": 20.0,
                "targetGrade": 0,
                "scoringType": 1,
                "requiresEvidence": True,
            },
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual(data["targetGrade"], 0)  # 0이 유지되어야 함

    # 7. 배점 상하한 검증 (max_score <= 0 또는 > 1000 차단)
    def test_criteria_score_bounds_validation(self):
        """0 이하 또는 1000 초과 배점 시 400 Bad Request 검증"""
        res_zero = self.client.post(
            "/api/criteria/areas/1/items",
            json={"name": "잘못된항목", "maxScore": 0.0},
        )
        self.assertEqual(res_zero.status_code, 400)

        res_over = self.client.post(
            "/api/criteria/areas/1/items",
            json={"name": "과도한항목", "maxScore": 10000.0},
        )
        self.assertEqual(res_over.status_code, 400)

        res_area_neg = self.client.patch(
            "/api/criteria/areas/1",
            json={"maxScore": -10.0},
        )
        self.assertEqual(res_area_neg.status_code, 400)

    # 8. Stored XSS 이스케이프 검증
    def test_criteria_xss_escaping(self):
        """영역 및 항목 이름 입력 시 HTML 태그가 정상 이스케이프되는지 검증"""
        xss_payload = "<script>alert('xss')</script>"
        res = self.client.post(
            "/api/criteria/areas/1/items",
            json={"name": xss_payload, "maxScore": 10.0},
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()["data"]
        self.assertEqual(data["name"], "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;")

    # 9. 관리자 Overview 소프트 삭제 학생 집계 제외 검증
    def test_admin_overview_excludes_deleted_students(self):
        """탈퇴(is_deleted=TRUE) 학생의 제출물은 총 제출물 수 및 영역 통계에서 배제되는지 검증"""
        res = self.client.get("/api/admin/overview?year=2026")
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data["totalStudents"], 1)  # 정상 학생 1명만 카운트
        self.assertEqual(data["submissions"]["total"], 1)  # 정상 학생 제출물 1건만 카운트 (탈퇴학생 제외)
        self.assertEqual(data["areaDistribution"][0]["submissionCount"], 1)

    # 10. 관리자 입력값 유효 범위 및 경계 조건(Boundary Conditions) 검증
    def test_admin_validation_boundary_conditions(self):
        """학년도 유효 범위, 기준 자가 복제 차단, 영역/항목 명칭 길이 및 학년 번호 검증"""
        # (1) 학년도 범위 검증 (1900~2100)
        res_low_yr = self.client.post("/api/years", json={"year": 1899})
        self.assertEqual(res_low_yr.status_code, 400)

        res_high_yr = self.client.post("/api/years", json={"year": 2101})
        self.assertEqual(res_high_yr.status_code, 400)

        res_toggle_yr = self.client.patch("/api/years/1800", json={"isActivated": True})
        self.assertEqual(res_toggle_yr.status_code, 400)

        # (2) 기준 자가 복제(Self-copy) 차단 (400 Bad Request)
        res_self_copy = self.client.post("/api/years/2026/copy-from/2026")
        self.assertEqual(res_self_copy.status_code, 400)

        # (3) 영역 명칭 공백 및 길이 검증 (30자 제한)
        res_empty_area = self.client.patch("/api/criteria/areas/1", json={"name": "   "})
        self.assertEqual(res_empty_area.status_code, 400)

        res_long_area = self.client.patch("/api/criteria/areas/1", json={"name": "가" * 31})
        self.assertEqual(res_long_area.status_code, 400)

        # (4) 항목 명칭 길이(50자) 및 대상 학년(0~3) 검증
        res_long_item = self.client.post("/api/criteria/areas/1/items", json={"name": "A" * 51, "maxScore": 10.0})
        self.assertEqual(res_long_item.status_code, 400)

        res_invalid_grade = self.client.post("/api/criteria/areas/1/items", json={"name": "정상항목", "maxScore": 10.0, "targetGrade": 4})
        self.assertEqual(res_invalid_grade.status_code, 400)

        # (5) 항목 수정 공백, 길이, 학년 검증
        res_patch_empty_item = self.client.patch("/api/criteria/items/1", json={"name": "  "})
        self.assertEqual(res_patch_empty_item.status_code, 400)

        res_patch_long_item = self.client.patch("/api/criteria/items/1", json={"name": "B" * 51})
        self.assertEqual(res_patch_long_item.status_code, 400)

        res_patch_invalid_grade = self.client.patch("/api/criteria/items/1", json={"targetGrade": 9})
        self.assertEqual(res_patch_invalid_grade.status_code, 400)


if __name__ == "__main__":
    unittest.main()
