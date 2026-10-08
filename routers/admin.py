"""
routers/admin.py - 학년도 관리, 평가 기준 관리, 관리자 통계 개요 API 라우터 (API-03)
"""

import html
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
import redis.asyncio as aioredis

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

from core.academic import clear_year_cache
from core.calculator import calculate_student_certification
from core.excel_parser import (
    parse_homeroom_field,
    parse_student_batch_file,
    parse_teacher_batch_file,
    sanitize_text,
)
from core.neis import (
    explain_neis_student_api_limitation,
    explain_neis_teacher_api_limitation,
    get_classes_for_year,
    get_school_info,
    validate_class_against_neis,
    NEIS_OFFICE_CODE,
    NEIS_OFFICE_NAME,
    NEIS_SCHOOL_CODE,
    NEIS_SCHOOL_NAME,
)
from core.security import get_current_user
from database import get_db, get_redis
from sr_format import Error, SrFormat

# ------------------------------------------------------------------------------
# 라우터 정의
# ------------------------------------------------------------------------------
years_router = APIRouter(prefix="/api/years", tags=["years"])
criteria_router = APIRouter(prefix="/api/criteria", tags=["criteria"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


def check_admin_access(current_user: Dict[str, Any]) -> Optional[JSONResponse]:
    """관리자 권한 필수 검증 의존성 헬퍼"""
    role = current_user.get("role")
    if role not in ("admin", 2, "2"):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=SrFormat(
                status_code=status.HTTP_403_FORBIDDEN,
                success=False,
                data=None,
                error=Error(code="FORBIDDEN", message="관리자 권한이 필요합니다."),
            ).model_dump(),
        )
    return None


# ------------------------------------------------------------------------------
# DTO 스키마
# ------------------------------------------------------------------------------
class CreateYearRequest(BaseModel):
    year: int
    is_activated: Optional[bool] = None
    isActivated: Optional[bool] = None


class PatchYearRequest(BaseModel):
    is_activated: Optional[bool] = None
    isActivated: Optional[bool] = None


class PatchAreaRequest(BaseModel):
    area_id: Optional[int] = None
    areaId: Optional[int] = None
    name: Optional[str] = None
    max_score: Optional[float] = None
    maxScore: Optional[float] = None


class CreateItemRequest(BaseModel):
    name: str
    max_score: Optional[float] = None
    maxScore: Optional[float] = None
    scoring_type: Optional[int] = None
    scoringType: Optional[int] = None
    target_grade: Optional[int] = None
    targetGrade: Optional[int] = None
    requires_evidence: Optional[bool] = None
    requiresEvidence: Optional[bool] = None


class UpdateItemRequest(BaseModel):
    name: Optional[str] = None
    max_score: Optional[float] = None
    maxScore: Optional[float] = None
    scoring_type: Optional[int] = None
    scoringType: Optional[int] = None
    target_grade: Optional[int] = None
    targetGrade: Optional[int] = None
    requires_evidence: Optional[bool] = None
    requiresEvidence: Optional[bool] = None
    is_active: Optional[bool] = None
    isActive: Optional[bool] = None


class BatchStudentItem(BaseModel):
    name: str
    grade: Optional[int] = None
    class_no: Optional[int] = None
    classNo: Optional[int] = None
    student_no: Optional[int] = None
    studentNo: Optional[int] = None
    student_number: Optional[str] = None
    studentNumber: Optional[str] = None
    email: Optional[str] = None


class BatchStudentRequest(BaseModel):
    year: Optional[int] = None
    overwrite: Optional[bool] = False
    validate_with_neis: Optional[bool] = None
    validateWithNeis: Optional[bool] = True
    default_email_domain: Optional[str] = "bssm.hs.kr"
    students: List[BatchStudentItem] = []


class BatchTeacherItem(BaseModel):
    name: str
    email: Optional[str] = None
    subject: Optional[str] = None
    grade: Optional[int] = None
    class_no: Optional[int] = None
    classNo: Optional[int] = None
    homeroom: Optional[str] = None


class BatchTeacherRequest(BaseModel):
    year: Optional[int] = None
    overwrite: Optional[bool] = True
    validate_with_neis: Optional[bool] = None
    validateWithNeis: Optional[bool] = True
    default_email_domain: Optional[str] = "bssm.hs.kr"
    teachers: List[BatchTeacherItem] = []


# ==============================================================================
# 1. 학년도 관리 API (/api/years)
# ==============================================================================

@years_router.get("", summary="전체 학년도 목록 조회 (API-03)", response_model=SrFormat)
async def get_all_years(
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT year_id AS yearId, year, is_activated AS isActivated
            FROM academic_years
            ORDER BY year DESC
            """
        )
        rows = await cur.fetchall()

    for r in rows:
        val = r.get("isActivated") if "isActivated" in r else r.get("is_activated")
        r["isActivated"] = bool(val)

    return SrFormat(status_code=200, success=True, data=rows).model_dump()


@years_router.post("", summary="신규 학년도 등록 (API-03)", response_model=SrFormat)
async def create_year(
    req: CreateYearRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    if req.year < 1900 or req.year > 2100:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="유효하지 않은 학년도입니다 (1900~2100)."),
            ).model_dump(),
        )

    is_act = req.is_activated if req.is_activated is not None else (req.isActivated or False)

    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (req.year,))
            if await cur.fetchone():
                return JSONResponse(
                    status_code=409,
                    content=SrFormat(
                        status_code=409,
                        success=False,
                        data=None,
                        error=Error(code="DUPLICATE_YEAR", message=f"{req.year} 학년도가 이미 등록되어 있습니다."),
                    ).model_dump(),
                )

            # 단일 활성 학년도 상호 배타성 강제 (SECURITY_AND_AUDIT.md 3.9)
            if is_act:
                await cur.execute("UPDATE academic_years SET is_activated = FALSE")

            await cur.execute(
                "INSERT INTO academic_years (year, is_activated) VALUES (%s, %s)",
                (req.year, is_act),
            )
            await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
            last_row = await cur.fetchone()
            year_id = last_row[0] if isinstance(last_row, (list, tuple)) else last_row["last_id"]

        await conn.commit()
        clear_year_cache()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    return JSONResponse(
        status_code=201,
        content=SrFormat(
            status_code=201,
            success=True,
            data={"yearId": year_id, "year": req.year, "isActivated": is_act},
        ).model_dump(),
    )


@years_router.patch("/{year}", summary="학년도 활성화 상태 토글 (API-03)", response_model=SrFormat)
async def toggle_year_active(
    year: int,
    req: PatchYearRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    if year < 1900 or year > 2100:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="유효하지 않은 학년도입니다 (1900~2100)."),
            ).model_dump(),
        )

    is_act = req.is_activated if req.is_activated is not None else (req.isActivated if req.isActivated is not None else True)

    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (year,))
            if not await cur.fetchone():
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="ITEM_NOT_FOUND", message=f"{year} 학년도를 찾을 수 없습니다."),
                    ).model_dump(),
                )

            # 단일 활성 학년도 상호 배타성 강제 (SECURITY_AND_AUDIT.md 3.9)
            if is_act:
                await cur.execute("UPDATE academic_years SET is_activated = FALSE WHERE year != %s", (year,))

            await cur.execute(
                "UPDATE academic_years SET is_activated = %s WHERE year = %s",
                (is_act, year),
            )

        await conn.commit()
        clear_year_cache()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    return SrFormat(
        status_code=200,
        success=True,
        data={"year": year, "isActivated": is_act},
    ).model_dump()


@years_router.post(
    "/{target_year}/copy-from/{source_year}",
    summary="이전 학년도 기준 복제 (API-03)",
    response_model=SrFormat,
)
async def copy_year_criteria(
    target_year: int,
    source_year: int,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    if target_year == source_year:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="동일한 학년도로부터 기준을 복제할 수 없습니다."),
            ).model_dump(),
        )

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (source_year,))
        s_row = await cur.fetchone()
        await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
        t_row = await cur.fetchone()

    if not s_row:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="ITEM_NOT_FOUND", message=f"복제 원본 {source_year} 학년도가 존재하지 않습니다."),
            ).model_dump(),
        )
    if not t_row:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="ITEM_NOT_FOUND", message=f"복제 대상 {target_year} 학년도가 존재하지 않습니다."),
            ).model_dump(),
        )

    s_year_id = s_row["year_id"]
    t_year_id = t_row["year_id"]

    async with conn.cursor(cursor=DictCursor) as cur:
        # [무결성 3.10] 복제 대상 학년도에 이미 기준이 구성되어 있는 경우 중복 증식 방어 (409 CONFLICT)
        await cur.execute(
            "SELECT COUNT(*) AS cnt FROM certification_areas WHERE year_id = %s",
            (t_year_id,),
        )
        t_cnt_row = await cur.fetchone()
        t_cnt = (t_cnt_row.get("cnt") if t_cnt_row else 0) or 0
        if t_cnt > 0:
            return JSONResponse(
                status_code=409,
                content=SrFormat(
                    status_code=409,
                    success=False,
                    data=None,
                    error=Error(code="CONFLICT", message=f"{target_year} 학년도에 이미 평가 기준이 설정되어 있습니다."),
                ).model_dump(),
            )

        await cur.execute(
            """
            SELECT area_id, grade, name, max_score
            FROM certification_areas
            WHERE year_id = %s
            """,
            (s_year_id,),
        )
        source_areas = await cur.fetchall()

    if not source_areas:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="CRITERIA_NOT_CONFIGURED", message=f"{source_year} 학년도에 설정된 평가 기준이 없습니다."),
            ).model_dump(),
        )

    copied_areas_count = 0
    copied_items_count = 0

    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            for s_area in source_areas:
                await cur.execute(
                    """
                    INSERT INTO certification_areas (year_id, grade, name, max_score)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE max_score = VALUES(max_score)
                    """,
                    (t_year_id, s_area["grade"], s_area["name"], s_area["max_score"]),
                )
                await cur.execute(
                    "SELECT area_id FROM certification_areas WHERE year_id = %s AND grade = %s AND name = %s LIMIT 1",
                    (t_year_id, s_area["grade"], s_area["name"]),
                )
                t_area_row = await cur.fetchone()
                t_area_id = t_area_row["area_id"]
                copied_areas_count += 1

                # 해당 영역의 항목 복제 (벌크 배치 최적화: SECURITY_AND_AUDIT.md 2.9)
                await cur.execute(
                    """
                    SELECT name, max_score, scoring_type, target_grade, requires_evidence
                    FROM evaluation_items
                    WHERE area_id = %s AND is_active = TRUE
                    """,
                    (s_area["area_id"],),
                )
                source_items = await cur.fetchall()

                if source_items:
                    items_params = [
                        (
                            t_area_id,
                            s_item["name"],
                            s_item["max_score"],
                            s_item["scoring_type"],
                            s_item["target_grade"],
                            s_item["requires_evidence"],
                        )
                        for s_item in source_items
                    ]
                    insert_sql = """
                        INSERT INTO evaluation_items (
                            area_id, name, max_score, scoring_type, target_grade, requires_evidence, is_active
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    """
                    if hasattr(cur, "executemany"):
                        await cur.executemany(insert_sql, items_params)
                    else:
                        for p in items_params:
                            await cur.execute(insert_sql, p)
                    copied_items_count += len(source_items)

        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "sourceYear": source_year,
            "targetYear": target_year,
            "copiedAreas": copied_areas_count,
            "copiedItems": copied_items_count,
        },
    ).model_dump()


# ==============================================================================
# 2. 평가 기준 관리 API (/api/criteria)
# ==============================================================================

@criteria_router.get("", summary="평가 기준 조회 (영역 및 항목) (API-03)", response_model=SrFormat)
async def get_criteria(
    year: Optional[int] = Query(None, description="조회 학년도 (미지정 시 활성/최신 학년도)"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    target_year = year
    async with conn.cursor(cursor=DictCursor) as cur:
        if target_year is None:
            await cur.execute(
                "SELECT year, year_id FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1"
            )
            y_row = await cur.fetchone()
            if y_row:
                target_year = y_row["year"]
                year_id = y_row["year_id"]
            else:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="ITEM_NOT_FOUND", message="등록된 학년도가 없습니다."),
                    ).model_dump(),
                )
        else:
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="ITEM_NOT_FOUND", message=f"{target_year} 학년도가 존재하지 않습니다."),
                    ).model_dump(),
                )
            year_id = y_row["year_id"]

        # 영역 목록 조회
        await cur.execute(
            """
            SELECT area_id AS areaId, year_id AS yearId, grade, name, max_score AS maxScore
            FROM certification_areas
            WHERE year_id = %s
            ORDER BY grade ASC, area_id ASC
            """,
            (year_id,),
        )
        areas = await cur.fetchall()

        if not areas:
            return SrFormat(
                status_code=200,
                success=True,
                data={"year": target_year, "yearId": year_id, "areas": []},
            ).model_dump()

        norm_areas = []
        for a in areas:
            a_id = a.get("areaId") or a.get("area_id")
            norm_areas.append({
                "areaId": a_id,
                "yearId": a.get("yearId") or a.get("year_id"),
                "grade": a.get("grade"),
                "name": a.get("name"),
                "maxScore": float(a.get("maxScore") if a.get("maxScore") is not None else a.get("max_score", 0.0)),
            })

        area_ids = [a["areaId"] for a in norm_areas]
        placeholders = ", ".join(["%s"] * len(area_ids))

        await cur.execute(
            f"""
            SELECT 
                item_id AS itemId,
                area_id AS areaId,
                name,
                max_score AS maxScore,
                scoring_type AS scoringType,
                target_grade AS targetGrade,
                requires_evidence AS requiresEvidence,
                is_active AS isActive
            FROM evaluation_items
            WHERE area_id IN ({placeholders}) AND is_active = TRUE
            ORDER BY item_id ASC
            """,
            tuple(area_ids),
        )
        items = await cur.fetchall()

    items_by_area: Dict[int, List[Dict[str, Any]]] = {}
    for it in items:
        it_area_id = it.get("areaId") or it.get("area_id")
        norm_it = {
            "itemId": it.get("itemId") or it.get("item_id"),
            "areaId": it_area_id,
            "name": it.get("name"),
            "maxScore": float(it.get("maxScore") if it.get("maxScore") is not None else it.get("max_score", 0.0)),
            "scoringType": it.get("scoringType") or it.get("scoring_type"),
            "targetGrade": it.get("targetGrade") or it.get("target_grade"),
            "requiresEvidence": bool(it.get("requiresEvidence") if "requiresEvidence" in it else it.get("requires_evidence", False)),
            "isActive": bool(it.get("isActive") if "isActive" in it else it.get("is_active", True)),
        }
        items_by_area.setdefault(it_area_id, []).append(norm_it)

    for a in norm_areas:
        a["items"] = items_by_area.get(a["areaId"], [])

    return SrFormat(
        status_code=200,
        success=True,
        data={"year": target_year, "yearId": year_id, "areas": norm_areas},
    ).model_dump()


@criteria_router.patch("/areas/{area_id}", summary="평가 영역 배점/정보 수정 (API-03)", response_model=SrFormat)
async def patch_criteria_area(
    area_id: int,
    req: PatchAreaRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    new_score = req.max_score if req.max_score is not None else req.maxScore
    if new_score is not None and (new_score <= 0 or new_score > 1000):
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="영역 배점은 0보다 크고 1000 이하여야 합니다."),
            ).model_dump(),
        )

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT area_id, name, max_score FROM certification_areas WHERE area_id = %s LIMIT 1", (area_id,))
        existing = await cur.fetchone()
        if not existing:
            return JSONResponse(
                status_code=404,
                content=SrFormat(
                    status_code=404,
                    success=False,
                    data=None,
                    error=Error(code="ITEM_NOT_FOUND", message="해당 영역을 찾을 수 없습니다."),
                ).model_dump(),
            )

        if req.name is not None:
            raw_name = req.name.strip()
            if not raw_name:
                return JSONResponse(
                    status_code=400,
                    content=SrFormat(
                        status_code=400,
                        success=False,
                        data=None,
                        error=Error(code="VALIDATION_ERROR", message="영역 이름은 비어 있을 수 없습니다."),
                    ).model_dump(),
                )
            updated_name = html.escape(raw_name)
            if len(updated_name) > 30:
                return JSONResponse(
                    status_code=400,
                    content=SrFormat(
                        status_code=400,
                        success=False,
                        data=None,
                        error=Error(code="VALIDATION_ERROR", message="영역 이름은 최대 30자까지 가능합니다."),
                    ).model_dump(),
                )
        else:
            updated_name = existing["name"]

        updated_score = new_score if new_score is not None else float(existing["max_score"])

        await cur.execute(
            "UPDATE certification_areas SET name = %s, max_score = %s WHERE area_id = %s",
            (updated_name, updated_score, area_id),
        )

    return SrFormat(
        status_code=200,
        success=True,
        data={"areaId": area_id, "name": updated_name, "maxScore": updated_score},
    ).model_dump()


@criteria_router.post("/areas/{area_id}/items", summary="평가 항목 추가 (API-03)", response_model=SrFormat)
async def create_criteria_item(
    area_id: int,
    req: CreateItemRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    max_score = req.max_score if req.max_score is not None else (req.maxScore if req.maxScore is not None else 10.0)
    if max_score <= 0 or max_score > 1000:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="항목 배점은 0보다 크고 1000 이하여야 합니다."),
            ).model_dump(),
        )

    scoring_type = req.scoring_type if req.scoring_type is not None else (req.scoringType or 1)
    # [버그 3.11] targetGrade: 0 (전학년 공통) Falsy 연산 왜곡 방어
    target_grade = req.target_grade if req.target_grade is not None else (req.targetGrade if req.targetGrade is not None else 0)
    requires_ev = req.requires_evidence if req.requires_evidence is not None else (req.requiresEvidence if req.requiresEvidence is not None else True)
    raw_name = (req.name or "").strip()
    if not raw_name:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="항목 이름은 필수입니다."),
            ).model_dump(),
        )
    if len(raw_name) > 50:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="항목 이름은 최대 50자까지 가능합니다."),
            ).model_dump(),
        )
    clean_name = html.escape(raw_name)
    if len(clean_name) > 100:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="항목 이름이 허용 길이를 초과했습니다."),
            ).model_dump(),
        )

    if target_grade not in (0, 1, 2, 3):
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="대상 학년은 0(전학년), 1, 2, 3 중 하나여야 합니다."),
            ).model_dump(),
        )

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT area_id FROM certification_areas WHERE area_id = %s LIMIT 1", (area_id,))
        if not await cur.fetchone():
            return JSONResponse(
                status_code=404,
                content=SrFormat(
                    status_code=404,
                    success=False,
                    data=None,
                    error=Error(code="ITEM_NOT_FOUND", message="해당 영역을 찾을 수 없습니다."),
                ).model_dump(),
            )

        await cur.execute(
            """
            INSERT INTO evaluation_items (
                area_id, name, max_score, scoring_type, target_grade, requires_evidence, is_active
            )
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            """,
            (area_id, clean_name, max_score, scoring_type, target_grade, requires_ev),
        )
        await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
        last_row = await cur.fetchone()
        item_id = last_row[0] if isinstance(last_row, (list, tuple)) else last_row["last_id"]

    return JSONResponse(
        status_code=201,
        content=SrFormat(
            status_code=201,
            success=True,
            data={
                "itemId": item_id,
                "areaId": area_id,
                "name": clean_name,
                "maxScore": max_score,
                "scoringType": scoring_type,
                "targetGrade": target_grade,
                "requiresEvidence": requires_ev,
            },
        ).model_dump(),
    )


@criteria_router.patch("/items/{item_id}", summary="평가 항목 수정 (API-03)", response_model=SrFormat)
async def update_criteria_item(
    item_id: int,
    req: UpdateItemRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT * FROM evaluation_items WHERE item_id = %s LIMIT 1", (item_id,))
        existing = await cur.fetchone()
        if not existing:
            return JSONResponse(
                status_code=404,
                content=SrFormat(
                    status_code=404,
                    success=False,
                    data=None,
                    error=Error(code="ITEM_NOT_FOUND", message="해당 평가 항목을 찾을 수 없습니다."),
                ).model_dump(),
            )

        new_max_score = req.max_score if req.max_score is not None else (req.maxScore if req.maxScore is not None else float(existing["max_score"]))
        if new_max_score <= 0 or new_max_score > 1000:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="VALIDATION_ERROR", message="항목 배점은 0보다 크고 1000 이하여야 합니다."),
                ).model_dump(),
            )

        if req.name is not None:
            raw_name = req.name.strip()
            if not raw_name:
                return JSONResponse(
                    status_code=400,
                    content=SrFormat(
                        status_code=400,
                        success=False,
                        data=None,
                        error=Error(code="VALIDATION_ERROR", message="항목 이름은 비어 있을 수 없습니다."),
                    ).model_dump(),
                )
            if len(raw_name) > 50:
                return JSONResponse(
                    status_code=400,
                    content=SrFormat(
                        status_code=400,
                        success=False,
                        data=None,
                        error=Error(code="VALIDATION_ERROR", message="항목 이름은 최대 50자까지 가능합니다."),
                    ).model_dump(),
                )
            new_name = html.escape(raw_name)
            if len(new_name) > 100:
                return JSONResponse(
                    status_code=400,
                    content=SrFormat(
                        status_code=400,
                        success=False,
                        data=None,
                        error=Error(code="VALIDATION_ERROR", message="항목 이름이 허용 길이를 초과했습니다."),
                    ).model_dump(),
                )
        else:
            new_name = existing["name"]

        new_type = req.scoring_type if req.scoring_type is not None else (req.scoringType if req.scoringType is not None else existing["scoring_type"])
        new_grade = req.target_grade if req.target_grade is not None else (req.targetGrade if req.targetGrade is not None else existing["target_grade"])
        if new_grade is not None and new_grade not in (0, 1, 2, 3):
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="VALIDATION_ERROR", message="대상 학년은 0(전학년), 1, 2, 3 중 하나여야 합니다."),
                ).model_dump(),
            )
        new_req_ev = req.requires_evidence if req.requires_evidence is not None else (req.requiresEvidence if req.requiresEvidence is not None else existing["requires_evidence"])
        new_active = req.is_active if req.is_active is not None else (req.isActive if req.isActive is not None else existing["is_active"])

        await cur.execute(
            """
            UPDATE evaluation_items
            SET name = %s,
                max_score = %s,
                scoring_type = %s,
                target_grade = %s,
                requires_evidence = %s,
                is_active = %s
            WHERE item_id = %s
            """,
            (new_name, new_max_score, new_type, new_grade, new_req_ev, new_active, item_id),
        )

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "itemId": item_id,
            "name": new_name,
            "maxScore": new_max_score,
            "scoringType": new_type,
            "targetGrade": new_grade,
            "requiresEvidence": bool(new_req_ev),
            "isActive": bool(new_active),
        },
    ).model_dump()


@criteria_router.delete("/items/{item_id}", summary="평가 항목 삭제 (소프트 삭제) (API-03)", response_model=SrFormat)
async def delete_criteria_item(
    item_id: int,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("UPDATE evaluation_items SET is_active = FALSE WHERE item_id = %s", (item_id,))
        if cur.rowcount == 0:
            return JSONResponse(
                status_code=404,
                content=SrFormat(
                    status_code=404,
                    success=False,
                    data=None,
                    error=Error(code="ITEM_NOT_FOUND", message="해당 평가 항목을 찾을 수 없습니다."),
                ).model_dump(),
            )

    return SrFormat(
        status_code=200,
        success=True,
        data={"itemId": item_id, "deleted": True},
    ).model_dump()


# ==============================================================================
# 3. 관리자 전체 현황 대시보드 API (/api/admin/overview)
# ==============================================================================

@admin_router.get("/overview", summary="관리자 전체 현황 대시보드 통계 API (API-03)", response_model=SrFormat)
async def get_admin_overview(
    year: Optional[int] = Query(None, description="통계 학년도"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    관리자 전용 전체 학교 인증 및 제출 현황 통계 (API-03)
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    async with conn.cursor(cursor=DictCursor) as cur:
        if year is None:
            await cur.execute("SELECT year, year_id FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            y_row = await cur.fetchone()
            if not y_row:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="ITEM_NOT_FOUND", message="등록된 학년도가 없습니다."),
                    ).model_dump(),
                )
            target_year = y_row["year"]
            year_id = y_row["year_id"]
        else:
            target_year = year
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="ITEM_NOT_FOUND", message=f"{target_year} 학년도가 존재하지 않습니다."),
                    ).model_dump(),
                )
            year_id = y_row["year_id"]

        # 1. 전교생 수
        await cur.execute(
            """
            SELECT COUNT(DISTINCT sar.student_id) AS total_students
            FROM student_academic_records sar
            JOIN students st ON sar.student_id = st.student_id AND st.is_deleted = FALSE
            WHERE sar.year_id = %s
            """,
            (year_id,),
        )
        st_count_row = await cur.fetchone()
        total_students = st_count_row.get("total_students", 0) if st_count_row else 0

        # 2. 제출 상태별 카운트 (소프트 삭제 학생 제출물 제외: SECURITY_AND_AUDIT.md 2.14)
        await cur.execute(
            """
            SELECT 
                COUNT(*) AS total_submissions,
                COUNT(CASE WHEN s.status_code = 1 THEN 1 END) AS submitted_count,
                COUNT(CASE WHEN s.status_code = 2 THEN 1 END) AS reviewing_count,
                COUNT(CASE WHEN s.status_code = 3 THEN 1 END) AS approved_count,
                COUNT(CASE WHEN s.status_code = 4 THEN 1 END) AS rejected_count,
                COUNT(CASE WHEN s.status_code = 5 THEN 1 END) AS resubmit_count
            FROM submissions s
            JOIN students st ON s.student_id = st.student_id AND st.is_deleted = FALSE
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id AND ca.year_id = %s
            WHERE s.is_deleted = FALSE
            """,
            (year_id,),
        )
        sub_stats = await cur.fetchone() or {}

        # 3. 인증 영역별 제출 수 통계 (소프트 삭제 학생 제출물 제외)
        await cur.execute(
            """
            SELECT ca.area_id AS areaId, ca.name AS areaName, COUNT(st.student_id) AS submissionCount
            FROM certification_areas ca
            LEFT JOIN evaluation_items ei ON ca.area_id = ei.area_id
            LEFT JOIN submissions s ON ei.item_id = s.item_id AND s.is_deleted = FALSE
            LEFT JOIN students st ON s.student_id = st.student_id AND st.is_deleted = FALSE
            WHERE ca.year_id = %s
            GROUP BY ca.area_id, ca.name
            ORDER BY ca.area_id ASC
            """,
            (year_id,),
        )
        area_distribution = await cur.fetchall()

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "year": target_year,
            "yearId": year_id,
            "totalStudents": total_students,
            "submissions": {
                "total": sub_stats.get("total_submissions", 0),
                "pending": (sub_stats.get("submitted_count", 0) or 0) + (sub_stats.get("reviewing_count", 0) or 0),
                "approved": sub_stats.get("approved_count", 0) or 0,
                "rejected": sub_stats.get("rejected_count", 0) or 0,
                "resubmitRequested": sub_stats.get("resubmit_count", 0) or 0,
            },
            "areaDistribution": area_distribution,
        },
    ).model_dump()


# ==============================================================================
# 4. 학생 명단 엑셀/CSV 일괄 등록 & NEIS 연동 API (4.6)
# ==============================================================================

@admin_router.get("/neis/info", summary="NEIS 연동 정책 및 학생 명단 API 미제공 법적 근거 안내 (4.6)", response_model=SrFormat)
async def get_neis_integration_info(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    NEIS Open API 연동 정보 및 학생 명단 직접 조회 API 미제공에 대한 구체적 법적/기술적 근거를 반환합니다.
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    return SrFormat(
        status_code=200,
        success=True,
        data=explain_neis_student_api_limitation(),
    ).model_dump()


@admin_router.get("/neis/school-info", summary="부산소프트웨어마이스터고 NEIS 학교 기본정보 조회 (4.6)", response_model=SrFormat)
async def get_bssm_neis_school_info(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    NEIS Open API에서 부산소프트웨어마이스터고등학교 공식 학교 기본정보를 조회합니다.
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    info = await get_school_info()
    if not info:
        return JSONResponse(
            status_code=502,
            content=SrFormat(
                status_code=502,
                success=False,
                data=None,
                error=Error(code="NEIS_API_ERROR", message="NEIS Open API로부터 학교 정보를 조회할 수 없습니다."),
            ).model_dump(),
        )

    return SrFormat(status_code=200, success=True, data=info).model_dump()


@admin_router.get("/neis/classes", summary="부산소프트웨어마이스터고 NEIS 학년도별 개설 학급 목록 조회 (4.6)", response_model=SrFormat)
async def get_bssm_neis_classes(
    year: Optional[int] = Query(None, description="조회 학년도 (미지정 시 활성 학년도)"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    NEIS Open API에서 부산소프트웨어마이스터고등학교의 해당 학년도 인가 학급 목록을 동적 조회합니다.
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    target_year = year
    if target_year is None:
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("SELECT year FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            row = await cur.fetchone()
            target_year = row["year"] if row else 2026

    classes = await get_classes_for_year(target_year)
    return SrFormat(
        status_code=200,
        success=True,
        data={
            "schoolName": NEIS_SCHOOL_NAME,
            "schoolCode": NEIS_SCHOOL_CODE,
            "officeCode": NEIS_OFFICE_CODE,
            "year": target_year,
            "classCount": len(classes),
            "classes": classes,
        },
    ).model_dump()


@admin_router.post("/students/batch", summary="학생 명단 엑셀/CSV 일괄 등록 (4.6)", response_model=SrFormat)
@admin_router.post("/students/batch/neis", summary="NEIS 표준 양식 학생 명단 일괄 등록 (4.6)", response_model=SrFormat)
async def batch_register_students(
    request: Request,
    file: Optional[UploadFile] = File(None),
    year: Optional[int] = Form(None),
    overwrite: Optional[bool] = Form(False),
    validate_with_neis: Optional[bool] = Form(None),
    default_email_domain: Optional[str] = Form("bssm.hs.kr"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 명단 엑셀(XLSX) 또는 CSV 파일 일괄 등록 엔드포인트 (TODO.md 4.6)
    - 관리자 전용
    - XLSX / CSV 파일 자동 감지 및 멀티 인코딩(UTF-8, CP949, EUC-KR) 파싱
    - 수식 인젝션(Formula Injection) 방어 및 XSS 방어
    - 부산소프트웨어마이스터고 NEIS 학급 정보 실시간 교차 검증 (validate_with_neis)
    - 학생 계정(students) 및 학년도별 학적(student_academic_records) 단일 트랜잭션 원자적 등록
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    content_type = request.headers.get("content-type", "").lower()
    query_params = request.query_params

    # 1. 쿼리/폼/JSON 파라미터 우선순위 정규화
    target_year = year if year is not None else (int(query_params.get("year")) if query_params.get("year") else None)
    is_overwrite = bool(overwrite if overwrite is not None else query_params.get("overwrite", False))
    should_validate_neis = validate_with_neis if validate_with_neis is not None else True
    email_domain = default_email_domain or query_params.get("default_email_domain", "bssm.hs.kr")

    valid_records: List[Dict[str, Any]] = []
    parsing_errors: List[Dict[str, Any]] = []

    # 2. 파일 업로드 또는 JSON 바디 분기 처리
    if file is not None:
        file_bytes = await file.read()
        if len(file_bytes) > 10 * 1024 * 1024:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="FILE_TOO_LARGE", message="파일 크기는 최대 10MB까지 허용됩니다."),
                ).model_dump(),
            )
        try:
            valid_records, parsing_errors = parse_student_batch_file(
                file_bytes=file_bytes,
                filename=file.filename or "students.xlsx",
                default_email_domain=email_domain,
            )
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="FILE_PARSE_ERROR", message=f"파일 파싱 실패: {str(e)}"),
                ).model_dump(),
            )
    elif "application/json" in content_type:
        try:
            body = await request.json()
            if "year" in body and target_year is None:
                target_year = int(body["year"])
            if "overwrite" in body:
                is_overwrite = bool(body["overwrite"])
            if "validateWithNeis" in body:
                should_validate_neis = bool(body["validateWithNeis"])
            elif "validate_with_neis" in body:
                should_validate_neis = bool(body["validate_with_neis"])
            if "default_email_domain" in body:
                email_domain = body["default_email_domain"]

            raw_students = body.get("students", [])
            from core.excel_parser import parse_student_number_field, sanitize_text, EMAIL_REGEX

            seen_students = set()
            seen_emails = set()

            for idx, item in enumerate(raw_students, start=1):
                clean_name = sanitize_text(item.get("name"))
                if not clean_name:
                    parsing_errors.append({"row": idx, "name": "", "reason": "학생 성명이 누락되었습니다."})
                    continue
                if len(clean_name) > 20:
                    parsing_errors.append({"row": idx, "name": clean_name, "reason": "학생 성명은 20자를 초과할 수 없습니다."})
                    continue
                name = html.escape(clean_name)

                grade = item.get("grade")
                class_no = item.get("class_no") if item.get("class_no") is not None else item.get("classNo")
                student_no = item.get("student_no") if item.get("student_no") is not None else item.get("studentNo")

                snum = item.get("student_number") or item.get("studentNumber")
                if snum:
                    g, c, n = parse_student_number_field(snum)
                    if g and c and n:
                        grade, class_no, student_no = g, c, n

                if grade is None or not (1 <= grade <= 3):
                    parsing_errors.append({"row": idx, "name": name, "reason": f"유효하지 않은 학년입니다 ({grade})."})
                    continue
                if class_no is None or not (1 <= class_no <= 20):
                    parsing_errors.append({"row": idx, "name": name, "reason": f"유효하지 않은 반입니다 ({class_no})."})
                    continue
                if student_no is None or not (1 <= student_no <= 50):
                    parsing_errors.append({"row": idx, "name": name, "reason": f"유효하지 않은 번호입니다 ({student_no})."})
                    continue

                email = (item.get("email") or "").strip().lower()
                if email:
                    if not EMAIL_REGEX.match(email):
                        parsing_errors.append({"row": idx, "name": name, "reason": f"올바르지 않은 이메일 형식입니다: {email}"})
                        continue
                else:
                    email = f"s{grade}{class_no:02d}{student_no:02d}@{email_domain}"

                st_key = (grade, class_no, student_no)
                if st_key in seen_students:
                    parsing_errors.append({"row": idx, "name": name, "reason": f"중복된 학년/반/번호 ({grade}-{class_no}-{student_no})"})
                    continue
                seen_students.add(st_key)

                if email in seen_emails:
                    parsing_errors.append({"row": idx, "name": name, "reason": f"중복된 이메일 ({email})"})
                    continue
                seen_emails.add(email)

                valid_records.append({
                    "row_number": idx,
                    "name": name,
                    "grade": grade,
                    "class_no": class_no,
                    "student_no": student_no,
                    "email": email,
                })
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="VALIDATION_ERROR", message=f"JSON 바디 파싱 실패: {str(e)}"),
                ).model_dump(),
            )
    else:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(
                    code="VALIDATION_ERROR",
                    message="업로드할 엑셀/CSV 파일(file) 또는 학생 명단 JSON(students)이 필요합니다.",
                ),
            ).model_dump(),
        )

    # 3. 대상 학년도(year_id) 확인
    async with conn.cursor(cursor=DictCursor) as cur:
        if target_year is None:
            await cur.execute("SELECT year_id, year FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            y_row = await cur.fetchone()
            if not y_row:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="ITEM_NOT_FOUND", message="등록된 학년도가 없습니다. 먼저 학년도를 등록해주세요."),
                    ).model_dump(),
                )
            target_year_id = y_row["year_id"]
            target_year = y_row["year"]
        else:
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="ITEM_NOT_FOUND", message=f"{target_year} 학년도가 시스템에 존재하지 않습니다."),
                    ).model_dump(),
                )
            target_year_id = y_row["year_id"]

    # 4. NEIS 학급 실시간 교차 검증 (validate_with_neis = True인 경우)
    neis_validated_classes: Dict[Tuple[int, int], bool] = {}
    final_to_insert: List[Dict[str, Any]] = []

    if should_validate_neis and valid_records:
        distinct_classes = set((st["grade"], st["class_no"]) for st in valid_records)
        for g, c in distinct_classes:
            is_valid, err_msg = await validate_class_against_neis(target_year, g, c)
            neis_validated_classes[(g, c)] = is_valid
            if not is_valid:
                for st in valid_records:
                    if st["grade"] == g and st["class_no"] == c:
                        parsing_errors.append({
                            "row": st.get("row_number", 0),
                            "name": st["name"],
                            "reason": err_msg or f"NEIS 미인가 학급 ({g}학년 {c}반)",
                        })

        for st in valid_records:
            if neis_validated_classes.get((st["grade"], st["class_no"]), True):
                final_to_insert.append(st)
    else:
        final_to_insert = valid_records

    # 5. DB 트랜잭션 원자적 일괄 등록
    created_count = 0
    updated_count = 0
    skipped_count = 0

    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            for st in final_to_insert:
                # 1) students 테이블 조회/생성
                await cur.execute(
                    "SELECT student_id, name, is_deleted FROM students WHERE email = %s LIMIT 1",
                    (st["email"],),
                )
                ex_student = await cur.fetchone()

                if ex_student:
                    student_id = ex_student["student_id"]
                    if is_overwrite and ex_student["name"] != st["name"]:
                        await cur.execute(
                            "UPDATE students SET name = %s, is_deleted = FALSE WHERE student_id = %s",
                            (st["name"], student_id),
                        )
                else:
                    await cur.execute(
                        "INSERT INTO students (name, email, status, is_deleted) VALUES (%s, %s, 0, FALSE)",
                        (st["name"], st["email"]),
                    )
                    await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
                    last_id_row = await cur.fetchone()
                    student_id = last_id_row["last_id"] if isinstance(last_id_row, dict) else last_id_row[0]
                    created_count += 1

                # 2) student_academic_records 테이블 조회/생성/갱신
                await cur.execute(
                    """
                    SELECT record_id, grade, class, number
                    FROM student_academic_records
                    WHERE student_id = %s AND year_id = %s
                    LIMIT 1
                    """,
                    (student_id, target_year_id),
                )
                ex_record = await cur.fetchone()

                if ex_record:
                    if is_overwrite:
                        await cur.execute(
                            """
                            UPDATE student_academic_records
                            SET grade = %s, class = %s, number = %s
                            WHERE record_id = %s
                            """,
                            (st["grade"], st["class_no"], st["student_no"], ex_record["record_id"]),
                        )
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    await cur.execute(
                        """
                        INSERT INTO student_academic_records (student_id, year_id, grade, class, number)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (student_id, target_year_id, st["grade"], st["class_no"], st["student_no"]),
                    )

        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "year": target_year,
            "yearId": target_year_id,
            "totalProcessed": len(final_to_insert) + len(parsing_errors),
            "successCount": len(final_to_insert),
            "failedCount": len(parsing_errors),
            "createdStudentsCount": created_count,
            "updatedRecordsCount": updated_count,
            "skippedRecordsCount": skipped_count,
            "errors": parsing_errors,
            "neisValidation": {
                "performed": should_validate_neis,
                "schoolName": NEIS_SCHOOL_NAME,
                "officeName": NEIS_OFFICE_NAME,
                "schoolCode": NEIS_SCHOOL_CODE,
                "officeCode": NEIS_OFFICE_CODE,
            },
        },
    ).model_dump()


