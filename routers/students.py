"""
routers/students.py - 마이스터 역량인증제 학생 업무 관련 API 라우터

명세 및 연동 기준:
- Tech_spec.md 2장 (학생 기능)
- BACKEND_REQUIREMENTS.md 4장 (학생 화면 API), 7장 (점수 계산 및 인증 판정 규칙)
- TODO.md 2.1 (인증 현황 조회 API: GET /api/students/{studentId}/certification-status)
"""

from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import math


try:
    import asyncmy
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    class _AsyncmyStub:
        Connection = Any
    asyncmy = _AsyncmyStub()  # type: ignore
    DictCursor = Any  # type: ignore
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import html
from core.academic import (
    _YEAR_ID_CACHE,
    clear_year_cache,
    get_current_academic_year,
    get_year_id_by_year,
)
from core.authorization import authorize_student_access
from core.calculator import (
    calculate_student_certification,
    calculate_area_grade,
    calculate_area_status,
)
from core.security import get_current_user, require_student
from core.storage import delete_uploaded_file, save_upload_file
from database import get_db, get_redis
from sr_format import Error, SrFormat
import hashlib
import redis.asyncio as aioredis

router = APIRouter(
    prefix="/api/students",
    tags=["students"],
)


# 하위 호환성을 위한 별칭 제공 (기존 참조 호환)
def calculate_cert_status(area_results: List[Dict[str, Any]], has_pending: bool) -> str:
    """마이스터 역량인증제 종합 최종 인증 판정 (FE 7장 명세)"""
    if not area_results:
        return "미달성"
    if all(area.get("grade") in ("S", "A") for area in area_results):
        return "인증 가능"
    if has_pending:
        return "검토중"
    return "보완 필요"


