"""
core/calculator.py - 역량인증제 평가 점수 및 성취 등급/인증 상태 공통 계산 서비스
(SCORE-01: 서버 계산 계약 확정 및 학생/교사 화면 계산 코드 단일화)
"""

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple, Union

# 8대 배점 기준 유형 상수
SCORING_TYPE_FIXED = 1       # 고정점수형 (자격증 취득 시 25점 등)
SCORING_TYPE_GRADE = 2       # 등급환산형 (S=5점, A=4점, B=3점 등)
SCORING_TYPE_RANGE = 3       # 구간점수형 (TOEIC 800 이상 30점, 700~795점 25점 등)
SCORING_TYPE_TIME = 4        # 시간계산형 (봉사활동 시간 × 0.3점 등)
SCORING_TYPE_DEDUCT = 5      # 감점형 (출결 기본점수에서 결석/지각 감점)
SCORING_TYPE_BONUS = 6       # 가산점형 (대회 입상 시 추가 점수)
SCORING_TYPE_MAX_LIMIT = 7   # 최대점제한형 (여러 자료 합산하되 최대점까지만 인정)
SCORING_TYPE_BEST_ONLY = 8   # 최상위인정형 (동일 종목 성적 중 가장 높은 점수만 반영)

SCORING_TYPE_NAMES = {
    1: "고정점수형",
    2: "등급환산형",
    3: "구간점수형",
    4: "시간계산형",
    5: "감점형",
    6: "가산점형",
    7: "최대점제한형",
    8: "최상위인정형",
}


def round_decimal(value: float | Decimal, places: int = 1) -> float:
    """금융/성적 처리용 사사오입(ROUND_HALF_UP) 반올림"""
    d = Decimal(str(value))
    target = Decimal("10") ** -places
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def parse_grade(text: str) -> Optional[str]:
    """텍스트에서 취득 등급(S, A, B, C, D, 1등급 등) 추출"""
    if not text:
        return None
    m = re.search(r'\b([SABCDsabcd])\b|([1-9])\s*등급', text)
    if m:
        if m.group(1):
            return m.group(1).upper()
        if m.group(2):
            return f"{m.group(2)}등급"
    return None


def parse_numeric_score(text: str) -> Optional[float]:
    """텍스트에서 공인 성적 점수(숫자) 추출 (날짜 형식 제외)"""
    if not text:
        return None
    clean = re.sub(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}', '', text)
    matches = re.findall(r'(\d+(?:\.\d+)?)\s*(?:점|점수)?', clean)
    if matches:
        nums = [float(x) for x in matches if float(x) <= 2000]
        if nums:
            return nums[-1]
    return None


def parse_hours(text: str) -> Optional[float]:
    """텍스트에서 활동 시간(시간 단위 숫자) 추출"""
    if not text:
        return None
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:시간|hr|hours|h)\b', text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    clean = re.sub(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}', '', text)
    nums = re.findall(r'\b(\d+(?:\.\d+)?)\b', clean)
    if nums:
        return float(nums[0])
    return None


def match_range_condition(val: float, condition: str) -> bool:
    """수치 점수가 조건식(>=800, 700~795, 700-795 등)에 일치하는지 판정"""
    if not condition:
        return False
    cond = condition.strip().replace(" ", "").replace("점", "")
    if ">=" in cond or "이상" in cond:
        m_num = re.search(r'(\d+(?:\.\d+)?)', cond)
        if m_num and val >= float(m_num.group(1)):
            return True
    if "<=" in cond or "이하" in cond:
        m_num = re.search(r'(\d+(?:\.\d+)?)', cond)
        if m_num and val <= float(m_num.group(1)):
            return True
    m_range = re.search(r'(\d+(?:\.\d+)?)[~-](\d+(?:\.\d+)?)', cond)
    if m_range:
        low, high = float(m_range.group(1)), float(m_range.group(2))
        if low <= val <= high:
            return True
    try:
        if float(cond) == val:
            return True
    except ValueError:
        pass
    return False


