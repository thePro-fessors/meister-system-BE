"""
tests/test_evaluator_hierarchy.py - 평가자 지정 우선순위(1~3순위) 체계, N:M 매핑, 계층 상속 종합 단위/통합 테스트

검증 항목:
1. evaluate_evaluator_access 단위 검증:
   - 1순위: 전담 교사 N명 목록 지정 시 포함 교사만 승인, 비포함 교사(동일 과목/부서여도) 403 차단
   - 2순위: 1순위 미지정 시 교과 우선 판정 (동일 부서 소속이어도 타 교과 교사는 차단)
   - 3순위: 1, 2순위 미지정 시 부서 판정 (소속 부서 교사만 허용, 타 부서 차단)
   - 기본: 1~3순위 모두 미지정 시 일반 교과/비담임 교사 차단 (담임/학년부장 전용)
2. 증빙자료 승인 API (PATCH /api/submissions/{id}/approve) 통합 검증:
   - 1순위 전담 교사 A, B 승인 허용(200), 동일 교과/부서 비배정 교사 C 차단(403)
   - 2순위 교과(수학) 지정 시 수학 교사 허용(200), 동일 부서 국어 교사 차단(403)
   - 3순위 부서(학생생활지도부) 지정 시 부서 교사 허용(200), 타 부서 교사 차단(403)
   - 상위 영역(area) 상속: 세부 항목 미지정 시 상위 영역 1순위/2순위/3순위 설정 상속 승인 검증
3. 관리자 기준 관리 API (GET/PATCH/POST/DELETE) CRUD 및 teacherIds 매핑 라운드트립:
   - /api/criteria 및 /api/admin/criteria 별칭 엔드포인트 동등 동작 검증
   - 영역/항목 수정 시 teacherIds N명 동기화 및 assignedTeachers 상세 DTO 반환 검증
4. 교사 대시보드 (GET /api/teacher/dashboard) 1~3순위 필터링 검증:
   - 비담임 교과교사의 담당 항목에 대한 통계 및 최근 제출 목록 격리 검증
"""

import glob
import os
import sys
import unittest
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

# 동적 .venv 감지
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sp in glob.glob(os.path.join(_project_root, ".venv", "lib", "python*", "site-packages")):
    if os.path.exists(_sp) and _sp not in sys.path:
        sys.path.append(_sp)

from fastapi.testclient import TestClient

from core.academic import _YEAR_ID_CACHE
from core.authorization import evaluate_evaluator_access
from core.security import create_access_token
from database import get_db, get_redis
from main import app
import routers.students as students_mod


class EvaluatorHierarchyMockCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = 0
        self.rowcount = 0
        self._current_result: List[Any] = []
        self._fetch_index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self, query: str, params: tuple = ()):
        q = " ".join(query.strip().split())
        self._fetch_index = 0

        # =====================================================================
        # 1. DML 문 처리 (DELETE, INSERT, UPDATE 우선 판정)
        # =====================================================================
        if q.startswith("DELETE FROM certification_area_teachers") or "DELETE FROM certification_area_teachers" in q:
            aid = params[0]
            self.conn.db_area_teachers = [cat for cat in self.conn.db_area_teachers if cat["area_id"] != aid]
            self.rowcount = 1
            self._current_result = []
            return

        if q.startswith("DELETE FROM evaluation_item_teachers") or "DELETE FROM evaluation_item_teachers" in q:
            iid = params[0]
            self.conn.db_item_teachers = [eit for eit in self.conn.db_item_teachers if eit["item_id"] != iid]
            self.rowcount = 1
            self._current_result = []
            return

        if q.startswith("INSERT INTO certification_area_teachers") or "INSERT INTO certification_area_teachers" in q:
            aid, tid = params[0], params[1]
            self.conn.db_area_teachers.append({"area_id": aid, "teacher_id": tid})
            self.rowcount = 1
            self._current_result = []
            return

        if q.startswith("INSERT INTO evaluation_item_teachers") or "INSERT INTO evaluation_item_teachers" in q:
            iid, tid = params[0], params[1]
            self.conn.db_item_teachers.append({"item_id": iid, "teacher_id": tid})
            self.rowcount = 1
            self._current_result = []
            return

        if q.startswith("INSERT INTO evaluation_items") or "INSERT INTO evaluation_items" in q:
            self.conn.item_seq += 1
            self.lastrowid = self.conn.item_seq
            if "subject" in q:
                new_item = {
                    "item_id": self.lastrowid,
                    "area_id": params[0],
                    "name": params[1],
                    "subject": params[2],
                    "department": params[3],
                    "max_score": float(params[4]),
                    "scoring_type": params[5],
                    "target_grade": params[6],
                    "requires_evidence": params[7],
                    "is_active": True,
                    "is_deleted": False,
                }
            else:
                new_item = {
                    "item_id": self.lastrowid,
                    "area_id": params[0],
                    "name": params[1],
                    "department": params[2],
                    "max_score": float(params[3]),
                    "scoring_type": params[4],
                    "target_grade": params[5],
                    "requires_evidence": params[6],
                    "subject": None,
                    "is_active": True,
                    "is_deleted": False,
                }
            self.conn.db_items.append(new_item)
            self.rowcount = 1
            self._current_result = []
            return

        if q.startswith("UPDATE certification_areas") or "UPDATE certification_areas" in q:
            aid = params[-1]
            for a in self.conn.db_areas:
                if a["area_id"] == aid:
                    if "name = %s" in q:
                        a["name"] = params[0]
                    if "max_score = %s" in q:
                        a["max_score"] = float(params[1])
                    if "subject = %s" in q:
                        a["subject"] = params[2]
                    if "department = %s" in q:
                        a["department"] = params[3]
                    break
            self.rowcount = 1
            self._current_result = []
            return

        if q.startswith("UPDATE evaluation_items") or "UPDATE evaluation_items" in q:
            if "is_deleted = TRUE" in q or "is_active = FALSE" in q:
                iid = params[0]
                for it in self.conn.db_items:
                    if it["item_id"] == iid:
                        it["is_deleted"] = True
                        it["is_active"] = False
                        break
            else:
                iid = params[-1]
                for it in self.conn.db_items:
                    if it["item_id"] == iid:
                        it["name"] = params[0]
                        if "subject = %s" in q and len(params) > 4:
                            it["subject"] = params[1]
                            it["department"] = params[2]
                            it["max_score"] = float(params[3])
                        elif len(params) > 3:
                            it["department"] = params[1]
                            it["max_score"] = float(params[2])
                        break
            self.rowcount = 1
            self._current_result = []
            return

        if q.startswith("UPDATE submissions") or "UPDATE submissions" in q:
            score, reviewer_id, comment, sub_id = params[0], params[1], params[2], params[3]
            for s in self.conn.db_submissions:
                if s["submission_id"] == sub_id:
                    s["status_code"] = 3
                    s["granted_score"] = score
                    s["reviewer_id"] = reviewer_id
                    s["teacher_comment"] = comment
                    break
            self.rowcount = 1
            self._current_result = []
            return

        if q.startswith("INSERT INTO submissions_logs") or "INSERT INTO submissions_logs" in q:
            self.rowcount = 1
            self._current_result = []
            return

        # =====================================================================
        # 2. 통계 / 대시보드 쿼리 우선 판정
        # =====================================================================
        if "pending_count" in q:
            self._current_result = [{"pending_count": 2, "resubmitted_count": 0, "unscored_count": 2}]
            return

        if "student_count" in q:
            self._current_result = [{"student_count": 1}]
            return

        if "ORDER BY s.created_at" in q or "recentSubmissions" in q:
            self._current_result = [
                {
                    "submission_id": 1,
                    "student_id": 1,
                    "student_name": "학생1",
                    "grade": 1,
                    "class_no": 1,
                    "number": 1,
                    "area_id": 1,
                    "area_name": "전공역량",
                    "item_id": 101,
                    "item_name": "수학올림피아드",
                    "detail": "취득",
                    "activity_date": date(2026, 4, 1),
                    "file_path": None,
                    "link_url": None,
                    "description": "제출",
                    "status_code": 1,
                    "granted_score": None,
                    "created_at": datetime.now(),
                    "is_resubmitted": 0,
                }
            ]
            return

        if "LAST_INSERT_ID" in q:
            self._current_result = [{"last_id": self.lastrowid or self.conn.item_seq}]
            return

        # =====================================================================
        # 3. SELECT 쿼리 판정
        # =====================================================================
        if "FROM academic_years" in q:
            if "WHERE year = %s" in q:
                y_val = int(params[0])
                self._current_result = [y for y in self.conn.db_years if y["year"] == y_val]
            else:
                self._current_result = list(self.conn.db_years)

        elif "FROM student_academic_records" in q:
            st_id = params[0]
            matched = [r for r in self.conn.db_records if r["student_id"] == st_id]
            self._current_result = matched

        elif "FROM students" in q:
            if "WHERE student_id = %s" in q:
                st_id = params[0]
                self._current_result = [s for s in self.conn.db_students if s["student_id"] == st_id and not s.get("is_deleted")]
            elif "WHERE uuid = %s" in q:
                st_uuid = params[0]
                self._current_result = [s for s in self.conn.db_students if s["uuid"] == st_uuid and not s.get("is_deleted")]
            else:
                self._current_result = list(self.conn.db_students)

        elif "FROM certification_area_teachers" in q:
            t_map = {t["teachers_id"]: t for t in self.conn.db_teachers if not t.get("is_deleted")}
            res = []
            for cat in self.conn.db_area_teachers:
                if not params or cat["area_id"] in params:
                    t = t_map.get(cat["teacher_id"])
                    if t is not None:
                        res.append({
                            "area_id": cat["area_id"],
                            "teacher_id": cat["teacher_id"],
                            "teachers_id": cat["teacher_id"],
                            "name": t.get("name"),
                            "subject": t.get("subject"),
                            "department": t.get("department"),
                        })
            self._current_result = res

        elif "FROM evaluation_item_teachers" in q:
            t_map = {t["teachers_id"]: t for t in self.conn.db_teachers if not t.get("is_deleted")}
            res = []
            for eit in self.conn.db_item_teachers:
                if not params or eit["item_id"] in params:
                    t = t_map.get(eit["teacher_id"])
                    if t is not None:
                        res.append({
                            "item_id": eit["item_id"],
                            "teacher_id": eit["teacher_id"],
                            "teachers_id": eit["teacher_id"],
                            "name": t.get("name"),
                            "subject": t.get("subject"),
                            "department": t.get("department"),
                        })
            self._current_result = res

        elif "FROM teachers" in q:
            if "WHERE uuid = %s" in q:
                t_uuid = params[0]
                self._current_result = [t for t in self.conn.db_teachers if t["uuid"] == t_uuid and not t.get("is_deleted")]
            elif "WHERE teachers_id = %s" in q:
                t_id = params[0]
                self._current_result = [t for t in self.conn.db_teachers if t["teachers_id"] == t_id and not t.get("is_deleted")]
            elif "WHERE teachers_id IN" in q:
                self._current_result = [t for t in self.conn.db_teachers if t["teachers_id"] in params and not t.get("is_deleted")]
            else:
                self._current_result = list(self.conn.db_teachers)

        elif "FROM certification_areas" in q:
            if "WHERE area_id = %s" in q:
                aid = params[0]
                self._current_result = [a for a in self.conn.db_areas if a["area_id"] == aid]
            elif "WHERE year_id = %s" in q:
                yid = params[0]
                matched = [a for a in self.conn.db_areas if a["year_id"] == yid]
                if "AND grade = %s" in q:
                    g_val = params[1]
                    matched = [a for a in matched if a.get("grade") == g_val]
                self._current_result = matched
            else:
                self._current_result = list(self.conn.db_areas)

        elif "FROM evaluation_items" in q:
            if "WHERE item_id = %s" in q or "WHERE ei.item_id = %s" in q:
                it_id = params[0]
                self._current_result = [it for it in self.conn.db_items if it["item_id"] == it_id and not it.get("is_deleted")]
            elif "WHERE area_id = %s" in q:
                aid = params[0]
                self._current_result = [it for it in self.conn.db_items if it["area_id"] == aid and not it.get("is_deleted")]
            elif "WHERE area_id IN" in q:
                self._current_result = [it for it in self.conn.db_items if it["area_id"] in params and not it.get("is_deleted")]
            else:
                self._current_result = [it for it in self.conn.db_items if not it.get("is_deleted")]

        elif "FROM submissions" in q:
            if "WHERE s.submission_id = %s" in q or "WHERE submission_id = %s" in q:
                sub_id = params[0]
                matched_subs = [s for s in self.conn.db_submissions if s["submission_id"] == sub_id and not s.get("is_deleted")]
                if matched_subs:
                    s_obj = matched_subs[0]
                    it_obj = next((it for it in self.conn.db_items if it["item_id"] == s_obj["item_id"]), {})
                    ar_obj = next((a for a in self.conn.db_areas if a["area_id"] == it_obj.get("area_id")), {})
                    st_obj = next((st for st in self.conn.db_students if st["student_id"] == s_obj["student_id"]), {})
                    self._current_result = [
                        {
                            "submission_id": s_obj["submission_id"],
                            "student_id": s_obj["student_id"],
                            "student_uuid": st_obj.get("uuid"),
                            "status_code": s_obj["status_code"],
                            "granted_score": s_obj.get("granted_score"),
                            "item_id": it_obj.get("item_id"),
                            "item_name": it_obj.get("name"),
                            "item_subject": it_obj.get("subject"),
                            "item_department": it_obj.get("department"),
                            "max_score": it_obj.get("max_score", 10.0),
                            "area_id": ar_obj.get("area_id"),
                            "area_name": ar_obj.get("name"),
                            "area_subject": ar_obj.get("subject"),
                            "area_department": ar_obj.get("department"),
                            "year_id": ar_obj.get("year_id", 101),
                            "year": 2026,
                        }
                    ]
                else:
                    self._current_result = []
            elif "WHERE s.student_id = %s" in q or "WHERE student_id = %s" in q:
                st_id = params[1] if len(params) > 1 and "year_id" in q else params[0]
                self._current_result = [s for s in self.conn.db_submissions if s["student_id"] == st_id and not s.get("is_deleted")]
            else:
                self._current_result = list(self.conn.db_submissions)

        elif "FROM merits" in q:
            st_id = params[0] if params else 1
            self._current_result = [m for m in self.conn.db_merits if m["student_id"] == st_id and not m.get("is_deleted")]

        else:
            self._current_result = []

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


