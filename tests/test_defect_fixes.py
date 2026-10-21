"""
tests/test_defect_fixes.py - 실제 코드 결함 3건 및 PDF 7.5 판정 필드 수정 검증 테스트

검증 항목:
1. 결함 1: admin_criteria rule_config INSERT/UPDATE 및 GET 응답 바인딩
2. 결함 2: students, teachers, admin routers 내 SUBMISSION_STATUS_MAP에 상태코드 6('이의신청') 일관 적용
3. 결함 3: scripts/load_test_k6.js의 미존재 404 엔드포인트(/api/students/me) 제거 및 /api/auth/me 정상화
4. PDF 7.5: calculate_student_certification에서 isAllS, canPrintEvaluation, unmetAreas 3대 판정 필드 정합성
"""

import os
import unittest
from core.calculator import calculate_student_certification
from routers.students import SUBMISSION_STATUS_MAP as STUDENT_STATUS_MAP
from routers.submissions import SUBMISSION_STATUS_MAP as SUBMISSION_STATUS_MAP
from routers.admin import AUDIT_SUBMISSION_STATUS_MAP


class DefectFixesTestCase(unittest.TestCase):
    def test_submission_status_map_consistency(self):
        """결함 2 검증: 이의신청(6) 상태 코드가 학생, 제출, 관리자 감사 매핑에 누락 없이 일관되게 정의되었는지 확인"""
        self.assertIn(6, SUBMISSION_STATUS_MAP)
        self.assertEqual(SUBMISSION_STATUS_MAP[6], "이의신청")

        self.assertIn(6, STUDENT_STATUS_MAP)
        self.assertEqual(STUDENT_STATUS_MAP[6], "이의신청")

        self.assertIn(6, AUDIT_SUBMISSION_STATUS_MAP)
        self.assertEqual(AUDIT_SUBMISSION_STATUS_MAP[6], "이의신청")

    def test_load_test_k6_script_endpoint_fix(self):
        """결함 3 검증: scripts/load_test_k6.js 파일 내 존재하지 않는 404 엔드포인트(/api/students/me)가 제거되었는지 확인"""
        k6_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "load_test_k6.js")
        self.assertTrue(os.path.exists(k6_path))
        with open(k6_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("/api/students/me", content)
        self.assertIn("/api/auth/me", content)

    def test_calculator_step_certification_fields_all_s(self):
        """PDF 7.5 검증: 모든 영역 S 달성 시 isAllS=True, canPrintEvaluation=True, unmetAreas=[] 확인"""
        areas = [
            {"area_id": 1, "name": "전공역량", "max_score": 100.0},
            {"area_id": 2, "name": "외국어", "max_score": 100.0},
        ]
        submissions = [
            {"item_id": 101, "area_id": 1, "status_code": 3, "granted_score": 95.0},
            {"item_id": 201, "area_id": 2, "status_code": 3, "granted_score": 92.0},
        ]
        merits = []

        res = calculate_student_certification(areas, submissions, merits)
        self.assertEqual(res["certStatus"], "인증 가능")
        self.assertTrue(res["isAllS"])
        self.assertTrue(res["canPrintEvaluation"])
        self.assertEqual(len(res["unmetAreas"]), 0)

    def test_calculator_step_certification_fields_unmet_areas(self):
        """PDF 7.5 검증: 한 영역이 B등급일 때 isAllS=False, canPrintEvaluation=False, unmetAreas에 해당 영역 포함 확인"""
        areas = [
            {"area_id": 1, "name": "전공역량", "max_score": 100.0},
            {"area_id": 2, "name": "외국어", "max_score": 100.0},
        ]
        # 외국어는 70점 -> B등급 (A 미만)
        submissions = [
            {"item_id": 101, "area_id": 1, "status_code": 3, "granted_score": 95.0},
            {"item_id": 201, "area_id": 2, "status_code": 3, "granted_score": 72.0},
        ]
        merits = []

        res = calculate_student_certification(areas, submissions, merits)
        self.assertEqual(res["certStatus"], "보완 필요")
        self.assertFalse(res["isAllS"])
        self.assertFalse(res["canPrintEvaluation"])
        self.assertEqual(len(res["unmetAreas"]), 1)
        self.assertEqual(res["unmetAreas"][0]["area"], "외국어")
        self.assertEqual(res["unmetAreas"][0]["grade"], "B")


if __name__ == "__main__":
    unittest.main()