# ==============================================================================
# 5. 교사 명단 엑셀/CSV 일괄 등록 & NEIS 연동 API (4.7)
# ==============================================================================

@admin_router.get("/neis/teacher-info", summary="NEIS 교원 명단 API 미제공 법적 근거 안내 (4.7)", response_model=SrFormat)
async def get_neis_teacher_integration_info(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    NEIS Open API의 교원 명단 직접 조회 API 미제공에 대한 구체적 법적/기술적 근거 및 학급 배정 연동 가이드를 반환합니다.
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    return SrFormat(
        status_code=200,
        success=True,
        data=explain_neis_teacher_api_limitation(),
    ).model_dump()


@admin_router.post("/teachers/batch", summary="교사 명단 엑셀/CSV 일괄 등록 및 학급 배정 (4.7)", response_model=SrFormat)
async def batch_register_teachers(
    request: Request,
    file: Optional[UploadFile] = File(None),
    year: Optional[int] = Form(None),
    overwrite: Optional[bool] = Form(True),
    validate_with_neis: Optional[bool] = Form(None),
    default_email_domain: Optional[str] = Form("bssm.hs.kr"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    교사 명단 엑셀(XLSX) 또는 CSV 파일 일괄 등록 및 담당 학급(담임) 배정 엔드포인트 (TODO.md 4.7)
    - 관리자 전용
    - XLSX / CSV 파일 자동 감지 및 멀티 인코딩(UTF-8, CP949, EUC-KR) 파싱
    - 수식 인젝션(Formula Injection) 방어 및 XSS 방어
    - 담임 배정 시 부산소프트웨어마이스터고 NEIS 개설 학급 실시간 교차 검증 (validate_with_neis)
    - 교사 정보(teachers) 단일 트랜잭션 원자적 등록 및 갱신
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    content_type = request.headers.get("content-type", "").lower()
    query_params = request.query_params

    # 1. 쿼리/폼/JSON 파라미터 우선순위 정규화
    target_year = year if year is not None else (int(query_params.get("year")) if query_params.get("year") else None)
    is_overwrite = bool(overwrite if overwrite is not None else query_params.get("overwrite", True))
    should_validate_neis = validate_with_neis if validate_with_neis is not None else True
    email_domain = default_email_domain or query_params.get("default_email_domain", "bssm.hs.kr")

    valid_records: List[Dict[str, Any]] = []
    parsing_errors: List[Dict[str, Any]] = []

    # 2. 파일 업로드 또는 JSON 바디 분기 처리
    if file is not None:
        file_bytes = await file.read()
        if len(file_bytes) > 10 * 1024 * 1024:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="FILE_TOO_LARGE", message="파일 크기는 최대 10MB까지 허용됩니다."),
                ).model_dump(),
            )
        try:
            valid_records, parsing_errors = parse_teacher_batch_file(
                file_bytes=file_bytes,
                filename=file.filename or "teachers.xlsx",
                default_email_domain=email_domain,
            )
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="FILE_PARSE_ERROR", message=f"파일 파싱 실패: {str(e)}"),
                ).model_dump(),
            )
    elif "application/json" in content_type:
        try:
            body = await request.json()
            if "year" in body and target_year is None:
                target_year = int(body["year"])
            if "overwrite" in body:
                is_overwrite = bool(body["overwrite"])
            if "validateWithNeis" in body:
                should_validate_neis = bool(body["validateWithNeis"])
            elif "validate_with_neis" in body:
                should_validate_neis = bool(body["validate_with_neis"])
            if "default_email_domain" in body:
                email_domain = body["default_email_domain"]

            raw_teachers = body.get("teachers", [])
            from core.excel_parser import EMAIL_REGEX

            seen_emails = set()
            seen_homerooms = set()

            for idx, item in enumerate(raw_teachers, start=1):
                clean_name = sanitize_text(item.get("name"))
                if not clean_name:
                    parsing_errors.append({"row": idx, "name": "", "reason": "교사 성명이 누락되었습니다."})
                    continue
                if len(clean_name) > 20:
                    parsing_errors.append({"row": idx, "name": clean_name, "reason": "교사 성명은 20자를 초과할 수 없습니다."})
                    continue
                name = html.escape(clean_name)

                # 교과
                subject = None
                raw_subj = item.get("subject")
                if raw_subj:
                    clean_subj = sanitize_text(raw_subj)
                    if len(clean_subj) > 20:
                        parsing_errors.append({"row": idx, "name": clean_name, "reason": "담당교과명은 20자를 초과할 수 없습니다."})
                        continue
                    subject = html.escape(clean_subj)

                # 학년/반 (담임)
                grade = item.get("grade")
                class_no = item.get("class_no") if item.get("class_no") is not None else item.get("classNo")

                if item.get("homeroom"):
                    g, c = parse_homeroom_field(item.get("homeroom"))
                    if g and c:
                        grade, class_no = g, c

                if (grade is not None and class_no is None) or (grade is None and class_no is not None):
                    parsing_errors.append({
                        "row": idx,
                        "name": clean_name,
                        "reason": "담임 학년과 반은 함께 지정되어야 합니다. (비담임인 경우 둘 다 비워두세요.)",
                    })
                    continue

                if grade is not None:
                    if not (1 <= grade <= 3):
                        parsing_errors.append({"row": idx, "name": clean_name, "reason": f"유효하지 않은 담당 학년입니다 ({grade})."})
                        continue
                    if not (1 <= class_no <= 20):
                        parsing_errors.append({"row": idx, "name": clean_name, "reason": f"유효하지 않은 담당 반입니다 ({class_no})."})
                        continue

                    homeroom_key = (grade, class_no)
                    if homeroom_key in seen_homerooms:
                        parsing_errors.append({
                            "row": idx,
                            "name": clean_name,
                            "reason": f"중복된 담임 학급 배정 ({grade}학년 {class_no}반)",
                        })
                        continue
                    seen_homerooms.add(homeroom_key)

                # 이메일
                email = (item.get("email") or "").strip().lower()
                if email:
                    if not EMAIL_REGEX.match(email):
                        parsing_errors.append({"row": idx, "name": clean_name, "reason": f"올바르지 않은 이메일 형식입니다: {email}"})
                        continue
                else:
                    en_name = re.sub(r"[^a-zA-Z0-9]", "", clean_name).lower()
                    if en_name:
                        email = f"{en_name}@{email_domain}"
                    else:
                        email = f"teacher_{idx}@{email_domain}"

                if email in seen_emails:
                    parsing_errors.append({"row": idx, "name": clean_name, "reason": f"중복된 이메일 ({email})"})
                    continue
                seen_emails.add(email)

                valid_records.append({
                    "row_number": idx,
                    "name": name,
                    "email": email,
                    "subject": subject,
                    "grade": grade,
                    "class_no": class_no,
                    "is_homeroom": bool(grade and class_no),
                })
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="VALIDATION_ERROR", message=f"JSON 바디 파싱 실패: {str(e)}"),
                ).model_dump(),
            )
    else:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(
                    code="VALIDATION_ERROR",
                    message="업로드할 엑셀/CSV 파일(file) 또는 교사 명단 JSON(teachers)이 필요합니다.",
                ),
            ).model_dump(),
        )

    # 3. 대상 학년도 확인 (NEIS 검증용)
    async with conn.cursor(cursor=DictCursor) as cur:
        if target_year is None:
            await cur.execute("SELECT year FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            y_row = await cur.fetchone()
            target_year = y_row["year"] if y_row else 2026

    # 4. NEIS 학급 실시간 교차 검증 (담임 배정 건 대상)
    neis_validated_classes: Dict[Tuple[int, int], bool] = {}
    final_to_insert: List[Dict[str, Any]] = []

    if should_validate_neis and valid_records:
        distinct_classes = set(
            (t["grade"], t["class_no"]) for t in valid_records if t["grade"] is not None and t["class_no"] is not None
        )
        for g, c in distinct_classes:
            is_valid, err_msg = await validate_class_against_neis(target_year, g, c)
            neis_validated_classes[(g, c)] = is_valid
            if not is_valid:
                for t in valid_records:
                    if t["grade"] == g and t["class_no"] == c:
                        parsing_errors.append({
                            "row": t.get("row_number", 0),
                            "name": t["name"],
                            "reason": err_msg or f"NEIS 미인가 학급 ({g}학년 {c}반 담임 배정 불가)",
                        })

        for t in valid_records:
            if t["grade"] is None or neis_validated_classes.get((t["grade"], t["class_no"]), True):
                final_to_insert.append(t)
    else:
        final_to_insert = valid_records

    # 5. DB 트랜잭션 원자적 일괄 등록 및 갱신
    created_count = 0
    updated_count = 0
    skipped_count = 0

    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            for t in final_to_insert:
                await cur.execute(
                    "SELECT teachers_id, name, subject, grade, class, is_deleted FROM teachers WHERE email = %s LIMIT 1",
                    (t["email"],),
                )
                ex_teacher = await cur.fetchone()

                if ex_teacher:
                    teachers_id = ex_teacher["teachers_id"]
                    if is_overwrite:
                        await cur.execute(
                            """
                            UPDATE teachers
                            SET name = %s,
                                subject = %s,
                                grade = %s,
                                class = %s,
                                is_deleted = FALSE
                            WHERE teachers_id = %s
                            """,
                            (t["name"], t["subject"], t["grade"], t["class_no"], teachers_id),
                        )
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    await cur.execute(
                        """
                        INSERT INTO teachers (name, subject, grade, class, email, is_deleted)
                        VALUES (%s, %s, %s, %s, %s, FALSE)
                        """,
                        (t["name"], t["subject"], t["grade"], t["class_no"], t["email"]),
                    )
                    created_count += 1

        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "year": target_year,
            "totalProcessed": len(final_to_insert) + len(parsing_errors),
            "successCount": len(final_to_insert),
            "failedCount": len(parsing_errors),
            "createdTeachersCount": created_count,
            "updatedTeachersCount": updated_count,
            "skippedTeachersCount": skipped_count,
            "errors": parsing_errors,
            "neisValidation": {
                "performed": should_validate_neis,
                "schoolName": NEIS_SCHOOL_NAME,
                "officeName": NEIS_OFFICE_NAME,
                "schoolCode": NEIS_SCHOOL_CODE,
                "officeCode": NEIS_OFFICE_CODE,
            },
        },
    ).model_dump()


