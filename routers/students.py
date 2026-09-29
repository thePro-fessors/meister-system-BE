"""
routers/students.py - 마이스터 역량인증제 학생 업무 관련 API 라우터

명세 및 연동 기준:
- Tech_spec.md 2장 (학생 기능)
- BACKEND_REQUIREMENTS.md 4장 (학생 화면 API), 7장 (점수 계산 및 인증 판정 규칙)
- TODO.md 2.1 (인증 현황 조회 API: GET /api/students/{studentId}/certification-status)
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import math

import asyncmy
from asyncmy.cursors import DictCursor
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.security import get_current_user, require_student
from database import get_db
from sr_format import Error, SrFormat

router = APIRouter(
    prefix="/api/students",
    tags=["students"],
)

# ==============================================================================
# 0. 인메모리 캐시 (In-Memory Cache)
# ==============================================================================
# 학사년도(year, 예: 2026) -> DB 고유 식별자(year_id) 매핑 캐시
# 매 요청마다 academic_years 테이블에 불필요한 반복 쿼리가 발생하는 병목을 제거합니다.
_YEAR_ID_CACHE: dict[int, int] = {}


# ==============================================================================
# 1. 헬퍼 유틸리티 (Domain Logic & Calculation Rule Helpers)
# ==============================================================================

async def get_current_academic_year(target_date: Optional[datetime] = None) -> int:
    """
    3월 1일 학사력 기준 현재 학년도 계산 함수
    
    규칙:
    - 초·중·고 학사 일정상 1월과 2월은 직전 학년도의 학기(겨울방학/학기말)에 속합니다.
    - 예: 2026년 1월/2월 -> 2025 학년도
    - 예: 2026년 3월~12월 -> 2026 학년도
    """
    now = target_date or datetime.now()
    if now.month < 3:
        return now.year - 1
    return now.year


async def get_year_id_by_year(conn: asyncmy.Connection, year: int) -> Optional[int]:
    """
    학년도 정수(예: 2026)를 기반으로 academic_years 테이블의 year_id를 조회합니다.
    
    성능 최적화:
    - 프로세스 인메모리 딕셔너리(_YEAR_ID_CACHE)를 먼저 확인하고,
      캐시 미스 시에만 DB 쿼리를 수행한 뒤 캐시에 적재합니다.
    """
    global _YEAR_ID_CACHE
    if year in _YEAR_ID_CACHE:
        return _YEAR_ID_CACHE[year]

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            "SELECT year_id FROM academic_years WHERE year = %s LIMIT 1",
            (year,)
        )
        row = await cur.fetchone()
        if row:
            _YEAR_ID_CACHE[year] = row["year_id"]
            return row["year_id"]
    return None


def calculate_area_grade(score: float, max_score: float) -> str:
    """
    FE 요구사항 7장 기준 영역별 성취 등급(Grade) 산정 로직
    
    등급 판정 기준:
    - S: 영역 최대 배점의 90% 이상 (score >= max_score * 0.9)
    - A: 영역 최대 배점의 80% 이상 90% 미만 (score >= max_score * 0.8)
    - B: 영역 최대 배점의 70% 이상 80% 미만 (score >= max_score * 0.7)
    - 미달성: 영역 최대 배점의 70% 미만 또는 최대 배점이 0 이하인 경우
    """
    if max_score <= 0:
        return "미달성"

    ratio = (score / max_score) * 100.0
    if ratio >= 90.0:
        return "S"
    elif ratio >= 80.0:
        return "A"
    elif ratio >= 70.0:
        return "B"
    else:
        return "미달성"


def calculate_area_status(grade: str, pending_count: int) -> str:
    """
    영역별 현재 진행/심사 상태 문자열 판정
    
    상태 기준:
    - 등급이 'S' 또는 'A'인 경우: 이미 목표를 달성하였으므로 "달성"
    - 등급이 'B' 또는 '미달성'이지만, 아직 교사 심사 대기 중인 증빙이 있는 경우: "검토중"
    - 검토 중인 건도 없고 기준 점수에 미달한 경우: 추가 활동이 필요하므로 "보완 필요"
    """
    if grade in ("S", "A"):
        return "달성"
    if pending_count > 0:
        return "검토중"
    return "보완 필요"


def calculate_cert_status(area_results: List[Dict[str, Any]], has_pending: bool) -> str:
    """
    마이스터 역량인증제 종합 최종 인증 판정 (FE 7장 명세)
    
    판정 우선순위:
    1. '인증 가능': 평가 대상인 5개 모든 영역이 각각 'S' 또는 'A' 등급을 충족한 경우
    2. '검토중': 한 영역이라도 B/미달성이지만 교사 심사 대기(제출완료/검토중) 중인 증빙자료가 존재하는 경우
    3. '보완 필요': 미달 영역이 존재하며 더 이상 대기 중인 증빙자료가 없는 경우 (학생의 추가 제출 필요)
    """
    if not area_results:
        return "미달성"

    # 모든 영역이 S 또는 A인지 전수 검사
    all_passed = all(area["grade"] in ("S", "A") for area in area_results)
    if all_passed:
        return "인증 가능"

    # 미달성 영역이 있으나 교사 검토 대기 건이 남아있는 경우
    if has_pending:
        return "검토중"

    # 추가 증빙 제출이 필요한 상태
    return "보완 필요"


# ==============================================================================
# 2. Pydantic 요청/응답 스키마 (DTO)
# ==============================================================================

class AreaStatusItem(BaseModel):
    """5대 영역별 세부 인증 현황 응답 객체"""
    area: str = Field(..., description="인증 영역명 (예: 전문기술역량, 인성/직업의식 등)")
    score: float = Field(..., description="학생 취득 점수 (상벌점 반영 및 소수점 1자리 반올림)")
    maxScore: float = Field(..., description="해당 학년/영역의 최대 배점")
    grade: str = Field(..., description="영역 등급 (S, A, B, 미달성)")
    status: str = Field(..., description="영역 진행 상태 (달성, 검토중, 보완 필요)")


class CertificationStatusResponse(BaseModel):
    """인증 현황 조회 API 최종 응답 data 스키마"""
    year: int = Field(..., description="조회 학년도 (예: 2026)")
    grade: int = Field(..., description="해당 학년도 학생의 학년 (1~3)")
    classNo: int = Field(..., description="해당 학년도 학생의 학급 반 (1~)")
    number: int = Field(..., description="해당 학년도 학생의 번호 (1~)")
    areas: List[AreaStatusItem] = Field(..., description="5대 영역별 세부 인증 현황 리스트")
    totalScore: float = Field(..., description="학생의 총 취득 점수 합계")
    certStatus: str = Field(..., description="종합 역량인증 상태 (인증 가능, 검토중, 보완 필요, 미달성)")


# ==============================================================================
# 3. API 엔드포인트 구현 (Endpoints)
# ==============================================================================

@router.get(
    "/{student_id}/certification-status",
    response_model=SrFormat[CertificationStatusResponse],
    summary="학생 인증 현황 조회 API",
    description=(
        "특정 학생의 학년도별 5대 역량인증 취득 점수, 배점, 성취 등급(S/A/B/미달성), "
        "상벌점 가감 반영 및 종합 인증 판정 결과(인증 가능/검토중/보완 필요)를 계산하여 반환합니다."
    )
)
async def get_certification_status(
    student_id: int,
    year: Optional[int] = Query(None, description="조회할 학사년도 (생략 시 3월 학사력 기준 현재 활성 학년도 자동 적용)"),
    current_user: dict = Depends(get_current_user),
    conn: asyncmy.Connection = Depends(get_db),
):
    """
    [GET] /api/students/{student_id}/certification-status
    
    동작 순서:
    1. 대상 학생 조회 및 존재 여부 확인 (404)
    2. RBAC & 학생 소유권 인가 검증:
       - 학생(role=student): 본인 student_id만 조회 가능 (타인 조회 시 403 FORBIDDEN)
       - 교사(role=teacher) / 관리자(role=admin): 학생 지도 및 심사를 위해 조회 허용 (200 OK)
    3. 학년도 및 academic_years 테이블의 year_id 확인 (404)
    4. 해당 학년도 학생의 학적 정보(grade, class, number) 조회 (404)
    5. 해당 학년도 및 학년(grade)에 설정된 인증 영역(certification_areas) 배점 조회
    6. 학생의 증빙자료 제출 건 조회 (인정완료=3, 심사대기=1,2)
    7. 학생의 반영된 상벌점(merits, is_reflected=True) 조회 (학사년도 필터링)
    8. 항목 산정 유형(최상위인정형 등) 및 영역 배점 상한/하한 보정 연산
    9. FE 표준 JSON 포맷(`SrFormat`) 조립 및 반환
    """
    user_uuid = current_user.get("uuid")
    user_role = current_user.get("role")  # "student", "teacher", "admin"

    async with conn.cursor(cursor=DictCursor) as cur:
        # ----------------------------------------------------------------------
        # 1. 대상 학생 기본 정보 확인
        # ----------------------------------------------------------------------
        await cur.execute(
            """
            SELECT student_id, uuid, name, email, is_deleted
            FROM students
            WHERE student_id = %s AND is_deleted = FALSE
            LIMIT 1
            """,
            (student_id,),
        )
        target_student = await cur.fetchone()

        if not target_student:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=SrFormat(
                    status_code=status.HTTP_404_NOT_FOUND,
                    success=False,
                    data=None,
                    error=Error(
                        code="USER_NOT_FOUND",
                        message="해당 학생을 찾을 수 없습니다.",
                    ),
                ).model_dump(),
            )

        # ----------------------------------------------------------------------
        # 2. RBAC 및 학생 본인 소유권 검증 (Authorization)
        #    - 학생은 다른 학생의 학적 및 점수를 열람할 수 없습니다.
        #    - 교사와 관리자는 담당 업무 수행을 위해 열람이 허용됩니다.
        # ----------------------------------------------------------------------
        if user_role == "student" and target_student["uuid"] != user_uuid:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content=SrFormat(
                    status_code=status.HTTP_403_FORBIDDEN,
                    success=False,
                    data=None,
                    error=Error(
                        code="FORBIDDEN",
                        message="본인의 인증 현황만 조회할 수 있습니다.",
                    ),
                ).model_dump(),
            )

        # ----------------------------------------------------------------------
        # 3. 학년도(Academic Year) 결정 및 year_id 확인
        # ----------------------------------------------------------------------
        query_year = year or await get_current_academic_year()
        year_id = await get_year_id_by_year(conn, query_year)

        if not year_id:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=SrFormat(
                    status_code=status.HTTP_404_NOT_FOUND,
                    success=False,
                    data=None,
                    error=Error(
                        code="ITEM_NOT_FOUND",
                        message=f"{query_year} 학년도 정보가 시스템에 존재하지 않습니다.",
                    ),
                ).model_dump(),
            )

        # ----------------------------------------------------------------------
        # 4. 해당 학년도 학생의 학적 정보(student_academic_records) 조회
        # ----------------------------------------------------------------------
        await cur.execute(
            """
            SELECT grade, class AS class_no, number
            FROM student_academic_records
            WHERE student_id = %s AND year_id = %s
            LIMIT 1
            """,
            (student_id, year_id),
        )
        academic_record = await cur.fetchone()

        if not academic_record:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=SrFormat(
                    status_code=status.HTTP_404_NOT_FOUND,
                    success=False,
                    data=None,
                    error=Error(
                        code="USER_NOT_FOUND",
                        message=f"{query_year} 학년도에 등록된 해당 학생의 학적 정보를 찾을 수 없습니다.",
                    ),
                ).model_dump(),
            )

        st_grade = academic_record["grade"]
        st_class = academic_record["class_no"]
        st_number = academic_record["number"]

        # ----------------------------------------------------------------------
        # 5. 해당 학년도 및 학년(grade)에 배정된 인증 영역(certification_areas) 조회
        # ----------------------------------------------------------------------
        await cur.execute(
            """
            SELECT area_id, name, max_score
            FROM certification_areas
            WHERE year_id = %s AND grade = %s
            ORDER BY area_id ASC
            """,
            (year_id, st_grade),
        )
        areas = await cur.fetchall()

        # [Fallback 정책] 만약 학년별(grade) 배점이 별도 분리되지 않은 기존 데이터베이스라면 grade 조건 없이 조회
        if not areas:
            await cur.execute(
                """
                SELECT area_id, name, max_score
                FROM certification_areas
                WHERE year_id = %s
                ORDER BY area_id ASC
                """,
                (year_id,),
            )
            areas = await cur.fetchall()

        # ----------------------------------------------------------------------
        # 6. 학생이 제출한 증빙자료(submissions) 및 평가 항목 메타데이터 조회
        #    - 상태 코드: 1(제출완료), 2(검토중), 3(인정완료)
        #    - 최상위인정형(scoring_type=8) 및 항목별 최대점 처리를 위해 항목 ID와 점수 산정 방식 함께 조인
        # ----------------------------------------------------------------------
        await cur.execute(
            """
            SELECT 
                s.submission_id,
                s.item_id,
                s.status_code,
                s.granted_score,
                ei.area_id,
                ei.scoring_type,
                ei.max_score AS item_max_score
            FROM submissions s
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            WHERE s.student_id = %s 
              AND s.is_deleted = FALSE
              AND ei.area_id IN (
                  SELECT area_id FROM certification_areas WHERE year_id = %s
              )
            """,
            (student_id, year_id),
        )
        all_submissions = await cur.fetchall()

        # ----------------------------------------------------------------------
        # 7. 해당 학생의 상벌점 중 실제 인증 반영 대상(is_reflected = TRUE) 조회
        # ----------------------------------------------------------------------
        await cur.execute(
            """
            SELECT 
                merits_point_id,
                type,
                points,
                related_area,
                occurred_at
            FROM merits
            WHERE student_id = %s
              AND is_reflected = TRUE
              AND is_deleted = FALSE
            """,
            (student_id,),
        )
        all_merits = await cur.fetchall()

    # ==========================================================================
    # 8. 점수 집계 및 등급 산정 연산 (In-Memory Processing)
    # ==========================================================================

    # 영역별 인정 점수 리스트: area_id -> item_id -> [부여된 점수 목록]
    area_item_scores: Dict[int, Dict[int, List[float]]] = {}
    # 평가 항목 메타데이터: item_id -> {scoring_type, item_max_score}
    area_item_meta: Dict[int, Dict[str, Any]] = {}
    # 영역별 교사 심사 대기(제출완료/검토중) 건수 집계: area_id -> 대기 건수
    area_pending_counts: Dict[int, int] = {}
    has_any_pending = False

    for sub in all_submissions:
        aid = sub["area_id"]
        iid = sub["item_id"]
        st_code = sub["status_code"]
        g_score = float(sub["granted_score"] or 0.0)
        sc_type = sub["scoring_type"]
        item_max = float(sub["item_max_score"]) if sub["item_max_score"] is not None else None

        area_item_meta[iid] = {
            "scoring_type": sc_type,
            "item_max_score": item_max,
        }

        # 상태 코드 1(제출완료) 또는 2(검토중) -> 심사 대기 중으로 분류
        if st_code in (1, 2):
            has_any_pending = True
            area_pending_counts[aid] = area_pending_counts.get(aid, 0) + 1

        # 상태 코드 3(인정완료) -> 합산 대상 점수로 분류
        elif st_code == 3:
            if aid not in area_item_scores:
                area_item_scores[aid] = {}
            if iid not in area_item_scores[aid]:
                area_item_scores[aid][iid] = []
            area_item_scores[aid][iid].append(g_score)

    # --------------------------------------------------------------------------
    # 상벌점 필터링: 발생일 기준 학사년도 판정 (3월~다음해 2월)
    # --------------------------------------------------------------------------
    area_merit_points: Dict[str, float] = {}
    for m in all_merits:
        occ_date = m["occurred_at"]
        if occ_date:
            m_year = occ_date.year if occ_date.month >= 3 else occ_date.year - 1
            if m_year != query_year:
                continue

        m_type = m["type"]  # '+' 또는 '상점' / '-' 또는 '벌점'
        pts = float(m["points"] or 0.0)
        rel_area = (m["related_area"] or "").strip()

        # 벌점인 경우 음수화, 상점인 경우 양수화
        if m_type in ("-", "벌점"):
            pts = -abs(pts)
        else:
            pts = abs(pts)

        if rel_area:
            area_merit_points[rel_area] = area_merit_points.get(rel_area, 0.0) + pts

    # --------------------------------------------------------------------------
    # 9. 영역별 종합 점수 연산 및 결과 조립
    # --------------------------------------------------------------------------
    area_results: List[Dict[str, Any]] = []
    total_score = 0.0

    for area in areas:
        aid = area["area_id"]
        aname = area["name"]
        amax = float(area["max_score"])

        # 각 평가 항목별 점수 집계 (최상위인정형 vs 일반 누적형)
        area_raw_score = 0.0
        items_dict = area_item_scores.get(aid, {})

        for iid, scores in items_dict.items():
            meta = area_item_meta.get(iid, {})
            scoring_type = meta.get("scoring_type")
            item_max_limit = meta.get("item_max_score")

            if scoring_type == 8:  # 8: 최상위인정형 (제출 인정 건 중 최고점 1건만 취합)
                best = max(scores) if scores else 0.0
                if item_max_limit is not None:
                    best = min(best, item_max_limit)
                area_raw_score += best
            else:  # 일반 누적 합산형
                sum_item = sum(scores)
                if item_max_limit is not None:
                    sum_item = min(sum_item, item_max_limit)
                area_raw_score += sum_item

        # 상벌점 가감 반영
        merit_adjustment = area_merit_points.get(aname, 0.0)
        adjusted_score = area_raw_score + merit_adjustment

        # 0점 이상 ~ 영역 최대점 이하로 클램핑(Clamping)
        final_score = max(0.0, min(adjusted_score, amax))
        final_score = round(final_score, 1)

        # 영역 성취 등급 판정 (S, A, B, 미달성)
        grade_str = calculate_area_grade(final_score, amax)

        # 영역 진행 상태 판정 (달성, 검토중, 보완 필요)
        pending_cnt = area_pending_counts.get(aid, 0)
        status_str = calculate_area_status(grade_str, pending_cnt)

        area_results.append({
            "area": aname,
            "score": final_score,
            "maxScore": amax,
            "grade": grade_str,
            "status": status_str,
        })
        total_score += final_score

    # --------------------------------------------------------------------------
    # 10. 종합 역량인증 최종 판정
    # --------------------------------------------------------------------------
    total_score = round(total_score, 1)
    overall_cert_status = calculate_cert_status(area_results, has_any_pending)

    response_data = {
        "year": query_year,
        "grade": st_grade,
        "classNo": st_class,
        "number": st_number,
        "areas": area_results,
        "totalScore": total_score,
        "certStatus": overall_cert_status,
    }

    return SrFormat(
        status_code=status.HTTP_200_OK,
        success=True,
        data=response_data,
        error=None,
    )
