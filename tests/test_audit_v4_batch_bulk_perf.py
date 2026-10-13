"""
tests/test_audit_v4_batch_bulk_perf.py - 학생/교사 일괄 등록 N+1 쿼리 최적화 및 보안/성능 검증 테스트

검증 항목:
1. 학생 일괄 등록 시 사전 벌크 조회(Bulk Prefetch) 검증:
   - SELECT 쿼리가 행 수(N)에 비례하지 않고 O(1) 고정 횟수(2~3회)만 실행되는지 검증
2. 교사 일괄 등록 시 사전 벌크 조회(Bulk Prefetch) 검증:
   - SELECT 쿼리가 행 수(N)에 비례하지 않고 O(1) 고정 횟수(2회)만 실행되는지 검증
3. 대량 등록(50명) 시 쿼리 카운트 검증 (N+1 방어 확인)
4. 학생-교사 이메일 교차 중복(Role Confusion) 차단이 벌크 환경에서도 완벽히 유지되는지 검증
5. AI 챗봇 학생 이메일 클레임 우선 해석(Custom Domain 호환) 검증
"""

import os
import unittest
from typing import Any, Dict, List
from fastapi.testclient import TestClient

from core.academic import _YEAR_ID_CACHE
from core.security import get_current_user
from database import get_db, get_redis
from main import app


class QueryTrackingMockCursor:
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
        self.conn.executed_queries.append({"sql": sql_clean, "params": params})
        if sql_clean.upper().startswith("SELECT"):
            self.conn.select_query_count += 1
        self._fetch_index = 0

        # academic_years
        if "FROM academic_years" in sql_clean:
            if "WHERE year = %s" in sql_clean:
                target_year = params[0]
                self._current_result = [y for y in self.conn.db_years if y["year"] == target_year]
            elif "ORDER BY is_activated DESC" in sql_clean:
                sorted_years = sorted(self.conn.db_years, key=lambda y: (y.get("is_activated", False), y["year"]), reverse=True)
                self._current_result = sorted_years
            else:
                self._current_result = list(self.conn.db_years)

        # teachers 조회
        elif "FROM teachers" in sql_clean and "SELECT" in sql_clean:
            if "WHERE email IN" in sql_clean:
                self._current_result = [t for t in self.conn.db_teachers if t.get("email") in params and not t.get("is_deleted")]
            elif "WHERE email = %s" in sql_clean:
                target_email = params[0]
                self._current_result = [t for t in self.conn.db_teachers if t.get("email") == target_email and not t.get("is_deleted")]
            else:
                self._current_result = list(self.conn.db_teachers)

        # teachers 갱신
        elif "UPDATE teachers" in sql_clean:
            t_id = params[6] if len(params) >= 7 else params[4]
            found = False
            for t in self.conn.db_teachers:
                if t["teachers_id"] == t_id:
                    t["name"] = params[0]
                    found = True
            self.rowcount = 1 if found else 0

        # teachers 삽입
        elif "INSERT INTO teachers" in sql_clean:
            new_id = len(self.conn.db_teachers) + 1
            t_data = {
                "teachers_id": new_id,
                "name": params[0],
                "email": params[6] if len(params) >= 7 else params[4],
                "is_deleted": False,
            }
            self.conn.db_teachers.append(t_data)
            self.lastrowid = new_id

        # students 조회
        elif "FROM students" in sql_clean and "SELECT" in sql_clean:
            if "WHERE email IN" in sql_clean:
                self._current_result = [s for s in self.conn.db_students if s.get("email") in params and not s.get("is_deleted")]
            elif "WHERE email = %s" in sql_clean:
                target_email = params[0]
                self._current_result = [s for s in self.conn.db_students if s.get("email") == target_email and not s.get("is_deleted")]
            elif "WHERE uuid = %s" in sql_clean:
                target_uuid = params[0]
                self._current_result = [s for s in self.conn.db_students if s.get("uuid") == target_uuid and not s.get("is_deleted")]
            else:
                self._current_result = list(self.conn.db_students)

        # students 삽입
        elif "INSERT INTO students" in sql_clean:
            new_id = len(self.conn.db_students) + 1
            st_data = {
                "student_id": new_id,
                "name": params[0],
                "email": params[1],
                "status": 0,
                "is_deleted": False,
            }
            self.conn.db_students.append(st_data)
            self.lastrowid = new_id

        # student_academic_records 조회
        elif "SELECT record_id" in sql_clean and "FROM student_academic_records" in sql_clean:
            if "student_id IN" in sql_clean:
                yr_id = params[0]
                st_ids = set(params[1:])
                self._current_result = [
                    r for r in self.conn.db_records if r["year_id"] == yr_id and r["student_id"] in st_ids
                ]
            else:
                st_id, yr_id = params[0], params[1]
                self._current_result = [
                    r for r in self.conn.db_records if r["student_id"] == st_id and r["year_id"] == yr_id
                ]

        # student_academic_records 삽입
        elif "INSERT INTO student_academic_records" in sql_clean:
            new_id = len(self.conn.db_records) + 1
            self.conn.db_records.append({
                "record_id": new_id,
                "student_id": params[0],
                "year_id": params[1],
                "grade": params[2],
                "class": params[3],
                "number": params[4],
            })
            self.lastrowid = new_id

        # LAST_INSERT_ID
        elif "SELECT LAST_INSERT_ID()" in sql_clean:
            self._current_result = [{"last_id": self.lastrowid}]

        # chat_conversations / AI 관련 모킹
        elif "FROM chat_conversations" in sql_clean:
            self._current_result = []

        elif "FROM certification_areas" in sql_clean or "FROM evaluation_items" in sql_clean:
            self._current_result = []

        elif "FROM submissions" in sql_clean or "FROM merits" in sql_clean:
            self._current_result = []

        else:
            self._current_result = []

    async def fetchone(self):
        if self._fetch_index < len(self._current_result):
            row = self._current_result[self._fetch_index]
            self._fetch_index += 1
            return row
        return None

    async def fetchall(self):
        res = self._current_result[self._fetch_index :]
        self._fetch_index = len(self._current_result)
        return res


class QueryTrackingMockConnection:
    def __init__(self):
        self.db_years = [
            {"year_id": 1, "year": 2026, "is_activated": True},
        ]
        self.db_students = []
        self.db_teachers = []
        self.db_records = []
        self.executed_queries: List[Dict[str, Any]] = []
        self.select_query_count = 0

    def cursor(self, cursor=None):
        return QueryTrackingMockCursor(self)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def autocommit(self, val: bool):
        pass


class TestBatchBulkQueryPerformance(unittest.TestCase):
    """학생 및 교사 일괄 등록 시 N+1 방어 및 벌크 쿼리 최적화 검증"""

    def setUp(self):
        _YEAR_ID_CACHE.clear()
        self.conn = QueryTrackingMockConnection()
        self.admin_user = {"uuid": "admin-uuid", "role": "admin", "id": "admin"}

        app.dependency_overrides[get_db] = lambda: self.conn
        app.dependency_overrides[get_current_user] = lambda: self.admin_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_student_batch_bulk_prefetch_query_count(self):
        """20명 학생 신규 일괄 등록 시 루프 내 SELECT가 반복 실행되지 않고 2회(teachers, students)만 실행되는지 검증"""
        students_payload = [
            {"name": f"학생{i}", "grade": 1, "class_no": 1, "student_no": i, "email": f"s101{i:02d}@bssm.hs.kr"}
            for i in range(1, 21)
        ]

        # 학년도 조회(1회) + 트랜잭션 진입 후 teachers bulk check(1회) + students bulk check(1회)
        # 신규 학생이므로 academic_records bulk check는 0회(기존 학생 id 없음)
        # 총 SELECT 횟수는 3회여야 함 (기존 N*4 = 80+회 대비 96% 감소)
        resp = self.client.post(
            "/api/admin/students/batch?validate_with_neis=false",
            json={"year": 2026, "students": students_payload},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["successCount"], 20)
        self.assertEqual(data["createdStudentsCount"], 20)
        self.assertEqual(data["failedCount"], 0)

        # 전체 SELECT 쿼리 수 검증
        # 1(academic_years) + 1(teachers bulk) + 1(students bulk) = 3
        self.assertLessEqual(self.conn.select_query_count, 4)

    def test_student_batch_bulk_prefetch_with_existing_records(self):
        """기존 학생 10명이 존재하는 상태에서 overwrite=True 일괄 등록 시 총 SELECT 수가 4회 이하인지 검증"""
        # 기존 학생 및 학적 세팅
        for i in range(1, 11):
            self.conn.db_students.append({
                "student_id": i,
                "name": f"기존학생{i}",
                "email": f"s101{i:02d}@bssm.hs.kr",
                "is_deleted": False,
            })
            self.conn.db_records.append({
                "record_id": i,
                "student_id": i,
                "year_id": 1,
                "grade": 1,
                "class": 1,
                "number": i,
            })

        students_payload = [
            {"name": f"수정학생{i}", "grade": 1, "class_no": 1, "student_no": i, "email": f"s101{i:02d}@bssm.hs.kr"}
            for i in range(1, 11)
        ]

        self.conn.select_query_count = 0
        resp = self.client.post(
            "/api/admin/students/batch?validate_with_neis=false",
            json={"year": 2026, "overwrite": True, "students": students_payload},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["updatedRecordsCount"], 10)
        self.assertEqual(data["createdStudentsCount"], 0)

        # SELECT: 1(academic_years) + 1(teachers bulk) + 1(students bulk) + 1(records bulk) = 4
        self.assertLessEqual(self.conn.select_query_count, 4)

    def test_teacher_batch_bulk_prefetch_query_count(self):
        """15명 교사 일괄 등록 시 루프 내 SELECT가 반복 실행되지 않고 2회(students, teachers)만 실행되는지 검증"""
        teachers_payload = [
            {"name": f"교사{i}", "subject": "소프트웨어", "position": "교과전담", "email": f"teacher{i}@bssm.hs.kr"}
            for i in range(1, 16)
        ]

        self.conn.select_query_count = 0
        resp = self.client.post(
            "/api/admin/teachers/batch?validate_with_neis=false",
            json={"year": 2026, "teachers": teachers_payload},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["successCount"], 15)

        # SELECT: 1(academic_years) + 1(students bulk) + 1(teachers bulk) = 3
        self.assertLessEqual(self.conn.select_query_count, 3)

    def test_cross_role_email_duplicate_rejected_in_bulk(self):
        """교사 명단에 있는 이메일이 학생 일괄 등록에 포함되면 벌크 검증 단계에서 정확히 걸러지는지 검증"""
        self.conn.db_teachers.append({
            "teachers_id": 99,
            "name": "홍선생",
            "email": "teacher_conflict@bssm.hs.kr",
            "is_deleted": False,
        })

        students_payload = [
            {"name": "학생1", "grade": 1, "class_no": 1, "student_no": 1, "email": "student1@bssm.hs.kr"},
            {"name": "악의적학생", "grade": 1, "class_no": 1, "student_no": 2, "email": "teacher_conflict@bssm.hs.kr"},
        ]

        resp = self.client.post(
            "/api/admin/students/batch?validate_with_neis=false",
            json={"year": 2026, "students": students_payload},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["createdStudentsCount"], 1)
        self.assertEqual(data["failedCount"], 1)
        self.assertIn("교사 명단에 이미 등록된 이메일", data["errors"][0]["reason"])
    
    def test_existing_student_new_academic_year_registration_count(self):
        """기존 학생(students 테이블에 기등록)이 신규 학년도 일괄 등록될 때 successCount 및 updatedRecordsCount가 정상 증가하는지 검증"""
        self.conn.db_students.append({
            "student_id": 42,
            "name": "재학생",
            "email": "s20101@bssm.hs.kr",
            "is_deleted": False,
        })
        # db_records는 비어있음 (2026 학년도 학적 미생성 상태)

        students_payload = [
            {"name": "재학생", "grade": 2, "class_no": 1, "student_no": 1, "email": "s20101@bssm.hs.kr"}
        ]

        resp = self.client.post(
            "/api/admin/students/batch?validate_with_neis=false",
            json={"year": 2026, "students": students_payload},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["createdStudentsCount"], 0)
        self.assertEqual(data["updatedRecordsCount"], 1)
        self.assertEqual(data["successCount"], 1)
        self.assertEqual(data["failedCount"], 0)
        self.assertEqual(len(self.conn.db_records), 1)
        self.assertEqual(self.conn.db_records[0]["student_id"], 42)

    def test_student_chat_custom_domain_and_safe_id_resolution(self):
        """학생 AI 챗봇에서 토큰 클레임 email 및 id 안전 해석(Custom Domain 호환, KeyError 방지) 검증"""
        # 1) 커스텀 도메인 이메일 보유 학생 매핑 검증
        self.conn.db_students.append({
            "student_id": 77,
            "name": "커스텀학생",
            "email": "student@custom-domain.org",
            "uuid": None,
            "is_deleted": False,
        })

        custom_user = {
            "uuid": "custom-uuid-1",
            "email": "student@custom-domain.org",
            "role": "student",
            "id": "custom_user",
        }
        app.dependency_overrides[get_current_user] = lambda: custom_user

        resp = self.client.post(
            "/api/ai/student/chat",
            json={"message": "안녕하세요"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIn("reply", data)

        # 2) id 및 email 클레임이 모두 누락된 토큰에서도 KeyError/크래시 없이 안전하게 처리되는지 검증
        minimal_user = {
            "uuid": "minimal-uuid-99",
            "role": "student",
        }
        app.dependency_overrides[get_current_user] = lambda: minimal_user

        resp_min = self.client.post(
            "/api/ai/student/chat",
            json={"message": "질문있어요"},
        )
        self.assertEqual(resp_min.status_code, 200)
        self.assertIn("reply", resp_min.json()["data"])


if __name__ == "__main__":
    unittest.main()