def is_valid_evidence_url(url: Optional[str]) -> bool:
    """외부 증빙 링크의 유효성(HTTP/HTTPS 프로토콜 및 도메인 구조)을 검증합니다."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.netloc.split(":")[0].strip()
        if not host:
            return False
        if host in ("localhost", "127.0.0.1"):
            return True
        if "." not in host or host.startswith(".") or host.endswith("."):
            return False
        return True
    except Exception:
        return False



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
        # 2. 학년도(Academic Year) 결정 및 year_id 확인
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
        # 3. RBAC 및 학생 리소스 인가 검증 (SEC-01)
        # ----------------------------------------------------------------------
        auth_error = await authorize_student_access(
            actor=current_user,
            student_id=student_id,
            conn=conn,
            year=query_year,
            action="read",
        )
        if auth_error:
            return auth_error

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

        # DATA-04: 학년 기준 누락 시 타 학년 데이터로 대체하지 않고 명시적 404 반환
        if not areas:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=SrFormat(
                    status_code=status.HTTP_404_NOT_FOUND,
                    success=False,
                    data=None,
                    error=Error(
                        code="CRITERIA_NOT_CONFIGURED",
                        message=f"{query_year} 학년도 {st_grade}학년 평가 기준이 설정되지 않았습니다.",
                    ),
                ).model_dump(),
            )

        # ----------------------------------------------------------------------
        # 6. 학생이 제출한 증빙자료(submissions) 및 평가 항목 메타데이터 조회
        #    - 상태 코드: 1(제출완료), 2(검토중), 3(인정완료)
        #    - 최상위인정형(scoring_type=8) 및 항목별 최대점 처리를 위해 항목 ID와 점수 산정 방식 함께 조인
        # ----------------------------------------------------------------------
        # [성능 2.2] 서브쿼리 → 명시적 JOIN 변환으로 인덱스 최적화
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
            JOIN certification_areas ca ON ei.area_id = ca.area_id AND ca.year_id = %s
            WHERE s.student_id = %s 
              AND s.is_deleted = FALSE
            """,
            (year_id, student_id),
        )
        all_submissions = await cur.fetchall()

        # ----------------------------------------------------------------------
        # 7. 해당 학생의 상벌점 중 실제 인증 반영 대상(is_reflected = TRUE) 조회
        # ----------------------------------------------------------------------
        # [성능 2.1] 학사년도 날짜 범위를 SQL에서 직접 필터링 (Over-fetching 방지)
        merit_start = date(query_year, 3, 1)
        merit_end = date(query_year + 1, 3, 1)

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
              AND occurred_at >= %s
              AND occurred_at < %s
            """,
            (student_id, merit_start, merit_end),
        )
        all_merits = await cur.fetchall()

    # ==========================================================================
    # 8. 점수 집계 및 등급 산정 연산 (SCORE-01: 공통 계산 엔진 단일화)
    # ==========================================================================
    calc_res = calculate_student_certification(areas, all_submissions, all_merits)

    response_data = {
        "year": query_year,
        "grade": st_grade,
        "classNo": st_class,
        "number": st_number,
        "areas": calc_res["areas"],
        "totalScore": calc_res["totalScore"],
        "certStatus": calc_res["certStatus"],
        "pendingCount": calc_res["pendingCount"],
        "totalMeritScore": calc_res["totalMeritScore"],
    }

    return SrFormat(
        status_code=status.HTTP_200_OK,
        success=True,
        data=response_data,
        error=None,
    )


# ==============================================================================
# 3. 증빙자료 신규 제출 비즈니스 로직 및 엔드포인트 (Submission APIs)
# ==============================================================================

async def handle_submit_evidence(
    conn: asyncmy.Connection,
    current_user: Dict[str, Any],
    student_id_param: Optional[int],
    item_id_val: Optional[int],
    detail_val: Optional[str],
    activity_date_val: Optional[str],
    description_val: Optional[str],
    link_url_val: Optional[str],
    file_val: Optional[UploadFile],
    year_val: Optional[int] = None,
    area_val: Optional[str] = None,
    redis: Optional[aioredis.Redis] = None,
) -> JSONResponse:
    """
    증빙자료 신규 제출 처리 공통 핵심 서비스 로직
    
    연동 명세:
    - Tech_spec.md 2.2 (증빙자료 제출)
    - TODO.md 2.2 (증빙자료 신규 제출 API)
    - SECURITY_AND_AUDIT.md (소유권 검증, 레이스 컨디션/중복 제출 방어, 트랜잭션 및 파일 클린업)
    """
    user_role = current_user.get("role")
    user_uuid = current_user.get("uuid")

    # 1. 호출 주체 권한(RBAC) 검증: 학생(student 또는 role=0)만 제출 가능
    if user_role not in ("student", "0", 0):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=SrFormat(
                status_code=status.HTTP_403_FORBIDDEN,
                success=False,
                data=None,
                error=Error(
                    code="FORBIDDEN",
                    message="학생만 증빙자료를 제출할 수 있습니다.",
                ),
            ).model_dump(),
        )


    async with conn.cursor(cursor=DictCursor) as cur:
        # 2. 대상 학생 식별 및 본인 소유권 검증 (Authorization)
        if student_id_param is not None:
            await cur.execute(
                """
                SELECT student_id, uuid, name, email, is_deleted
                FROM students
                WHERE student_id = %s AND is_deleted = FALSE
                LIMIT 1
                """,
                (student_id_param,),
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

            if target_student["uuid"] != user_uuid:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content=SrFormat(
                        status_code=status.HTTP_403_FORBIDDEN,
                        success=False,
                        data=None,
                        error=Error(
                            code="FORBIDDEN",
                            message="본인의 증빙자료만 제출할 수 있습니다.",
                        ),
                    ).model_dump(),
                )
            student_id = target_student["student_id"]
        else:
            await cur.execute(
                """
                SELECT student_id, uuid, name, email, is_deleted
                FROM students
                WHERE uuid = %s AND is_deleted = FALSE
                LIMIT 1
                """,
                (user_uuid,),
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
                            message="해당 학생 계정의 학적 정보를 찾을 수 없습니다.",
                        ),
                    ).model_dump(),
                )
            student_id = target_student["student_id"]

        # 3. 필수 입력값 기본 유효성 검증
        if item_id_val is None:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message="평가 항목 ID(item_id)는 필수입니다.",
                    ),
                ).model_dump(),
            )

        raw_detail = (detail_val or "").strip()
        if not raw_detail:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message="세부 활동명 또는 자격명(detail)을 입력해주세요.",
                    ),
                ).model_dump(),
            )
        clean_detail = html.escape(raw_detail)
        if len(clean_detail) > 200:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message="세부 활동명(detail)은 최대 200자까지 입력 가능합니다.",
                    ),
                ).model_dump(),
            )

        raw_description = (description_val or "").strip()
        if not raw_description:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message="활동 설명(description)을 입력해주세요.",
                    ),
                ).model_dump(),
            )
        clean_description = html.escape(raw_description)

        if not activity_date_val:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message="활동 일자(activity_date)를 입력해주세요.",
                    ),
                ).model_dump(),
            )

        try:
            parsed_date = datetime.strptime(str(activity_date_val).strip(), "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message="활동 일자 형식이 올바르지 않습니다. (YYYY-MM-DD 형식 필요)",
                    ),
                ).model_dump(),
            )

        if parsed_date > date.today():
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message="활동 일자는 미래 일자일 수 없습니다.",
                    ),
                ).model_dump(),
            )

        clean_link = (link_url_val or "").strip() or None
        if clean_link:
            if not is_valid_evidence_url(clean_link):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=SrFormat(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        success=False,
                        data=None,
                        error=Error(
                            code="VALIDATION_ERROR",
                            message="외부 링크는 http:// 또는 https:// 형식의 올바른 웹 주소(도메인 포함)여야 합니다.",
                        ),
                    ).model_dump(),
                )
            if len(clean_link) > 500:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=SrFormat(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        success=False,
                        data=None,
                        error=Error(
                            code="VALIDATION_ERROR",
                            message="외부 링크는 최대 500자까지 입력 가능합니다.",
                        ),
                    ).model_dump(),
                )


        # 4. 대상 평가 항목(evaluation_items) 및 소속 영역/학년도 검증
        await cur.execute(
            """
            SELECT 
                ei.item_id,
                ei.area_id,
                ei.name AS item_name,
                ei.target_grade,
                ei.max_score AS item_max_score,
                ei.scoring_type,
                ei.requires_evidence,
                ei.is_active,
                ca.year_id,
                ca.grade AS area_grade,
                ca.name AS area_name,
                ay.year AS academic_year,
                ay.is_activated AS year_is_activated
            FROM evaluation_items ei
            JOIN certification_areas ca ON ei.area_id = ca.area_id
            JOIN academic_years ay ON ca.year_id = ay.year_id
            WHERE ei.item_id = %s
            LIMIT 1
            """,
            (item_id_val,),
        )
        eval_item = await cur.fetchone()

        if not eval_item:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=SrFormat(
                    status_code=status.HTTP_404_NOT_FOUND,
                    success=False,
                    data=None,
                    error=Error(
                        code="ITEM_NOT_FOUND",
                        message="해당 평가 항목을 찾을 수 없습니다.",
                    ),
                ).model_dump(),
            )

        if not eval_item["is_active"]:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message="비활성화된 평가 항목에는 증빙자료를 제출할 수 없습니다.",
                    ),
                ).model_dump(),
            )

        # DATA-04: 신규 제출은 활성화된 학년도에만 허용
        if eval_item.get("year_is_activated") is False:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="ACADEMIC_YEAR_INACTIVE",
                        message="현재 활성화된 학년도에만 신규 증빙자료를 제출할 수 있습니다.",
                    ),
                ).model_dump(),
            )

        # 학년도 파라미터(year) 전달 시 평가 항목 학년도와 일치 여부 검증
        if year_val is not None:
            try:
                parsed_year = int(year_val)
                if parsed_year != eval_item["academic_year"]:
                    return JSONResponse(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        content=SrFormat(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            success=False,
                            data=None,
                            error=Error(
                                code="VALIDATION_ERROR",
                                message=f"요청한 학년도({parsed_year})와 평가 항목의 대상 학년도({eval_item['academic_year']})가 일치하지 않습니다.",
                            ),
                        ).model_dump(),
                    )
            except (ValueError, TypeError):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=SrFormat(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        success=False,
                        data=None,
                        error=Error(
                            code="VALIDATION_ERROR",
                            message="학년도(year)는 유효한 정수여야 합니다.",
                        ),
                    ).model_dump(),
                )

        # 인증 영역 파라미터(area) 전달 시 평가 항목 영역과 일치 여부 검증
        if area_val is not None:
            clean_area = str(area_val).strip()
            if clean_area and clean_area != eval_item["area_name"] and clean_area != str(eval_item["area_id"]):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=SrFormat(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        success=False,
                        data=None,
                        error=Error(
                            code="VALIDATION_ERROR",
                            message=f"요청한 인증 영역({clean_area})과 평가 항목의 소속 영역({eval_item['area_name']})이 일치하지 않습니다.",
                        ),
                    ).model_dump(),
                )

        # 5. 학생의 해당 학년도 학적 정보 조회 및 대상 학년 일치 검증

        await cur.execute(
            """
            SELECT grade, class AS class_no, number
            FROM student_academic_records
            WHERE student_id = %s AND year_id = %s
            LIMIT 1
            """,
            (student_id, eval_item["year_id"]),
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
                        message=f"{eval_item['academic_year']} 학년도에 등록된 해당 학생의 학적 정보가 존재하지 않습니다.",
                    ),
                ).model_dump(),
            )

        st_grade = academic_record["grade"]
        area_grade = eval_item["area_grade"]
        target_grade = eval_item["target_grade"]

        if area_grade and area_grade != 0 and area_grade != st_grade:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message=f"해당 평가 항목은 {area_grade}학년 대상입니다. (현재 학생 학년: {st_grade}학년)",
                    ),
                ).model_dump(),
            )

        if target_grade and target_grade != 0 and target_grade != st_grade:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message=f"해당 평가 항목은 {target_grade}학년 대상입니다. (현재 학생 학년: {st_grade}학년)",
                    ),
                ).model_dump(),
            )

        # 6. 증빙자료 필수 제출 요건 검증 (requires_evidence)
        has_file = file_val is not None and bool(file_val.filename and file_val.filename.strip())
        has_link = bool(clean_link)
        requires_evidence = bool(eval_item["requires_evidence"])

        if requires_evidence and not (has_file or has_link):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="VALIDATION_ERROR",
                        message="해당 평가 항목은 증빙자료(첨부파일 또는 외부 링크) 제출이 필수입니다.",
                    ),
                ).model_dump(),
            )

        # 7. 동일 세부활동 및 일자 심사 대기 또는 기인정 건 중복 제출 방어 (DUPLICATE_SUBMISSION)
        await cur.execute(
            """
            SELECT submission_id, status_code
            FROM submissions
            WHERE student_id = %s 
              AND item_id = %s 
              AND detail = %s 
              AND activity_date = %s 
              AND is_deleted = FALSE 
              AND status_code IN (1, 2, 3)
            LIMIT 1
            """,
            (student_id, item_id_val, clean_detail, parsed_date),
        )
        duplicate_sub = await cur.fetchone()

        if duplicate_sub:
            dup_msg = (
                "동일한 세부 활동 및 일자의 증빙자료가 이미 인정(승인) 완료되었습니다."
                if duplicate_sub.get("status_code") == 3
                else "동일한 세부 활동 및 일자의 증빙자료가 이미 심사 대기 중입니다."
            )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=SrFormat(
                    status_code=status.HTTP_409_CONFLICT,
                    success=False,
                    data=None,
                    error=Error(
                        code="DUPLICATE_SUBMISSION",
                        message=dup_msg,
                    ),
                ).model_dump(),
            )


    # [동시성 3.5] TOCTOU 레이스 컨디션 방어: 검증 후 파일 저장 및 DB 삽입 구간 동시 요청 직렬화
    lock_key = f"lock:submission:{student_id}:{item_id_val}:{parsed_date}:{hashlib.sha256(clean_detail.encode('utf-8')).hexdigest()[:16]}"
    lock_acquired = False
    if redis is not None:
        try:
            lock_acquired = await redis.set(lock_key, "1", nx=True, ex=10)
            if not lock_acquired:
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content=SrFormat(
                        status_code=status.HTTP_409_CONFLICT,
                        success=False,
                        data=None,
                        error=Error(
                            code="DUPLICATE_SUBMISSION",
                            message="동일한 증빙자료 제출 요청이 현재 처리 중입니다. 잠시 후 결과를 확인해주세요.",
                        ),
                    ).model_dump(),
                )
        except Exception:
            pass  # Redis 장애 시에도 DB 레벨 트랜잭션 무결성으로 Fallback

    # 8. 첨부파일 저장 처리 (core/storage.py 청크 스트리밍 및 50MB 용량/확장자 검증)
    saved_file_info = None
    saved_disk_path = None
    saved_file_path = None
    original_filename = None

    try:
        if has_file:
            saved_file_info = await save_upload_file(file_val, subfolder="submissions")
            saved_file_path = saved_file_info["file_path"]
            saved_disk_path = saved_file_info["disk_path"]
            original_filename = saved_file_info["original_filename"]

        # 9. 데이터베이스 트랜잭션 수행 (submissions 및 submissions_logs 원자적 기록)
        now = datetime.now()
        try:
            await conn.autocommit(False)
            async with conn.cursor(cursor=DictCursor) as cur:
                await cur.execute(
                    """
                    INSERT INTO submissions (
                        student_id,
                        item_id,
                        detail,
                        activity_date,
                        file_path,
                        original_filename,
                        link_url,
                        description,
                        status_code,
                        granted_score,
                        reviewer_id,
                        reviewed_at,
                        teacher_comment,
                        created_at,
                        is_deleted
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, 1,
                        NULL, NULL, NULL, NULL, %s,
                        FALSE
                    )
                    """,
                    (
                        student_id,
                        item_id_val,
                        clean_detail,
                        parsed_date,
                        saved_file_path,
                        original_filename,
                        clean_link,
                        clean_description,
                        now,
                    ),
                )
                new_submission_id = cur.lastrowid
                if not new_submission_id:
                    await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
                    id_row = await cur.fetchone()
                    new_submission_id = id_row["last_id"] if id_row else 0

                # submissions_logs 초기 제출 이력 기록
                await cur.execute(
                    """
                    INSERT INTO submissions_logs (
                        submission_id,
                        modifier_uuid,
                        action_type,
                        old_status_code,
                        new_status_code,
                        old_score,
                        new_score,
                        comment,
                        created_at
                    ) VALUES (
                        %s, %s, 'SUBMIT', NULL,
                        1, NULL, NULL, '증빙자료 최초 제출', %s
                    )
                    """,
                    (
                        new_submission_id,
                        user_uuid,
                        now,
                    ),
                )

            await conn.commit()
        except Exception as e:
            await conn.rollback()
            # 트랜잭션 실패 시 방금 생성된 디스크 파일 롤백 삭제 (고아 파일 방지)
            if saved_disk_path:
                delete_uploaded_file(saved_disk_path)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content=SrFormat(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    success=False,
                    data=None,
                    error=Error(
                        code="DATABASE_ERROR",
                        message="증빙자료 제출 처리 중 데이터베이스 오류가 발생했습니다.",
                    ),
                ).model_dump(),
            )
        finally:
            await conn.autocommit(True)
    finally:
        if lock_acquired and redis is not None:
            try:
                await redis.delete(lock_key)
            except Exception:
                pass

    # 10. 표준 응답 조립 및 반환 (TODO.md 2.2 / Tech_spec 2.2 규격)
    response_data = {
        "id": new_submission_id,
        "submissionId": new_submission_id,
        "studentId": student_id,
        "year": eval_item["academic_year"],
        "area": eval_item["area_name"],
        "areaId": eval_item["area_id"],
        "itemId": eval_item["item_id"],
        "itemName": eval_item["item_name"],
        "detail": clean_detail,
        "activityDate": str(parsed_date),
        "description": clean_description,
        "filePath": saved_file_path,
        "originalFilename": original_filename,
        "linkUrl": clean_link,
        "status": "제출완료",
        "statusCode": 1,
        "score": None,
        "submittedAt": now.isoformat(),
    }

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=SrFormat(
            status_code=status.HTTP_200_OK,
            success=True,
            data=response_data,
            error=None,
        ).model_dump(),
    )


@router.post(
    "/{student_id}/submissions",
    summary="학생 증빙자료 신규 제출 API (경로 학생 ID 포함)",
    response_model=SrFormat,
)
async def submit_evidence_for_student(
    student_id: int,
    item_id: Optional[int] = Form(None),
    itemId: Optional[int] = Form(None),
    detail: Optional[str] = Form(None),
    activity_date: Optional[str] = Form(None),
    activityDate: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    link_url: Optional[str] = Form(None),
    link: Optional[str] = Form(None),
    year: Optional[int] = Form(None),
    area: Optional[str] = Form(None),
    areaId: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    conn: asyncmy.Connection = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    학생 증빙자료 신규 제출 엔드포인트 (경로 학생 식별자 포함)
    
    엔드포인트: POST /api/students/{studentId}/submissions
    """
    target_item_id = item_id if item_id is not None else itemId
    target_date = activity_date if activity_date is not None else activityDate
    target_link = link_url if link_url is not None else link
    target_area = area if area is not None else areaId

    return await handle_submit_evidence(
        conn=conn,
        current_user=current_user,
        student_id_param=student_id,
        item_id_val=target_item_id,
        detail_val=detail,
        activity_date_val=target_date,
        description_val=description,
        link_url_val=target_link,
        file_val=file,
        year_val=year,
        area_val=target_area,
        redis=redis,
    )


