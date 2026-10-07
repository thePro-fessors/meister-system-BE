"""
routers/admin.py - 학년도 관리, 평가 기준 관리, 관리자 통계 개요 API 라우터 (API-03)
"""

import html
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import redis.asyncio as aioredis

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

from core.academic import clear_year_cache
from core.calculator import calculate_student_certification
from core.excel_parser import parse_student_batch_file, sanitize_text
from core.neis import (
    explain_neis_student_api_limitation,
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