def calculate_recommended_score(
    scoring_type: Optional[int],
    item_max_score: float,
    detail_text: Optional[str] = None,
    scoring_rules: Optional[List[Dict[str, Any]]] = None,
    rule_config: Optional[Dict[str, Any]] = None,
) -> float:
    """
    8대 배점 유형 및 상세 배점 규칙(scoring_rules / rule_config)에 따라
    단일 제출 증빙의 추천 취득 점수를 정밀 산출합니다.
    """
    sc_type = int(scoring_type or 1)
    max_score = float(item_max_score) if item_max_score is not None else 100.0
    rules = scoring_rules or []
    config = rule_config or {}

    # 1. 고정점수형: 설정된 배점(기본 max_score 또는 조건 일치 배점)
    if sc_type == SCORING_TYPE_FIXED:
        if rules and detail_text:
            text_clean = detail_text.strip().lower()
            for r in rules:
                c_val = str(r.get("condition_value") or "").strip().lower()
                if c_val and c_val in text_clean:
                    return min(float(r.get("score", max_score)), max_score)
        return max_score

    # 2. 등급환산형: 등급(S, A, B 등) 파싱 후 배점 환산
    elif sc_type == SCORING_TYPE_GRADE:
        grade = parse_grade(detail_text or "")
        if grade and rules:
            for r in rules:
                c_val = str(r.get("condition_value") or "").strip().upper()
                if c_val == grade.upper() or c_val.replace(" ", "") == grade.replace(" ", ""):
                    return min(float(r.get("score", 0.0)), max_score)
        # 기본 디폴트 규칙 (규칙 테이블이 비어있을 때)
        if grade:
            grade_defaults = {"S": 1.0, "A": 0.8, "B": 0.6, "C": 0.4, "D": 0.2, "1등급": 1.0, "2등급": 0.8, "3등급": 0.6}
            ratio = grade_defaults.get(grade, 0.5)
            return round_decimal(min(max_score * ratio, max_score), 1)
        return max_score

    # 3. 구간점수형: 수치 점수 파싱 후 구간 매핑
    elif sc_type == SCORING_TYPE_RANGE:
        num_score = parse_numeric_score(detail_text or "")
        if num_score is not None and rules:
            for r in rules:
                cond = str(r.get("condition_value") or "")
                if match_range_condition(num_score, cond):
                    return min(float(r.get("score", 0.0)), max_score)
        elif num_score is not None:
            # 규칙이 명시되지 않은 경우 수치 점수를 max_score 내로 비율 환산/클램핑
            return round_decimal(min(num_score, max_score), 1)
        return max_score

    # 4. 시간계산형: 활동 시간 × 단위 시간당 배점
    elif sc_type == SCORING_TYPE_TIME:
        hours = parse_hours(detail_text or "")
        unit_rate = float(config.get("unit_rate") or (rules[0].get("score") if rules else 0.3))
        if hours is not None:
            calc = hours * unit_rate
            return round_decimal(min(calc, max_score), 1)
        return max_score

    # 5. 감점형: 기본점수에서 차감 (최하 0점 클램핑)
    elif sc_type == SCORING_TYPE_DEDUCT:
        penalty_per_unit = float(config.get("penalty_per_unit") or (rules[0].get("score") if rules else 2.0))
        violations = parse_numeric_score(detail_text or "") or 1.0
        calc = max_score - (violations * penalty_per_unit)
        return round_decimal(max(0.0, min(calc, max_score)), 1)

    # 6. 가산점형: 기준 점수 + 가산점
    elif sc_type == SCORING_TYPE_BONUS:
        base = float(config.get("base_score") or 0.0)
        bonus = float(config.get("bonus") or (rules[0].get("score") if rules else 5.0))
        calc = base + bonus
        return round_decimal(min(calc, max_score), 1)

    # 7. 최대점제한형 & 8. 최상위인정형
    elif sc_type in (SCORING_TYPE_MAX_LIMIT, SCORING_TYPE_BEST_ONLY):
        num_score = parse_numeric_score(detail_text or "")
        if num_score is not None:
            return round_decimal(min(num_score, max_score), 1)
        return max_score

    return max_score


def calculate_area_grade(score: float, max_score: float) -> str:
    """영역별 취득 점수 비율에 따른 성취 등급 판정 (S, A, B, 미달성)"""
    if max_score <= 0:
        return "미달성"
    ratio = (score / max_score) * 100.0
    if ratio >= 90.0:
        return "S"
    elif ratio >= 80.0:
        return "A"
    elif ratio >= 70.0:
        return "B"
    return "미달성"


def calculate_area_status(grade: str, pending_count: int) -> str:
    """영역 진행 상태 판정 (달성, 검토중, 보완 필요)"""
    if grade in ("S", "A"):
        return "달성"
    if pending_count > 0:
        return "검토중"
    return "보완 필요"


def evaluate_composite_rule(
    scores: List[float],
    rule_config: Optional[Dict[str, Any]],
    item_max_limit: Optional[float],
    scoring_type: Optional[int] = None,
) -> float:
    """
    복합 배점 룰(Composite Rules)에 따라 항목 점수를 산출합니다.
    - rule_config 지원 유형:
      1) "BEST_WITH_BONUS":
         최상위 1건(100%) + 추가 건수당 bonus_per_extra점 가산 (최대 bonus_cap점 한도)
      2) "TIERED_DECAY":
         점수 내림차순 정렬 후 순위별 감쇄 비율 적용 (예: 1위 1.0, 2위 0.5, 3위 0.25 등)
      3) 기본값:
         scoring_type == 8 이면 최고 점수(max), 그 외에는 단순 누적(sum)
    - 최종 점수는 item_max_limit 초과 시 클램핑됩니다.
    """
    if not scores:
        return 0.0

    sorted_scores = sorted(scores, reverse=True)

    if rule_config and isinstance(rule_config, dict):
        rule_type = rule_config.get("type", "").upper()

        if rule_type == "BEST_WITH_BONUS":
            best = sorted_scores[0]
            extra_count = len(sorted_scores) - 1
            bonus_per_extra = float(rule_config.get("bonus_per_extra", 2.0))
            bonus_cap = float(rule_config.get("bonus_cap", 4.0))

            extra_bonus = min(extra_count * bonus_per_extra, bonus_cap)
            total = best + extra_bonus
            if item_max_limit is not None:
                total = min(total, item_max_limit)
            return round_decimal(total, 1)

        elif rule_type == "TIERED_DECAY":
            decay_rates = rule_config.get("decay_rates", [1.0, 0.5, 0.25])
            total = 0.0
            for idx, sc in enumerate(sorted_scores):
                rate = float(decay_rates[idx]) if idx < len(decay_rates) else 0.0
                total += sc * rate
            if item_max_limit is not None:
                total = min(total, item_max_limit)
            return round_decimal(total, 1)

    # 기본 평가 분기
    if scoring_type == SCORING_TYPE_BEST_ONLY:  # 8: 최상위인정형
        best = sorted_scores[0]
        if item_max_limit is not None:
            best = min(best, item_max_limit)
        return round_decimal(best, 1)
    elif scoring_type == SCORING_TYPE_DEDUCT:  # 5: 감점형
        base = item_max_limit if item_max_limit is not None else 100.0
        total = max(0.0, base - sum(sorted_scores))
        return round_decimal(total, 1)
    else:  # 일반 누적 합산형 (1: 고정점수, 2: 등급환산, 3: 구간점수, 4: 시간계산, 6: 가산점, 7: 최대점제한)
        item_sum = sum(sorted_scores)
        if item_max_limit is not None:
            item_sum = min(item_sum, item_max_limit)
        return round_decimal(item_sum, 1)


def calculate_student_certification(
    areas: List[Dict[str, Any]],
    submissions: List[Dict[str, Any]],
    merits: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    학생의 인증 영역 목록, 제출 증빙 목록, 상벌점 목록을 기반으로
    영역별 취득 점수, 성취 등급, 진행 상태 및 종합 인증 상태를 계산합니다.

    학생 현황 조회(2.1), 교사 학생 목록(3.2), 교사 학생 상세(3.3)에서 100% 동일한 로직을 공유합니다.
    """
    # 1. 항목별 승인 점수 및 메타데이터 분류
    area_item_scores: Dict[int, Dict[int, List[float]]] = {}
    area_item_meta: Dict[int, Dict[str, Any]] = {}
    area_pending_counts: Dict[int, int] = {}
    total_pending_count = 0

    for sub in submissions:
        aid = sub.get("area_id")
        iid = sub.get("item_id")
        st_code = sub.get("status_code", 1)
        g_score = float(sub.get("granted_score") or 0.0)
        sc_type = sub.get("scoring_type", 1)
        item_max = float(sub["item_max_score"]) if sub.get("item_max_score") is not None else None

        if iid:
            area_item_meta[iid] = {
                "scoring_type": sc_type,
                "item_max_score": item_max,
                "rule_config": sub.get("rule_config"),
            }

        # 상태 코드 1(제출완료) 또는 2(검토중) -> 심사 대기
        if st_code in (1, 2):
            total_pending_count += 1
            if aid:
                area_pending_counts[aid] = area_pending_counts.get(aid, 0) + 1

        # 상태 코드 3(인정완료) -> 합산 대상
        elif st_code == 3 and aid and iid:
            if aid not in area_item_scores:
                area_item_scores[aid] = {}
            if iid not in area_item_scores[aid]:
                area_item_scores[aid][iid] = []
            area_item_scores[aid][iid].append(g_score)

    # 2. 상벌점 영역별 가감점 집계 (SCORE-02)
    area_merit_points: Dict[str, float] = {}
    total_merit_score = 0.0
    for m in merits:
        if not m.get("is_reflected", True):
            continue
        m_type = (m.get("type") or "-").strip()
        pts = float(m.get("points") or 0.0)
        rel_area = (m.get("related_area") or "").strip()

        # 벌점은 음수화, 상점은 양수화
        signed_pts = -abs(pts) if m_type in ("-", "벌점") else abs(pts)
        total_merit_score += signed_pts
        if rel_area:
            area_merit_points[rel_area] = area_merit_points.get(rel_area, 0.0) + signed_pts

    # 3. 각 영역별 점수 계산
    area_results: List[Dict[str, Any]] = []
    total_score = 0.0
    all_areas_achieved = True if areas else False
    is_all_s = True if areas else False
    unmet_areas: List[Dict[str, Any]] = []

    for area in areas:
        aid = area["area_id"]
        aname = area["name"]
        amax = float(area["max_score"])

        items_dict = area_item_scores.get(aid, {})
        area_raw_score = 0.0

        for iid, scores in items_dict.items():
            meta = area_item_meta.get(iid, {})
            sc_type = meta.get("scoring_type")
            item_max_limit = meta.get("item_max_score")

            rule_cfg = meta.get("rule_config")
            item_score = evaluate_composite_rule(scores, rule_cfg, item_max_limit, sc_type)
            area_raw_score += item_score

        # 상벌점 가산/감점 반영
        merit_adj = area_merit_points.get(aname, 0.0)
        adjusted_score = area_raw_score + merit_adj

        # 0.0 ~ max_score 클램핑
        final_score = max(0.0, min(adjusted_score, amax))
        final_score = round_decimal(final_score, 1)

        grade_str = calculate_area_grade(final_score, amax)
        pending_cnt = area_pending_counts.get(aid, 0)
        status_str = calculate_area_status(grade_str, pending_cnt)

        if grade_str != "S":
            is_all_s = False

        if grade_str not in ("S", "A"):
            all_areas_achieved = False
            unmet_areas.append({
                "areaId": aid,
                "area": aname,
                "grade": grade_str,
                "score": final_score,
                "maxScore": amax,
            })

        total_score += final_score
        area_results.append({
            "areaId": aid,
            "area": aname,
            "score": final_score,
            "maxScore": amax,
            "grade": grade_str,
            "status": status_str,
            "pendingCount": pending_cnt,
        })

    total_score = round_decimal(total_score, 1)

    # 4. 종합 인증 상태(certStatus) 결정
    if not areas:
        cert_status = "보완 필요"
    elif all_areas_achieved:
        cert_status = "인증 가능"
    elif total_pending_count > 0:
        cert_status = "검토중"
    else:
        cert_status = "보완 필요"

    return {
        "areas": area_results,
        "totalScore": total_score,
        "certStatus": cert_status,
        "pendingCount": total_pending_count,
        "totalMeritScore": round_decimal(total_merit_score, 1),
        "pointTotal": round_decimal(total_merit_score, 1),
        "isAllS": is_all_s,
        "canPrintEvaluation": bool(all_areas_achieved),
        "unmetAreas": unmet_areas,
    }
