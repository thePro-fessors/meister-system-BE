"""
tests/test_scoring_types.py - 8대 배점 유형 전수 산정 및 세부 규칙 단위 테스트
"""

import pytest
from core.calculator import (
    SCORING_TYPE_FIXED,
    SCORING_TYPE_GRADE,
    SCORING_TYPE_RANGE,
    SCORING_TYPE_TIME,
    SCORING_TYPE_DEDUCT,
    SCORING_TYPE_BONUS,
    SCORING_TYPE_MAX_LIMIT,
    SCORING_TYPE_BEST_ONLY,
    SCORING_TYPE_NAMES,
    calculate_recommended_score,
    evaluate_composite_rule,
    parse_grade,
    parse_numeric_score,
    parse_hours,
    match_range_condition,
)


def test_scoring_type_constants_and_names():
    assert len(SCORING_TYPE_NAMES) == 8
    assert SCORING_TYPE_NAMES[1] == "고정점수형"
    assert SCORING_TYPE_NAMES[2] == "등급환산형"
    assert SCORING_TYPE_NAMES[3] == "구간점수형"
    assert SCORING_TYPE_NAMES[4] == "시간계산형"
    assert SCORING_TYPE_NAMES[5] == "감점형"
    assert SCORING_TYPE_NAMES[6] == "가산점형"
    assert SCORING_TYPE_NAMES[7] == "최대점제한형"
    assert SCORING_TYPE_NAMES[8] == "최상위인정형"


def test_type1_fixed_scoring():
    # 조건 없이 만점 부여
    score = calculate_recommended_score(
        scoring_type=SCORING_TYPE_FIXED,
        item_max_score=25.0,
        detail_text="정보처리기능사 자격증 취득",
    )
    assert score == 25.0

    # 조건별 배점이 있을 때
    rules = [
        {"condition_value": "정보처리기능사", "score": 20.0},
        {"condition_value": "정보처리산업기사", "score": 25.0},
    ]
    score_craftsman = calculate_recommended_score(
        scoring_type=SCORING_TYPE_FIXED,
        item_max_score=25.0,
        detail_text="정보처리기능사",
        scoring_rules=rules,
    )
    assert score_craftsman == 20.0


def test_type2_grade_conversion():
    rules = [
        {"condition_value": "S", "score": 10.0},
        {"condition_value": "A", "score": 8.0},
        {"condition_value": "B", "score": 6.0},
        {"condition_value": "C", "score": 4.0},
    ]
    # S등급 파싱 및 환산
    score_s = calculate_recommended_score(
        scoring_type=SCORING_TYPE_GRADE,
        item_max_score=10.0,
        detail_text="전공실기능력평가 S등급 달성",
        scoring_rules=rules,
    )
    assert score_s == 10.0

    # B등급 파싱 및 환산
    score_b = calculate_recommended_score(
        scoring_type=SCORING_TYPE_GRADE,
        item_max_score=10.0,
        detail_text="성취도 B",
        scoring_rules=rules,
    )
    assert score_b == 6.0


def test_type3_range_scoring():
    rules = [
        {"condition_value": ">=800", "score": 30.0},
        {"condition_value": "700~795", "score": 25.0},
        {"condition_value": "600~695", "score": 20.0},
    ]
    # 850점 (800 이상)
    assert calculate_recommended_score(
        scoring_type=SCORING_TYPE_RANGE,
        item_max_score=30.0,
        detail_text="TOEIC 850점",
        scoring_rules=rules,
    ) == 30.0

    # 750점 (700~795)
    assert calculate_recommended_score(
        scoring_type=SCORING_TYPE_RANGE,
        item_max_score=30.0,
        detail_text="토익 750점 취득",
        scoring_rules=rules,
    ) == 25.0

    # 620점 (600~695)
    assert calculate_recommended_score(
        scoring_type=SCORING_TYPE_RANGE,
        item_max_score=30.0,
        detail_text="정기토익 620",
        scoring_rules=rules,
    ) == 20.0


def test_type4_time_calculation():
    # 20시간 × 0.5점 = 10점 (최대점 15점 한도)
    assert calculate_recommended_score(
        scoring_type=SCORING_TYPE_TIME,
        item_max_score=15.0,
        detail_text="지역사회 봉사활동 20시간",
        rule_config={"unit_rate": 0.5},
    ) == 10.0

    # 40시간 × 0.5점 = 20점 -> 최대점 15점 클램핑
    assert calculate_recommended_score(
        scoring_type=SCORING_TYPE_TIME,
        item_max_score=15.0,
        detail_text="교내 환경정화 40시간",
        rule_config={"unit_rate": 0.5},
    ) == 15.0


def test_type5_deduction():
    # 기본점수 20점에서 무단결석 2회 × 3점 감점 = 14점
    assert calculate_recommended_score(
        scoring_type=SCORING_TYPE_DEDUCT,
        item_max_score=20.0,
        detail_text="미인정 결석 2회",
        rule_config={"penalty_per_unit": 3.0},
    ) == 14.0

    # 10회 감점 = 0점 하한선 클램핑
    assert calculate_recommended_score(
        scoring_type=SCORING_TYPE_DEDUCT,
        item_max_score=20.0,
        detail_text="지각 10회",
        rule_config={"penalty_per_unit": 3.0},
    ) == 0.0


def test_type6_bonus():
    # 기본점수 5점 + 가산점 3점 = 8점
    assert calculate_recommended_score(
        scoring_type=SCORING_TYPE_BONUS,
        item_max_score=10.0,
        detail_text="교내 알고리즘 경진대회 특별상",
        rule_config={"base_score": 5.0, "bonus": 3.0},
    ) == 8.0


def test_type7_max_limit_composite():
    # 3건의 제출 점수 (8점, 6점, 4점) 단순 합산 18점 -> 항목 최대점 15점 클램핑
    scores = [8.0, 6.0, 4.0]
    total = evaluate_composite_rule(
        scores=scores,
        rule_config=None,
        item_max_limit=15.0,
        scoring_type=SCORING_TYPE_MAX_LIMIT,
    )
    assert total == 15.0


def test_type8_best_only_composite():
    # 3건의 제출 점수 (15점, 25점, 10점) 중 최상위 1건만 반영 -> 25점
    scores = [15.0, 25.0, 10.0]
    total = evaluate_composite_rule(
        scores=scores,
        rule_config=None,
        item_max_limit=30.0,
        scoring_type=SCORING_TYPE_BEST_ONLY,
    )
    assert total == 25.0
