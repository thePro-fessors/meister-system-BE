"""
tests/test_composite_scoring_rules.py - 복합 배점 룰(Composite Rules) 계산 엔진 검증 스위트
"""

import unittest
from core.calculator import calculate_student_certification, evaluate_composite_rule


class TestCompositeScoringRules(unittest.TestCase):
    def test_01_best_with_bonus_rule(self):
        """1. BEST_WITH_BONUS (최상위 1건 100% + 추가 입상 가산점 Cap) 산정 검증"""
        rule_cfg = {
            "type": "BEST_WITH_BONUS",
            "bonus_per_extra": 2.0,
            "bonus_cap": 4.0,
        }

        # 1건만 입상 (10.0점)
        s1 = evaluate_composite_rule([10.0], rule_cfg, item_max_limit=20.0)
        self.assertEqual(s1, 10.0)

        # 2건 입상 (10.0점, 8.0점) -> 10.0 + 2.0 = 12.0점
        s2 = evaluate_composite_rule([10.0, 8.0], rule_cfg, item_max_limit=20.0)
        self.assertEqual(s2, 12.0)

        # 3건 입상 (10.0점, 8.0점, 6.0점) -> 10.0 + 4.0 (Cap 도달) = 14.0점
        s3 = evaluate_composite_rule([10.0, 8.0, 6.0], rule_cfg, item_max_limit=20.0)
        self.assertEqual(s3, 14.0)

        # 5건 입상 (추가 4건이지만 Cap=4.0에 걸림) -> 10.0 + 4.0 = 14.0점
        s5 = evaluate_composite_rule([10.0, 8.0, 6.0, 5.0, 4.0], rule_cfg, item_max_limit=20.0)
        self.assertEqual(s5, 14.0)

        # 항목 한도(item_max_limit=11.0) 클램핑 검증
        s_clamped = evaluate_composite_rule([10.0, 8.0], rule_cfg, item_max_limit=11.0)
        self.assertEqual(s_clamped, 11.0)

    def test_02_tiered_decay_rule(self):
        """2. TIERED_DECAY (순위별 감쇄 배점: 1위 100%, 2위 50%, 3위 25%) 산정 검증"""
        rule_cfg = {
            "type": "TIERED_DECAY",
            "decay_rates": [1.0, 0.5, 0.25],
        }

        # 1위 10.0점, 2위 10.0점, 3위 8.0점 -> 10*1.0 + 10*0.5 + 8*0.25 = 10 + 5 + 2 = 17.0점
        s = evaluate_composite_rule([10.0, 10.0, 8.0], rule_cfg, item_max_limit=25.0)
        self.assertEqual(s, 17.0)

    def test_03_calculate_student_certification_with_composite_rule(self):
        """3. calculate_student_certification 종합 인증 산정식 복합 룰 연동 검증"""
        areas = [{"area_id": 1, "name": "전공", "max_score": 25.0}]
        submissions = [
            {
                "area_id": 1,
                "item_id": 101,
                "status_code": 3,
                "granted_score": 10.0,
                "scoring_type": 1,
                "item_max_score": 20.0,
                "rule_config": {
                    "type": "BEST_WITH_BONUS",
                    "bonus_per_extra": 2.0,
                    "bonus_cap": 4.0,
                },
            },
            {
                "area_id": 1,
                "item_id": 101,
                "status_code": 3,
                "granted_score": 8.0,
                "scoring_type": 1,
                "item_max_score": 20.0,
                "rule_config": {
                    "type": "BEST_WITH_BONUS",
                    "bonus_per_extra": 2.0,
                    "bonus_cap": 4.0,
                },
            },
        ]

        result = calculate_student_certification(areas=areas, submissions=submissions, merits=[])
        # 10 + 2 = 12.0점
        self.assertEqual(result["totalScore"], 12.0)
        self.assertEqual(result["areas"][0]["score"], 12.0)