# ==============================================================================
# 5. 제출 내역 및 상벌점 내역 조회 공통 서비스 로직 & 엔드포인트 (TODO 2.3 & 2.5)
# ==============================================================================

SUBMISSION_STATUS_MAP: dict[int, str] = {
    1: "제출완료",
    2: "검토중",
    3: "인정완료",
    4: "반려",
    5: "재제출요청",
}

MERIT_TYPE_MAP: dict[str, str] = {
    "+": "상점",
    "-": "벌점",
    "상점": "상점",
    "벌점": "벌점",
}


async def handle_get_submissions(
    conn: Any,
    current_user: Dict[str, Any],
    student_id_param: Optional[int] = None,
    year_val: Optional[int] = None,
) -> JSONResponse:
    """
    학생 증빙 제출 내역 조회 공통 비즈니스 로직 (TODO.md 2.3)
    """
    user_uuid = current_user.get("uuid")
    user_role = current_user.get("role")
    is_student = (user_role in ("student", "0", 0))

    target_student_id: Optional[int] = None

    async with conn.cursor(cursor=DictCursor) as cur:
        # 1. 학생 권한인 경우 본인 소유권 강제 검증 (IDOR 방어)
        if is_student:
            await cur.execute(
                "SELECT student_id FROM students WHERE uuid = %s AND is_deleted = FALSE LIMIT 1",
                (user_uuid,),
            )
            student_row = await cur.fetchone()
            if not student_row:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content=SrFormat(
                        status_code=status.HTTP_404_NOT_FOUND,
                        success=False,
                        data=None,
                        error=Error(code="USER_NOT_FOUND", message="학생 정보를 찾을 수 없습니다."),
                    ).model_dump(),
                )
            own_student_id = student_row["student_id"]
            if student_id_param is not None and student_id_param != own_student_id:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content=SrFormat(
                        status_code=status.HTTP_403_FORBIDDEN,
                        success=False,
                        data=None,
                        error=Error(code="FORBIDDEN", message="본인의 제출 내역만 조회할 수 있습니다."),
                    ).model_dump(),
                )
            target_student_id = own_student_id
        else:
            # 교사 및 관리자는 studentId 파라미터 필수
            if student_id_param is None:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=SrFormat(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        success=False,
                        data=None,
                        error=Error(code="VALIDATION_ERROR", message="조회 대상 학생 식별자(studentId)가 필요합니다."),
                    ).model_dump(),
                )
            await cur.execute(
                "SELECT student_id FROM students WHERE student_id = %s AND is_deleted = FALSE LIMIT 1",
                (student_id_param,),
            )
            target_student = await cur.fetchone()
            if not target_student:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content=SrFormat(
                        status_code=status.HTTP_404_NOT_FOUND,
                        success=False,
                        data=None,
                        error=Error(code="USER_NOT_FOUND", message="해당 학생을 찾을 수 없습니다."),
                    ).model_dump(),
                )
            target_student_id = student_id_param

        # SEC-01: 공통 인가 계층을 통한 교사 담당 범위 및 역할 검증
        auth_error = await authorize_student_access(
            actor=current_user,
            student_id=target_student_id,
            conn=conn,
            year=year_val,
            action="read",
        )
        if auth_error:
            return auth_error

        # 2. 동적 쿼리 빌드 및 인덱스 기반 정렬 조회
        query_parts = [
            """
            SELECT 
                s.submission_id,
                s.student_id,
                ay.year,
                ca.name AS area_name,
                s.item_id,
                ei.name AS item_name,
                s.detail,
                s.activity_date,
                s.description,
                s.file_path,
                s.original_filename,
                s.link_url,
                s.status_code,
                s.granted_score,
                s.teacher_comment,
                s.created_at,
                s.reviewed_at,
                s.reviewer_id
            FROM submissions s
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id
            JOIN academic_years ay ON ca.year_id = ay.year_id
            WHERE s.student_id = %s
              AND s.is_deleted = FALSE
            """
        ]
        params: list[Any] = [target_student_id]

        if year_val is not None:
            query_parts.append("AND ay.year = %s")
            params.append(year_val)

        # 복합 인덱스(idx_submissions_student_deleted) 활용을 위해 최신순 정렬
        query_parts.append("ORDER BY s.created_at DESC, s.submission_id DESC")
        full_query = " ".join(query_parts)

        await cur.execute(full_query, tuple(params))
        rows = await cur.fetchall()

    # 3. FE 스펙(TODO 2.3 / Tech_spec 2.3) 규격 변환
    results: list[dict[str, Any]] = []
    for r in rows:
        st_code = r["status_code"]
        st_text = SUBMISSION_STATUS_MAP.get(st_code, "제출완료")
        score_val = float(r["granted_score"]) if r["granted_score"] is not None else None
        act_date_str = r["activity_date"].strftime("%Y-%m-%d") if r["activity_date"] else None
        created_str = r["created_at"].isoformat() if r["created_at"] else None
        reviewed_str = r["reviewed_at"].isoformat() if r["reviewed_at"] else None

        results.append({
            "id": r["submission_id"],
            "studentId": r["student_id"],
            "year": r["year"],
            "area": r["area_name"],
            "itemId": r["item_id"],
            "itemName": r["item_name"],
            "detail": r["detail"] or "",
            "activityDate": act_date_str,
            "description": r["description"] or "",
            "filePath": r["file_path"],
            "fileUrl": r["file_path"],  # FE 필드 호환성 보장
            "originalFilename": r["original_filename"],
            "linkUrl": r["link_url"],
            "link": r["link_url"],      # FE 필드 호환성 보장
            "status": st_text,
            "score": score_val,
            "teacherComment": r["teacher_comment"],
            "submittedAt": created_str,
            "reviewedAt": reviewed_str,
            "reviewerId": r["reviewer_id"],
        })

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=SrFormat(
            status_code=status.HTTP_200_OK,
            success=True,
            data=results,
            error=None,
        ).model_dump(),
    )