class EvaluatorHierarchyMockConnection:
    def __init__(self):
        self.item_seq = 200

        self.db_years = [
            {"year_id": 101, "year": 2026, "is_activated": True}
        ]
        self.db_students = [
            {"student_id": 1, "uuid": "st-uuid-1", "name": "학생1", "is_deleted": False}
        ]
        self.db_records = [
            {"student_id": 1, "grade": 1, "class_no": 1, "class": 1, "number": 1, "year_id": 101, "year": 2026}
        ]
        self.db_teachers = [
            {"teachers_id": 10, "uuid": "t-uuid-10", "name": "교사A(수학/교육연구)", "subject": "수학", "department": "교육연구부", "position": "교과전담", "grade": None, "class": None, "is_deleted": False},
            {"teachers_id": 11, "uuid": "t-uuid-11", "name": "교사B(수학/교육연구)", "subject": "수학", "department": "교육연구부", "position": "교과전담", "grade": None, "class": None, "is_deleted": False},
            {"teachers_id": 12, "uuid": "t-uuid-12", "name": "교사C(수학/교육연구/비배정)", "subject": "수학", "department": "교육연구부", "position": "교과전담", "grade": None, "class": None, "is_deleted": False},
            {"teachers_id": 13, "uuid": "t-uuid-13", "name": "교사D(국어/교육연구)", "subject": "국어", "department": "교육연구부", "position": "교과전담", "grade": None, "class": None, "is_deleted": False},
            {"teachers_id": 14, "uuid": "t-uuid-14", "name": "교사E(도덕/학생생활)", "subject": "도덕", "department": "학생생활지도부", "position": "교과전담", "grade": None, "class": None, "is_deleted": False},
            {"teachers_id": 15, "uuid": "t-uuid-15", "name": "교사F(영어/외국어)", "subject": "영어", "department": "외국어교육부", "position": "교과전담", "grade": None, "class": None, "is_deleted": False},
            {"teachers_id": 1, "uuid": "admin-uuid", "name": "관리자", "subject": None, "department": None, "position": "관리자", "grade": None, "class": None, "is_deleted": False},
        ]
        self.db_areas = [
            {"area_id": 1, "year_id": 101, "grade": 1, "name": "전공역량", "max_score": 100.0, "subject": None, "department": None},
            {"area_id": 2, "year_id": 101, "grade": 1, "name": "외국어능력", "max_score": 50.0, "subject": "영어", "department": None},
            {"area_id": 3, "year_id": 101, "grade": 1, "name": "인성영역", "max_score": 50.0, "subject": None, "department": "학생생활지도부"},
        ]
        self.db_area_teachers = [
            {"area_id": 1, "teacher_id": 10},
            {"area_id": 1, "teacher_id": 11},
        ]
        self.db_items = [
            # Item 101: 1순위 전담 교사 지정 (10, 11)
            {"item_id": 101, "area_id": 1, "target_grade": 1, "name": "수학올림피아드", "description": "", "max_score": 10.0, "scoring_type": "SCORE", "subject": "수학", "department": "교육연구부", "is_active": True, "is_deleted": False},
            # Item 102: 2순위 교과 지정 (수학), 전담 교사 없음, department='교육연구부'
            {"item_id": 102, "area_id": 1, "target_grade": 1, "name": "수학경시대회", "description": "", "max_score": 10.0, "scoring_type": "SCORE", "subject": "수학", "department": "교육연구부", "is_active": True, "is_deleted": False},
            # Item 103: 3순위 부서 지정 (학생생활지도부), 전담 교사 없음, subject=None
            {"item_id": 103, "area_id": 3, "target_grade": 1, "name": "선행표창", "description": "", "max_score": 10.0, "scoring_type": "SCORE", "subject": None, "department": "학생생활지도부", "is_active": True, "is_deleted": False},
            # Item 104: 설정 전무 (상위 Area 1의 area_teachers [10, 11] 상속)
            {"item_id": 104, "area_id": 1, "target_grade": 1, "name": "상속과제1", "description": "", "max_score": 10.0, "scoring_type": "SCORE", "subject": None, "department": None, "is_active": True, "is_deleted": False},
            # Item 105: 설정 전무 (상위 Area 2의 subject='영어' 상속)
            {"item_id": 105, "area_id": 2, "target_grade": 1, "name": "상속과제2(영어)", "description": "", "max_score": 10.0, "scoring_type": "SCORE", "subject": None, "department": None, "is_active": True, "is_deleted": False},
        ]
        self.db_item_teachers = [
            {"item_id": 101, "teacher_id": 10},
            {"item_id": 101, "teacher_id": 11},
        ]
        self.db_submissions = [
            {"submission_id": 1, "student_id": 1, "item_id": 101, "status_code": 1, "granted_score": None, "is_deleted": False},
            {"submission_id": 2, "student_id": 1, "item_id": 102, "status_code": 1, "granted_score": None, "is_deleted": False},
            {"submission_id": 3, "student_id": 1, "item_id": 103, "status_code": 1, "granted_score": None, "is_deleted": False},
            {"submission_id": 4, "student_id": 1, "item_id": 104, "status_code": 1, "granted_score": None, "is_deleted": False},
            {"submission_id": 5, "student_id": 1, "item_id": 105, "status_code": 1, "granted_score": None, "is_deleted": False},
        ]
        self.db_merits: List[Any] = []

    async def autocommit(self, value=True):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def cursor(self, cursor=None):
        return EvaluatorHierarchyMockCursor(self)