# ------------------------------------------------------------------------------
# 4.8 시스템 종합 감사 로그 조회 API (GET /api/admin/audit-logs)
# ------------------------------------------------------------------------------

AUDIT_SUBMISSION_STATUS_MAP = {
    1: "제출완료",
    2: "검토중",
    3: "인정완료",
    4: "반려",
    5: "재제출요청",
}


@admin_router.get("/audit-logs", summary="시스템 종합 감사 로그 조회 (4.8)", response_model=SrFormat)
async def get_system_audit_logs(
    log_type: Optional[str] = Query("ALL", description="로그 유형 필터 ('ALL', 'SUBMISSION', 'MERIT')"),
    action_type: Optional[str] = Query(None, description="행위 구분 필터 ('CREATE', 'UPDATE', 'DELETE', 'APPROVE', 'REJECT', 등)"),
    student_id: Optional[int] = Query(None, description="학생 고유 ID 필터"),
    student_name: Optional[str] = Query(None, description="학생 성명 부분 일치 검색"),
    modifier_name: Optional[str] = Query(None, description="수정/처리자 성명 부분 일치 검색"),
    start_date: Optional[str] = Query(None, description="조회 시작 일시 (YYYY-MM-DD 또는 ISO8601)"),
    end_date: Optional[str] = Query(None, description="조회 종료 일시 (YYYY-MM-DD 또는 ISO8601)"),
    page: int = Query(1, ge=1, description="페이지 번호 (1부터 시작)"),
    limit: int = Query(20, ge=1, le=100, description="페이지 당 항목 수 (최대 100)"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    시스템 종합 감사 로그 조회 엔드포인트 (TODO.md 4.8 & Tech_spec.md 4.8)
    - 관리자 전용 (check_admin_access)
    - submissions_logs(증빙 심사 이력) 및 merits_log(상벌점 변경 이력) 통합/필터링 조회
    - 동적 SQL 파라미터 바인딩으로 SQL Injection 방어
    - 최신 발생 순(created_at DESC, log_id DESC) 정렬 및 페이지네이션
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    # 1. log_type 정규화
    normalized_log_type = (log_type or "ALL").strip().upper()
    if normalized_log_type not in ("ALL", "SUBMISSION", "MERIT"):
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="INVALID_LOG_TYPE", message="log_type은 'ALL', 'SUBMISSION', 'MERIT' 중 하나여야 합니다."),
            ).model_dump(),
        )

    # 2. 날짜 파라미터 유효성 검사 및 정규화
    norm_start_date = None
    if start_date:
        s_date = start_date.strip()
        if len(s_date) == 10:
            norm_start_date = f"{s_date} 00:00:00"
        else:
            norm_start_date = s_date.replace("T", " ")

    norm_end_date = None
    if end_date:
        e_date = end_date.strip()
        if len(e_date) == 10:
            norm_end_date = f"{e_date} 23:59:59"
        else:
            norm_end_date = e_date.replace("T", " ")

    # 3. submissions_logs 서브쿼리 구성
    sub_conditions = []
    sub_params: List[Any] = []

    if action_type:
        sub_conditions.append("sl.action_type = %s")
        sub_params.append(action_type.strip())
    if student_id is not None:
        sub_conditions.append("s.student_id = %s")
        sub_params.append(student_id)
    if student_name:
        sub_conditions.append("st.name LIKE %s")
        sub_params.append(f"%{student_name.strip()}%")
    if modifier_name:
        sub_conditions.append(
            "(CASE WHEN u.role IN (2, '2', 'admin') THEN '관리자' "
            "WHEN t.name IS NOT NULL THEN t.name "
            "WHEN mst.name IS NOT NULL THEN mst.name "
            "ELSE '알 수 없음' END) LIKE %s"
        )
        sub_params.append(f"%{modifier_name.strip()}%")
    if norm_start_date:
        sub_conditions.append("sl.created_at >= %s")
        sub_params.append(norm_start_date)
    if norm_end_date:
        sub_conditions.append("sl.created_at <= %s")
        sub_params.append(norm_end_date)

    sub_where_clause = f"WHERE {' AND '.join(sub_conditions)}" if sub_conditions else ""

    sub_query = f"""
    SELECT
        CONCAT('SUB_', sl.log_id) AS unified_id,
        sl.log_id AS original_log_id,
        'SUBMISSION' AS log_type,
        sl.action_type,
        sl.submission_id AS target_id,
        s.student_id,
        st.name AS student_name,
        sar.grade AS student_grade,
        sar.class AS student_class,
        sar.number AS student_number,
        sl.modifier_uuid,
        CASE
            WHEN u.role IN (2, '2', 'admin') THEN '관리자'
            WHEN t.name IS NOT NULL THEN t.name
            WHEN mst.name IS NOT NULL THEN mst.name
            ELSE '알 수 없음'
        END AS modifier_name,
        CASE
            WHEN u.role IN (2, '2', 'admin') THEN 'admin'
            WHEN t.teachers_id IS NOT NULL THEN 'teacher'
            WHEN mst.student_id IS NOT NULL THEN 'student'
            ELSE 'unknown'
        END AS modifier_role,
        sl.old_status_code,
        sl.new_status_code,
        sl.old_score,
        sl.new_score,
        NULL AS old_points,
        NULL AS new_points,
        sl.comment AS reason,
        s.detail AS target_title,
        sl.created_at
    FROM submissions_logs sl
    JOIN submissions s ON sl.submission_id = s.submission_id
    JOIN students st ON s.student_id = st.student_id
    LEFT JOIN (
        SELECT sar1.*
        FROM student_academic_records sar1
        JOIN (
            SELECT student_id, MAX(year_id) AS max_year_id
            FROM student_academic_records
            GROUP BY student_id
        ) sar_max ON sar1.student_id = sar_max.student_id AND sar1.year_id = sar_max.max_year_id
    ) sar ON st.student_id = sar.student_id
    LEFT JOIN users u ON sl.modifier_uuid = u.uuid
    LEFT JOIN teachers t ON sl.modifier_uuid = t.uuid
    LEFT JOIN students mst ON sl.modifier_uuid = mst.uuid
    {sub_where_clause}
    """

    # 4. merits_log 서브쿼리 구성
    merit_conditions = []
    merit_params: List[Any] = []

    if action_type:
        merit_conditions.append("ml.action_type = %s")
        merit_params.append(action_type.strip())
    if student_id is not None:
        merit_conditions.append("m.student_id = %s")
        merit_params.append(student_id)
    if student_name:
        merit_conditions.append("st.name LIKE %s")
        merit_params.append(f"%{student_name.strip()}%")
    if modifier_name:
        merit_conditions.append(
            "(CASE WHEN u.role IN (2, '2', 'admin') THEN '관리자' "
            "WHEN t.name IS NOT NULL THEN t.name "
            "WHEN mst.name IS NOT NULL THEN mst.name "
            "ELSE '알 수 없음' END) LIKE %s"
        )
        merit_params.append(f"%{modifier_name.strip()}%")
    if norm_start_date:
        merit_conditions.append("ml.created_at >= %s")
        merit_params.append(norm_start_date)
    if norm_end_date:
        merit_conditions.append("ml.created_at <= %s")
        merit_params.append(norm_end_date)

    merit_where_clause = f"WHERE {' AND '.join(merit_conditions)}" if merit_conditions else ""

    merit_query = f"""
    SELECT
        CONCAT('MERIT_', ml.log_id) AS unified_id,
        ml.log_id AS original_log_id,
        'MERIT' AS log_type,
        ml.action_type,
        ml.merits_point_id AS target_id,
        m.student_id,
        st.name AS student_name,
        sar.grade AS student_grade,
        sar.class AS student_class,
        sar.number AS student_number,
        ml.modifier_uuid,
        CASE
            WHEN u.role IN (2, '2', 'admin') THEN '관리자'
            WHEN t.name IS NOT NULL THEN t.name
            WHEN mst.name IS NOT NULL THEN mst.name
            ELSE '알 수 없음'
        END AS modifier_name,
        CASE
            WHEN u.role IN (2, '2', 'admin') THEN 'admin'
            WHEN t.teachers_id IS NOT NULL THEN 'teacher'
            WHEN mst.student_id IS NOT NULL THEN 'student'
            ELSE 'unknown'
        END AS modifier_role,
        NULL AS old_status_code,
        NULL AS new_status_code,
        NULL AS old_score,
        NULL AS new_score,
        ml.old_points,
        ml.new_points,
        ml.modify_reason AS reason,
        m.reason AS target_title,
        ml.created_at
    FROM merits_log ml
    JOIN merits m ON ml.merits_point_id = m.merits_point_id
    JOIN students st ON m.student_id = st.student_id
    LEFT JOIN (
        SELECT sar1.*
        FROM student_academic_records sar1
        JOIN (
            SELECT student_id, MAX(year_id) AS max_year_id
            FROM student_academic_records
            GROUP BY student_id
        ) sar_max ON sar1.student_id = sar_max.student_id AND sar1.year_id = sar_max.max_year_id
    ) sar ON st.student_id = sar.student_id
    LEFT JOIN users u ON ml.modifier_uuid = u.uuid
    LEFT JOIN teachers t ON ml.modifier_uuid = t.uuid
    LEFT JOIN students mst ON ml.modifier_uuid = mst.uuid
    {merit_where_clause}
    """

    # 5. log_type에 따른 UNION 또는 단일 쿼리 구성
    if normalized_log_type == "SUBMISSION":
        base_query = sub_query
        base_params = list(sub_params)
    elif normalized_log_type == "MERIT":
        base_query = merit_query
        base_params = list(merit_params)
    else:  # ALL
        base_query = f"{sub_query}\nUNION ALL\n{merit_query}"
        base_params = list(sub_params) + list(merit_params)

    count_query = f"SELECT COUNT(*) AS total_count FROM ({base_query}) AS combined_logs"

    offset = (page - 1) * limit
    paged_query = f"""
    SELECT *
    FROM ({base_query}) AS combined_logs
    ORDER BY created_at DESC, original_log_id DESC
    LIMIT %s OFFSET %s
    """
    paged_params = list(base_params) + [limit, offset]

    async with conn.cursor(cursor=DictCursor) as cur:
        # 전체 개수 카운트
        await cur.execute(count_query, tuple(base_params))
        count_row = await cur.fetchone()
        total_count = count_row.get("total_count", 0) if count_row else 0

        # 페이지네이션된 목록 조회
        await cur.execute(paged_query, tuple(paged_params))
        rows = await cur.fetchall() or []

    # 6. 반환 DTO 매핑
    total_pages = (total_count + limit - 1) // limit if total_count > 0 else 0
    has_next = page < total_pages
    has_prev = page > 1

    formatted_items = []
    for r in rows:
        c_at = r.get("created_at")
        c_at_str = c_at.isoformat() if hasattr(c_at, "isoformat") else str(c_at) if c_at else None

        o_score = float(r["old_score"]) if r.get("old_score") is not None else None
        n_score = float(r["new_score"]) if r.get("new_score") is not None else None
        o_pts = float(r["old_points"]) if r.get("old_points") is not None else None
        n_pts = float(r["new_points"]) if r.get("new_points") is not None else None

        old_st_code = r.get("old_status_code")
        new_st_code = r.get("new_status_code")

        log_item = {
            "id": r["unified_id"],
            "logId": r["original_log_id"],
            "logType": r["log_type"],
            "actionType": r["action_type"],
            "targetId": r["target_id"],
            "targetTitle": r.get("target_title"),
            "student": {
                "id": r.get("student_id"),
                "name": r.get("student_name"),
                "grade": r.get("student_grade"),
                "classNo": r.get("student_class"),
                "number": r.get("student_number"),
            },
            "modifier": {
                "uuid": r.get("modifier_uuid"),
                "name": r.get("modifier_name") or "알 수 없음",
                "role": r.get("modifier_role") or "unknown",
            },
            "changes": {
                "oldStatusCode": old_st_code,
                "newStatusCode": new_st_code,
                "oldStatusName": AUDIT_SUBMISSION_STATUS_MAP.get(old_st_code) if old_st_code is not None else None,
                "newStatusName": AUDIT_SUBMISSION_STATUS_MAP.get(new_st_code) if new_st_code is not None else None,
                "oldScore": o_score,
                "newScore": n_score,
                "oldPoints": o_pts,
                "newPoints": n_pts,
            },
            "reason": r.get("reason"),
            "createdAt": c_at_str,
        }
        formatted_items.append(log_item)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "logs": formatted_items,
            "pagination": {
                "totalCount": total_count,
                "page": page,
                "limit": limit,
                "totalPages": total_pages,
                "hasNext": has_next,
                "hasPrev": has_prev,
            },
            "filter": {
                "logType": normalized_log_type,
                "actionType": action_type,
                "studentId": student_id,
                "studentName": student_name,
                "modifierName": modifier_name,
                "startDate": start_date,
                "endDate": end_date,
            },
        },
    ).model_dump()