async def handle_get_points(
    conn: Any,
    current_user: Dict[str, Any],
    student_id_param: Optional[int] = None,
    year_val: Optional[int] = None,
) -> JSONResponse:
    """
    학생 상벌점 내역 조회 공통 비즈니스 로직 (TODO.md 2.5)
    """
    user_uuid = current_user.get("uuid")
    user_role = current_user.get("role")
    is_student = (user_role in ("student", "0", 0))

    target_student_id: Optional[int] = None

    async with conn.cursor(cursor=DictCursor) as cur:
        # 1. 학생 권한인 경우 본인 소유권 강제 검증 (IDOR 방어)
        if is_student:
            await cur.execute(
                "SELECT student_id FROM students WHERE uuid = %s AND is_deleted = FALSE LIMIT 1",
                (user_uuid,),
            )
            student_row = await cur.fetchone()
            if not student_row:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content=SrFormat(
                        status_code=status.HTTP_404_NOT_FOUND,
                        success=False,
                        data=None,
                        error=Error(code="USER_NOT_FOUND", message="학생 정보를 찾을 수 없습니다."),
                    ).model_dump(),
                )
            own_student_id = student_row["student_id"]
            if student_id_param is not None and student_id_param != own_student_id:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content=SrFormat(
                        status_code=status.HTTP_403_FORBIDDEN,
                        success=False,
                        data=None,
                        error=Error(code="FORBIDDEN", message="본인의 상벌점 내역만 조회할 수 있습니다."),
                    ).model_dump(),
                )
            target_student_id = own_student_id
        else:
            if student_id_param is None:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=SrFormat(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        success=False,
                        data=None,
                        error=Error(code="VALIDATION_ERROR", message="조회 대상 학생 식별자(studentId)가 필요합니다."),
                    ).model_dump(),
                )
            await cur.execute(
                "SELECT student_id FROM students WHERE student_id = %s AND is_deleted = FALSE LIMIT 1",
                (student_id_param,),
            )
            target_student = await cur.fetchone()
            if not target_student:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content=SrFormat(
                        status_code=status.HTTP_404_NOT_FOUND,
                        success=False,
                        data=None,
                        error=Error(code="USER_NOT_FOUND", message="해당 학생을 찾을 수 없습니다."),
                    ).model_dump(),
                )
            target_student_id = student_id_param

        # SEC-01: 공통 인가 계층을 통한 교사 담당 범위 및 역할 검증
        auth_error = await authorize_student_access(
            actor=current_user,
            student_id=target_student_id,
            conn=conn,
            year=year_val,
            action="read",
        )
        if auth_error:
            return auth_error

        # 2. 동적 쿼리 빌드 (3월 1일 학사력 기준 인덱스 범위 스캔 적용)
        query_parts = [
            """
            SELECT 
                m.merits_point_id,
                m.student_id,
                m.teachers_id,
                t.name AS teacher_name,
                m.type,
                m.points,
                m.reason,
                m.related_area,
                m.occurred_at,
                m.created_at,
                m.is_reflected
            FROM merits m
            LEFT JOIN teachers t ON m.teachers_id = t.teachers_id
            WHERE m.student_id = %s
              AND m.is_deleted = FALSE
            """
        ]
        params: list[Any] = [target_student_id]

        if year_val is not None:
            # 학사력 기준 범위 [year-03-01, year+1-03-01)
            merit_start = date(year_val, 3, 1)
            merit_end = date(year_val + 1, 3, 1)
            query_parts.append("AND m.occurred_at >= %s AND m.occurred_at < %s")
            params.extend([merit_start, merit_end])

        query_parts.append("ORDER BY m.occurred_at DESC, m.merits_point_id DESC")
        full_query = " ".join(query_parts)

        await cur.execute(full_query, tuple(params))
        rows = await cur.fetchall()

    # 3. FE 스펙(TODO 2.5 / Tech_spec 2.5 / SCORE-02) 규격 변환
    results: list[dict[str, Any]] = []
    for r in rows:
        raw_type = (r["type"] or "-").strip()
        type_str = MERIT_TYPE_MAP.get(raw_type, "벌점" if raw_type in ("-", "벌점") else "상점")
        raw_pts = float(r["points"] or 0.0)
        signed_pts = -abs(raw_pts) if type_str in ("벌점", "-") else abs(raw_pts)
        occ_str = r["occurred_at"].strftime("%Y-%m-%d") if r["occurred_at"] else None
        created_str = r["created_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("created_at") else None
        rec_year = r["occurred_at"].year if r["occurred_at"] else None

        results.append({
            "id": r["merits_point_id"],
            "pointId": r["merits_point_id"],
            "studentId": r["student_id"],
            "teacherId": r["teachers_id"],
            "type": type_str,
            "points": raw_pts,
            "score": signed_pts,  # SCORE-02: 상점 양수 / 벌점 음수 정규화
            "reason": r["reason"] or "",
            "date": occ_str,
            "year": rec_year,
            "createdAt": created_str,
            "teacherName": r["teacher_name"],
            "reflected": bool(r["is_reflected"]),
            "reflectedArea": r["related_area"],
        })

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=SrFormat(
            status_code=status.HTTP_200_OK,
            success=True,
            data=results,
            error=None,
        ).model_dump(),
    )


@router.get(
    "/{student_id}/submissions",
    summary="학생 제출 내역 조회 API (경로 학생 식별자)",
    response_model=SrFormat,
)
async def get_student_submissions(
    student_id: int,
    year: Optional[int] = Query(None, description="학년도 필터"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 제출 내역 조회 엔드포인트 (경로 학생 식별자)
    
    엔드포인트: GET /api/students/{studentId}/submissions
    """
    return await handle_get_submissions(
        conn=conn,
        current_user=current_user,
        student_id_param=student_id,
        year_val=year,
    )


@router.get(
    "/{student_id}/points",
    summary="학생 상벌점 내역 조회 API (경로 학생 식별자)",
    response_model=SrFormat,
)
async def get_student_points(
    student_id: int,
    year: Optional[int] = Query(None, description="학년도 필터"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 상벌점 내역 조회 엔드포인트 (경로 학생 식별자)
    
    엔드포인트: GET /api/students/{studentId}/points
    """
    return await handle_get_points(
        conn=conn,
        current_user=current_user,
        student_id_param=student_id,
        year_val=year,
    )