class TestEvaluatorHierarchy(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.mock_conn = EvaluatorHierarchyMockConnection()
        self.mock_redis = MagicMock()

        async def _fake_redis_get(k):
            return None
        self.mock_redis.get = _fake_redis_get

        app.dependency_overrides[get_db] = lambda: self.mock_conn
        app.dependency_overrides[get_redis] = lambda: self.mock_redis
        students_mod._YEAR_ID_CACHE.clear()
        _YEAR_ID_CACHE.clear()

        # Token helpers
        self.token_teacher10 = create_access_token({"uuid": "t-uuid-10", "sub": "t-uuid-10", "role": "teacher"})
        self.token_teacher11 = create_access_token({"uuid": "t-uuid-11", "sub": "t-uuid-11", "role": "teacher"})
        self.token_teacher12 = create_access_token({"uuid": "t-uuid-12", "sub": "t-uuid-12", "role": "teacher"})
        self.token_teacher13 = create_access_token({"uuid": "t-uuid-13", "sub": "t-uuid-13", "role": "teacher"})
        self.token_teacher14 = create_access_token({"uuid": "t-uuid-14", "sub": "t-uuid-14", "role": "teacher"})
        self.token_teacher15 = create_access_token({"uuid": "t-uuid-15", "sub": "t-uuid-15", "role": "teacher"})
        self.token_admin = create_access_token({"uuid": "admin-uuid", "sub": "admin-uuid", "role": "admin"})

    def tearDown(self):
        app.dependency_overrides.clear()
        students_mod._YEAR_ID_CACHE.clear()
        _YEAR_ID_CACHE.clear()

    # =========================================================================
    # 1. evaluate_evaluator_access 단위 검증
    # =========================================================================

    def test_unit_priority_1_assigned_teachers(self):
        """1순위: 전담 교사 N명 목록 지정 시 배정 교사 허용, 비배정 교사(동일 과목/부서) 차단"""
        assigned = {10, 11}
        # 교사 10: 배정됨 -> PASS
        ok, msg = evaluate_evaluator_access(10, "교육연구부", "수학", assigned, "수학", "교육연구부")
        self.assertTrue(ok)
        self.assertIn("1순위: 전담 교사 일치", msg)

        # 교사 11: 배정됨 -> PASS
        ok, msg = evaluate_evaluator_access(11, "교육연구부", "수학", assigned, "수학", "교육연구부")
        self.assertTrue(ok)

        # 교사 12: 비배정 (동일 수학 교과, 동일 교육연구부 소속임에도 1순위 전담 원칙에 따라 차단)
        ok, msg = evaluate_evaluator_access(12, "교육연구부", "수학", assigned, "수학", "교육연구부")
        self.assertFalse(ok)
        self.assertIn("1순위: 지정된 전담 교사만 심사할 수 있습니다", msg)

    def test_unit_priority_2_subject_precedence_over_department(self):
        """2순위: 1순위 미지정 시 교과 우선 판정 (동일 부서 소속이어도 타 교과 교사는 차단)"""
        assigned = set()
        # 과제: subject='수학', department='교육연구부'
        # 교사 12: subject='수학', department='자연과학부' (타 부서이지만 2순위 교과 일치로 승인)
        ok, msg = evaluate_evaluator_access(12, "자연과학부", "수학", assigned, "수학", "교육연구부")
        self.assertTrue(ok)
        self.assertIn("2순위: 담당 교과 일치", msg)

        # 교사 13: subject='국어', department='교육연구부' (동일 부서 소속이지만 2순위 교과가 불일치하므로 차단)
        ok, msg = evaluate_evaluator_access(13, "교육연구부", "국어", assigned, "수학", "교육연구부")
        self.assertFalse(ok)
        self.assertIn("2순위: 수학 담당 교사만 심사할 수 있습니다", msg)

    def test_unit_priority_3_department(self):
        """3순위: 1, 2순위 미지정 시 부서 판정 (소속 부서 교사 허용, 타 부서 차단)"""
        assigned = set()
        # 과제: subject=None, department='학생생활지도부'
        # 교사 14: department='학생생활지도부', subject='도덕' -> PASS
        ok, msg = evaluate_evaluator_access(14, "학생생활지도부", "도덕", assigned, None, "학생생활지도부")
        self.assertTrue(ok)
        self.assertIn("3순위: 소속 부서 일치", msg)

        # 교사 13: department='교육연구부' -> FAIL
        ok, msg = evaluate_evaluator_access(13, "교육연구부", "국어", assigned, None, "학생생활지도부")
        self.assertFalse(ok)
        self.assertIn("3순위: 학생생활지도부 소속 교사만 심사할 수 있습니다", msg)

    def test_unit_priority_fallback_unassigned(self):
        """기본값: 1~3순위 모두 미지정 시 일반 비담임 교과교사 차단 및 레거시 폴백 판정"""
        assigned = set()
        # 과제: target_subject=None, target_department=None
        ok, msg = evaluate_evaluator_access(10, "교육연구부", "수학", assigned, None, None)
        self.assertFalse(ok)
        self.assertIn("기본: 별도 평가자 미지정", msg)

        # 레거시 무제한 교사 플래그 시에는 기본 허용
        ok, msg = evaluate_evaluator_access(99, None, None, set(), None, None, is_legacy_unrestricted=True)
        self.assertTrue(ok)

    # =========================================================================
    # 2. 증빙자료 승인 API (PATCH /api/submissions/{id}/approve) 통합 검증
    # =========================================================================

    def test_api_priority_1_assigned_teachers(self):
        """[통합] 1순위 전담 교사 A, B 승인 허용(200), 동일 교과/부서 비배정 교사 C 차단(403)"""
        # Submission 1: Item 101 (전담교사 10, 11 지정)
        # 1. 교사 A(id=10) 승인 -> 200 OK
        resp1 = self.client.patch(
            "/api/submissions/1/approve",
            json={"score": 8.0, "comment": "우수"},
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(resp1.status_code, 200)
        self.assertTrue(resp1.json()["success"])

        # 원복
        self.mock_conn.db_submissions[0]["status_code"] = 1

        # 2. 교사 B(id=11) 승인 -> 200 OK
        resp2 = self.client.patch(
            "/api/submissions/1/approve",
            json={"score": 9.0, "comment": "확인"},
            headers={"Authorization": f"Bearer {self.token_teacher11}"},
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.json()["success"])

        # 원복
        self.mock_conn.db_submissions[0]["status_code"] = 1

        # 3. 교사 C(id=12): 동일한 수학/교육연구부이지만 전담 목록에 없음 -> 403 차단
        resp3 = self.client.patch(
            "/api/submissions/1/approve",
            json={"score": 7.0, "comment": "승인시도"},
            headers={"Authorization": f"Bearer {self.token_teacher12}"},
        )
        self.assertEqual(resp3.status_code, 403)
        self.assertFalse(resp3.json()["success"])
        self.assertIn("1순위: 지정된 전담 교사만 심사할 수 있습니다", resp3.json()["error"]["message"])

    def test_api_priority_2_subject_precedence_over_department(self):
        """[통합] 2순위 교과(수학) 지정 시 수학 교사 허용(200), 동일 부서 국어 교사 차단(403)"""
        # Submission 2: Item 102 (전담 없음, subject='수학', department='교육연구부')
        # 1. 교사 C(id=12): subject='수학' -> 200 OK
        resp1 = self.client.patch(
            "/api/submissions/2/approve",
            json={"score": 10.0, "comment": "수학 승인"},
            headers={"Authorization": f"Bearer {self.token_teacher12}"},
        )
        self.assertEqual(resp1.status_code, 200)

        # 원복
        self.mock_conn.db_submissions[1]["status_code"] = 1

        # 2. 교사 D(id=13): subject='국어', department='교육연구부' -> 403 FORBIDDEN (교과 우선 원칙)
        resp2 = self.client.patch(
            "/api/submissions/2/approve",
            json={"score": 10.0, "comment": "국어 교사 우회 시도"},
            headers={"Authorization": f"Bearer {self.token_teacher13}"},
        )
        self.assertEqual(resp2.status_code, 403)
        self.assertIn("2순위: 수학 담당 교사만", resp2.json()["error"]["message"])

    def test_api_priority_3_department(self):
        """[통합] 3순위 부서(학생생활지도부) 지정 시 부서 교사 허용(200), 타 부서 교사 차단(403)"""
        # Submission 3: Item 103 (전담 없음, subject=None, department='학생생활지도부')
        # 1. 교사 E(id=14): department='학생생활지도부' -> 200 OK
        resp1 = self.client.patch(
            "/api/submissions/3/approve",
            json={"score": 5.0, "comment": "선행 인정"},
            headers={"Authorization": f"Bearer {self.token_teacher14}"},
        )
        self.assertEqual(resp1.status_code, 200)

        # 원복
        self.mock_conn.db_submissions[2]["status_code"] = 1

        # 2. 교사 D(id=13): department='교육연구부' -> 403 FORBIDDEN
        resp2 = self.client.patch(
            "/api/submissions/3/approve",
            json={"score": 5.0, "comment": "타 부서 승인 시도"},
            headers={"Authorization": f"Bearer {self.token_teacher13}"},
        )
        self.assertEqual(resp2.status_code, 403)
        self.assertIn("3순위: 학생생활지도부 소속 교사만", resp2.json()["error"]["message"])

    def test_api_area_inheritance_priority_1_and_2(self):
        """[통합] 상위 영역(Area) 상속: 1순위 전담 교사 및 2순위 교과 상속 승인 검증"""
        # Submission 4: Item 104 (항목 설정 없음 -> Area 1의 전담 교사 [10, 11] 상속)
        # 1. 교사 A(id=10) -> 200 OK
        resp1 = self.client.patch(
            "/api/submissions/4/approve",
            json={"score": 5.0, "comment": "상속 전담 승인"},
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(resp1.status_code, 200)

        # 2. 교사 C(id=12) -> 403 FORBIDDEN (상속된 1순위 전담 목록에 없음)
        resp2 = self.client.patch(
            "/api/submissions/4/approve",
            json={"score": 5.0, "comment": "상속 차단 검증"},
            headers={"Authorization": f"Bearer {self.token_teacher12}"},
        )
        self.assertEqual(resp2.status_code, 403)

        # Submission 5: Item 105 (항목 설정 없음 -> Area 2의 subject='영어' 상속)
        # 3. 교사 F(id=15, subject='영어') -> 200 OK
        resp3 = self.client.patch(
            "/api/submissions/5/approve",
            json={"score": 10.0, "comment": "상속 교과 영어 승인"},
            headers={"Authorization": f"Bearer {self.token_teacher15}"},
        )
        self.assertEqual(resp3.status_code, 200)

        # 4. 교사 A(id=10, subject='수학') -> 403 FORBIDDEN
        resp4 = self.client.patch(
            "/api/submissions/5/approve",
            json={"score": 10.0, "comment": "수학 교사 영어 항목 차단"},
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(resp4.status_code, 403)

    # =========================================================================
    # 3. 관리자 기준 관리 API (GET/PATCH/POST/DELETE) CRUD 및 teacherIds 매핑 라운드트립
    # =========================================================================

    def test_admin_criteria_crud_and_assigned_teachers(self):
        """관리자 평가 기준 CRUD 및 N명 배정 라운드트립 검증 (/api/criteria 및 /api/admin/criteria)"""
        # 1. GET /api/criteria 및 /api/admin/criteria 조회
        resp_get1 = self.client.get("/api/criteria?year=2026", headers={"Authorization": f"Bearer {self.token_admin}"})
        self.assertEqual(resp_get1.status_code, 200)
        data1 = resp_get1.json()["data"]
        self.assertIn("areas", data1)
        area1 = next(a for a in data1["areas"] if a["areaId"] == 1)
        self.assertEqual(area1["teacherIds"], [10, 11])
        self.assertEqual(len(area1["assignedTeachers"]), 2)

        resp_get2 = self.client.get("/api/admin/criteria?year=2026", headers={"Authorization": f"Bearer {self.token_admin}"})
        self.assertEqual(resp_get2.status_code, 200)

        # 2. PATCH /api/criteria/areas/1 및 /api/admin/criteria/areas/1
        patch_area_payload = {
            "name": "전공역량(수정)",
            "maxScore": 120.0,
            "subject": "전공",
            "department": "마이스터부",
            "teacherIds": [10, 12],
        }
        resp_patch_area = self.client.patch(
            "/api/admin/criteria/areas/1",
            json=patch_area_payload,
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(resp_patch_area.status_code, 200)
        patched_area = resp_patch_area.json()["data"]
        self.assertEqual(patched_area["name"], "전공역량(수정)")
        self.assertEqual(patched_area["subject"], "전공")
        self.assertEqual(patched_area["department"], "마이스터부")
        self.assertEqual(set(patched_area["teacherIds"]), {10, 12})

        # 3. POST /api/criteria/areas/1/items (신규 항목 및 N명 교사 등록)
        create_item_payload = {
            "name": "신규과제",
            "targetGrade": 1,
            "maxScore": 15.0,
            "scoringType": 1,
            "subject": "소프트웨어",
            "department": "교무기획부",
            "teacherIds": [10, 11, 12],
        }
        resp_create_item = self.client.post(
            "/api/criteria/areas/1/items",
            json=create_item_payload,
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(resp_create_item.status_code, 201)
        created_item = resp_create_item.json()["data"]
        new_item_id = created_item["itemId"]
        self.assertEqual(set(created_item["teacherIds"]), {10, 11, 12})
        self.assertEqual(len(created_item["assignedTeachers"]), 3)

        # 4. PATCH /api/criteria/items/{itemId} (수정 및 교사 재배정)
        update_item_payload = {
            "name": "신규과제(수정)",
            "subject": "정보",
            "teacherIds": [13],
        }
        resp_update_item = self.client.patch(
            f"/api/admin/criteria/items/{new_item_id}",
            json=update_item_payload,
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(resp_update_item.status_code, 200)
        updated_item = resp_update_item.json()["data"]
        self.assertEqual(updated_item["name"], "신규과제(수정)")
        self.assertEqual(updated_item["subject"], "정보")
        self.assertEqual(updated_item["teacherIds"], [13])

        # 5. DELETE /api/criteria/items/{itemId} (소프트 삭제)
        resp_del = self.client.delete(
            f"/api/admin/criteria/items/{new_item_id}",
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(resp_del.status_code, 200)
        self.assertTrue(resp_del.json()["data"]["deleted"])

    # =========================================================================
    # 4. 교사 대시보드 (GET /api/teacher/dashboard) 1~3순위 필터링 검증
    # =========================================================================

    def test_teacher_dashboard_non_homeroom_filtering(self):
        """비담임 교과전담 교사가 대시보드 조회 시 1~3순위 인가 필터 쿼리 정상 반영 및 조회 성공 검증"""
        # 교사 10 (수학/교육연구부 전담) 대시보드 조회
        resp = self.client.get(
            "/api/teacher/dashboard",
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        data = body["data"]
        self.assertEqual(data["pendingCount"], 2)
        self.assertIn("교육연구부", data["scopeLabel"])
        self.assertIn("수학", data["scopeLabel"])

    # =========================================================================
    # 5. [신규 검증] 3순위 부서 엄격 격리, 소프트 삭제 교사 배정 제외, 무효 교사 ID 방어
    # =========================================================================

    def test_unit_priority_3_department_isolation_no_subject_bleed(self):
        """3순위 부서 판정 시 교사의 담당 교과가 부서 토큰에 오염(bleed)되지 않는지 엄격 검증"""
        # 과제: target_department='외국어교육부', target_subject=None, assigned_teacher_ids=set()
        # 교사: department='교무기획부', subject='영어' (교과가 영어여도 3순위 부서 지정이므로 교무기획부 소속 교사는 차단되어야 함)
        ok, msg = evaluate_evaluator_access(
            teacher_id=99,
            teacher_dept="교무기획부",
            teacher_subj="영어",
            assigned_teacher_ids=set(),
            target_subject=None,
            target_department="외국어교육부",
        )
        self.assertFalse(ok)
        self.assertIn("3순위: 외국어교육부 소속 교사만 심사할 수 있습니다", msg)

    def test_api_priority_3_department_strict_isolation(self):
        """[통합] 3순위 부서(학생생활지도부) 지정 과제에 타 부서 교사(동일/유사 교과여도) 403 차단 검증"""
        # Submission 3: Item 103 (department='학생생활지도부', subject=None)
        # 교사 10: department='교육연구부', subject='수학' -> 403 차단
        resp = self.client.patch(
            "/api/submissions/3/approve",
            json={"score": 5.0, "comment": "타 부서 심사 시도"},
            headers={"Authorization": f"Bearer {self.token_teacher10}"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("3순위: 학생생활지도부 소속 교사만", resp.json()["error"]["message"])

    def test_deleted_teacher_not_returned_in_criteria(self):
        """소프트 삭제(is_deleted=TRUE)된 교사는 Criteria 조회 시 assignedTeachers 및 teacherIds에서 제외되는지 검증"""
        # 교사 11을 소프트 삭제 처리
        teacher11 = next(t for t in self.mock_conn.db_teachers if t["teachers_id"] == 11)
        teacher11["is_deleted"] = True

        resp = self.client.get("/api/criteria?year=2026", headers={"Authorization": f"Bearer {self.token_admin}"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        area1 = next(a for a in data["areas"] if a["areaId"] == 1)

        # 삭제된 교사 11은 제외되고 활성 교사 10만 반환되어야 함
        self.assertEqual(area1["teacherIds"], [10])
        self.assertEqual(len(area1["assignedTeachers"]), 1)
        self.assertEqual(area1["assignedTeachers"][0]["id"], 10)

        # 원복
        teacher11["is_deleted"] = False

    def test_admin_criteria_invalid_teacher_ids_sanitized(self):
        """관리자 criteria 수정 시 존재하지 않거나 삭제된 교사 ID가 포함되어도 유효 교사만 안전하게 배정되는지 검증"""
        # Area 1에 유효 교사 10과 존재하지 않는 교사 999999 전달
        payload = {
            "teacherIds": [10, 999999],
        }
        resp = self.client.patch(
            "/api/admin/criteria/areas/1",
            json=payload,
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["teacherIds"], [10])
        self.assertEqual(len(data["assignedTeachers"]), 1)
        self.assertEqual(data["assignedTeachers"][0]["id"], 10)

        # Item 101에 유효 교사 10과 999999 전달
        item_payload = {
            "teacherIds": [10, 999999],
        }
        resp_item = self.client.patch(
            "/api/admin/criteria/items/101",
            json=item_payload,
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(resp_item.status_code, 200)
        item_data = resp_item.json()["data"]
        self.assertEqual(item_data["teacherIds"], [10])
        self.assertEqual(len(item_data["assignedTeachers"]), 1)


if __name__ == "__main__":
    unittest.main()
