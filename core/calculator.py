"""
core/calculator.py - 역량인증제 평가 점수 및 성취 등급/인증 상태 공통 계산 서비스
(SCORE-01: 서버 계산 계약 확정 및 학생/교사 화면 계산 코드 단일화)
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Tuple


def round_decimal(value: float | Decimal, places: int = 1) -> float:
    """금융/성적 처리용 사사오입(ROUND_HALF_UP) 반올림"""
    d = Decimal(str(value))
    target = Decimal("10") ** -places
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


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

            if sc_type == 8:  # 8: 최상위인정형
                best = max(scores) if scores else 0.0
                if item_max_limit is not None:
                    best = min(best, item_max_limit)
                area_raw_score += best
            else:  # 일반 누적 합산형
                item_sum = sum(scores)
                if item_max_limit is not None:
                    item_sum = min(item_sum, item_max_limit)
                area_raw_score += item_sum

        # 상벌점 가산/감점 반영
        merit_adj = area_merit_points.get(aname, 0.0)
        adjusted_score = area_raw_score + merit_adj

        # 0.0 ~ max_score 클램핑
        final_score = max(0.0, min(adjusted_score, amax))
        final_score = round_decimal(final_score, 1)

        grade_str = calculate_area_grade(final_score, amax)
        pending_cnt = area_pending_counts.get(aid, 0)
        status_str = calculate_area_status(grade_str, pending_cnt)

        if grade_str not in ("S", "A"):
            all_areas_achieved = False

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
    }