# ------------------------------------------------------------------------------
# 4.9 교내 통계 데이터 엑셀/CSV/PDF 내보내기 API (GET /api/admin/export/stats)
# ------------------------------------------------------------------------------

def _escape_csv_val(val: Any) -> str:
    """CSV 셀 내 따옴표, 콤마 및 수식 인젝션 방어용 이스케이프"""
    if val is None:
        return ""
    s = str(val).replace('"', '""')
    if s.startswith(("=", "+", "-", "@")):
        s = f"'{s}"
    if any(c in s for c in (",", '"', "\n", "\r")):
        return f'"{s}"'
    return s


# ------------------------------------------------------------------------------
# ReportLab PDF 리포트 생성기 모듈 연동 (core.pdf)
# ------------------------------------------------------------------------------
from core.pdf import generate_stats_pdf, _generate_stats_pdf


@admin_router.get("/export/stats", summary="교내 통계 데이터 엑셀/CSV/PDF 내보내기 (4.9)")
async def export_school_stats(
    year: Optional[int] = Query(None, description="조회 학년도 (미지정 시 활성 학년도)"),
    format: str = Query("xlsx", description="출력 파일 형식 ('csv', 'xlsx', 'pdf')"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    교내 통계 데이터 및 학생별 인증 결과 일괄 내보내기 엔드포인트 (TODO.md 4.9 & Tech_spec.md 4.9)
    - 관리자 전용 (check_admin_access)
    - 출력 포맷: csv, xlsx, pdf 로 엄격 제한 (미지원 포맷 시 400 에러)
    - CSV: UTF-8 BOM 인코딩 적용 (엑셀 한글 깨짐 방지) 및 수식 인젝션 방어
    - XLSX: 학생별 원천 시트 + 통계 요약 시트 + openpyxl 막대 차트(BarChart) 시각화 그래프 임베딩
    - PDF: 외부 라이브러리 없이 순수 PDF 벡터 그래픽 기반의 인증 상태 분포 막대 그래프(Bar Chart Graph) 리포트 렌더링
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    # 1. 포맷 검증
    norm_format = (format or "xlsx").strip().lower()
    if norm_format not in ("csv", "xlsx", "pdf"):
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="INVALID_FORMAT", message="지원하지 않는 출력 형식입니다. 'csv', 'xlsx', 'pdf' 중 하나여야 합니다."),
            ).model_dump(),
        )

    # 2. 대상 학년도 결정
    target_year = year
    year_id = None
    async with conn.cursor(cursor=DictCursor) as cur:
        if target_year is None:
            await cur.execute("SELECT year_id, year FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            y_row = await cur.fetchone()
            if not y_row:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="ACADEMIC_YEAR_NOT_FOUND", message="등록된 학년도가 없습니다."),
                    ).model_dump(),
                )
            year_id = y_row["year_id"]
            target_year = y_row["year"]
        else:
            await cur.execute("SELECT year_id, year FROM academic_years WHERE year = %s", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="ACADEMIC_YEAR_NOT_FOUND", message=f"{target_year} 학년도가 존재하지 않습니다."),
                    ).model_dump(),
                )
            year_id = y_row["year_id"]

        # 3. 인증 영역 및 평가 항목 조회
        await cur.execute(
            """
            SELECT ca.area_id, ca.name AS area_name, ca.max_score,
                   ei.item_id, ei.name AS item_name, ei.max_score AS item_max_score, ei.scoring_type
            FROM certification_areas ca
            LEFT JOIN evaluation_items ei ON ca.area_id = ei.area_id AND ei.is_active = TRUE
            WHERE ca.year_id = %s
            ORDER BY ca.area_id ASC, ei.item_id ASC
            """,
            (year_id,),
        )
        criteria_rows = await cur.fetchall() or []

        # 영역별 맵 구성
        areas_dict: Dict[int, Dict[str, Any]] = {}
        for row in criteria_rows:
            aid = row["area_id"]
            if aid not in areas_dict:
                areas_dict[aid] = {
                    "area_id": aid,
                    "name": row["area_name"],
                    "max_score": float(row["max_score"] or 0),
                    "items": [],
                }
            if row.get("item_id"):
                areas_dict[aid]["items"].append({
                    "item_id": row["item_id"],
                    "name": row["item_name"],
                    "max_score": float(row["item_max_score"] or 0),
                    "scoring_type": row["scoring_type"],
                })
        areas_list = list(areas_dict.values())

        # 4. 해당 학년도 학생 목록 조회
        await cur.execute(
            """
            SELECT st.student_id, st.name, st.email,
                   sar.grade, sar.class, sar.number
            FROM student_academic_records sar
            JOIN students st ON sar.student_id = st.student_id
            WHERE sar.year_id = %s AND st.is_deleted = FALSE
            ORDER BY sar.grade ASC, sar.class ASC, sar.number ASC
            """,
            (year_id,),
        )
        students = await cur.fetchall() or []

        # 5. 해당 학년도 전교생 제출 증빙 및 상벌점 일괄 조회
        await cur.execute(
            """
            SELECT s.submission_id, s.student_id, s.item_id, s.status_code, s.granted_score,
                   ei.area_id
            FROM submissions s
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id
            WHERE ca.year_id = %s AND s.is_deleted = FALSE
            """,
            (year_id,),
        )
        all_submissions = await cur.fetchall() or []

        await cur.execute(
            """
            SELECT merits_point_id, student_id, points, related_area
            FROM merits
            WHERE is_reflected = TRUE AND is_deleted = FALSE
            """,
        )
        all_merits = await cur.fetchall() or []

    # 6. 학생별 제출물/상벌점 그룹핑
    subs_by_student: Dict[int, List[Dict[str, Any]]] = {}
    for s in all_submissions:
        sid = s["student_id"]
        subs_by_student.setdefault(sid, []).append(s)

    merits_by_student: Dict[int, List[Dict[str, Any]]] = {}
    for m in all_merits:
        sid = m["student_id"]
        merits_by_student.setdefault(sid, []).append(m)

    # 7. 학생별 역량인증제 평가 계산 (단일화된 calculate_student_certification 활용)
    student_results = []
    cert_status_counts = {"인증 가능": 0, "검토중": 0, "보완 필요": 0, "미달성": 0}

    for st in students:
        sid = st["student_id"]
        st_subs = subs_by_student.get(sid, [])
        st_merits = merits_by_student.get(sid, [])

        calc = calculate_student_certification(areas_list, st_subs, st_merits)
        cert_status = calc.get("certStatus", "미달성")
        if cert_status in cert_status_counts:
            cert_status_counts[cert_status] += 1
        else:
            cert_status_counts["미달성"] += 1

        student_no_fmt = f"{st['grade']}{st['class']:02d}{st['number']:02d}"

        # 영역별 점수 맵
        area_score_map = {a.get("area") or a.get("name"): a["score"] for a in calc.get("areas", [])}

        student_results.append({
            "student_id": sid,
            "student_number": student_no_fmt,
            "grade": st["grade"],
            "class": st["class"],
            "number": st["number"],
            "name": st["name"],
            "email": st["email"],
            "total_score": calc.get("totalScore", 0.0),
            "cert_status": cert_status,
            "pending_count": calc.get("pendingCount", 0),
            "point_total": calc.get("pointTotal", 0.0),
            "area_scores": area_score_map,
        })

    # 정렬된 영역 명칭 목록
    area_names = [a["name"] for a in areas_list]

    # ==========================================================================
    # 포맷별 파일 생성 및 스트리밍 응답
    # ==========================================================================

    # 8. CSV 형식 출력
    if norm_format == "csv":
        import io
        csv_buffer = io.StringIO()

        # 헤더 생성
        headers = ["학번", "학년", "반", "번호", "이름", "이메일", "취득총점", "인증상태", "상벌점합계", "검토대기건수"] + area_names
        csv_buffer.write(",".join([_escape_csv_val(h) for h in headers]) + "\n")

        # 행 데이터 작성
        for sr in student_results:
            row = [
                sr["student_number"],
                sr["grade"],
                sr["class"],
                sr["number"],
                sr["name"],
                sr["email"],
                f"{sr['total_score']:.1f}",
                sr["cert_status"],
                f"{sr['point_total']:.1f}",
                sr["pending_count"],
            ]
            for aname in area_names:
                sc = sr["area_scores"].get(aname, 0.0)
                row.append(f"{sc:.1f}")
            csv_buffer.write(",".join([_escape_csv_val(v) for v in row]) + "\n")

        filename = f"meister_stats_{target_year}.csv"
        return Response(
            content=csv_buffer.getvalue().encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # 9. XLSX 형식 출력 (openpyxl + 차트 시트)
    elif norm_format == "xlsx":
        import io
        import openpyxl
        from openpyxl.chart import BarChart, Reference

        wb = openpyxl.Workbook()

        # Sheet 1: 학생별 인증 상세
        ws_students = wb.active
        ws_students.title = "학생별 인증 결과"

        headers = ["학번", "학년", "반", "번호", "이름", "이메일", "취득총점", "인증상태", "상벌점합계", "검토대기건수"] + area_names
        ws_students.append(headers)

        for sr in student_results:
            row = [
                sr["student_number"],
                sr["grade"],
                sr["class"],
                sr["number"],
                sr["name"],
                sr["email"],
                sr["total_score"],
                sr["cert_status"],
                sr["point_total"],
                sr["pending_count"],
            ]
            for aname in area_names:
                row.append(sr["area_scores"].get(aname, 0.0))
            ws_students.append(row)

        # Sheet 2: 통계 요약 및 막대 차트
        ws_stats = wb.create_sheet("통계 요약 및 차트")
        ws_stats.append(["인증 상태", "학생 수 (명)"])
        stat_rows = [
            ("인증 가능", cert_status_counts.get("인증 가능", 0)),
            ("검토중", cert_status_counts.get("검토중", 0)),
            ("보완 필요", cert_status_counts.get("보완 필요", 0)),
            ("미달성", cert_status_counts.get("미달성", 0)),
        ]
        for cat, cnt in stat_rows:
            ws_stats.append([cat, cnt])

        # openpyxl BarChart 추가
        chart = BarChart()
        chart.type = "col"
        chart.style = 10
        chart.title = f"{target_year}학년도 마이스터 역량인증 상태 분포"
        chart.y_axis.title = "학생 수 (명)"
        chart.x_axis.title = "인증 상태"

        data_ref = Reference(ws_stats, min_col=2, min_row=1, max_row=5)
        cats_ref = Reference(ws_stats, min_col=1, min_row=2, max_row=5)
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        chart.width = 16
        chart.height = 10
        ws_stats.add_chart(chart, "D2")

        xlsx_buffer = io.BytesIO()
        wb.save(xlsx_buffer)
        filename = f"meister_stats_{target_year}.xlsx"

        return Response(
            content=xlsx_buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # 10. PDF 형식 출력 (벡터 그래프 리포트)
    else:  # norm_format == "pdf"
        # 대회 명세서 7.4 등급별 전체 집계 및 5.1 영역별 평균 달성률 산출
        grade_counts = {"S": 0, "A": 0, "B": 0, "미달성": 0}
        area_totals: Dict[str, Dict[str, float]] = {
            a["name"]: {"total_score": 0.0, "max_score": a["max_score"]}
            for a in areas_list
        }

        for sr in student_results:
            for aname, sc in sr["area_scores"].items():
                if aname in area_totals:
                    area_totals[aname]["total_score"] += sc
                    amax = area_totals[aname]["max_score"]
                    ratio = (sc / amax * 100.0) if amax > 0 else 0.0
                    if ratio >= 90.0:
                        grade_counts["S"] += 1
                    elif ratio >= 80.0:
                        grade_counts["A"] += 1
                    elif ratio >= 70.0:
                        grade_counts["B"] += 1
                    else:
                        grade_counts["미달성"] += 1

        total_st_cnt = len(students)
        area_averages = []
        for a in areas_list:
            aname = a["name"]
            amax = a["max_score"]
            t_score = area_totals[aname]["total_score"]
            avg_sc = (t_score / total_st_cnt) if total_st_cnt > 0 else 0.0
            r = (avg_sc / amax * 100.0) if amax > 0 else 0.0
            area_averages.append({
                "name": aname,
                "avg_score": round(avg_sc, 1),
                "max_score": amax,
                "ratio": round(r, 1),
            })

        pdf_bytes = _generate_stats_pdf(
            target_year=target_year,
            total_students=total_st_cnt,
            cert_counts=cert_status_counts,
            grade_counts=grade_counts,
            area_averages=area_averages,
        )
        filename = f"meister_stats_{target_year}.pdf"

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )



