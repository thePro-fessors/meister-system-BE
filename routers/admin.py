"""
routers/admin.py - 학년도 관리, 평가 기준 관리, 관리자 통계 개요 API 라우터 (API-03)
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import redis.asyncio as aioredis

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

from core.calculator import calculate_student_certification
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

    is_act = req.is_activated if req.is_activated is not None else (req.isActivated or False)

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

        await cur.execute(
            "INSERT INTO academic_years (year, is_activated) VALUES (%s, %s)",
            (req.year, is_act),
        )
        await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
        last_row = await cur.fetchone()
        year_id = last_row[0] if isinstance(last_row, (list, tuple)) else last_row["last_id"]

    return SrFormat(
        status_code=201,
        success=True,
        data={"yearId": year_id, "year": req.year, "isActivated": is_act},
    ).model_dump()


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

    is_act = req.is_activated if req.is_activated is not None else (req.isActivated if req.isActivated is not None else True)

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            "UPDATE academic_years SET is_activated = %s WHERE year = %s",
            (is_act, year),
        )
        if cur.rowcount == 0:
            return JSONResponse(
                status_code=404,
                content=SrFormat(
                    status_code=404,
                    success=False,
                    data=None,
                    error=Error(code="ITEM_NOT_FOUND", message=f"{year} 학년도를 찾을 수 없습니다."),
                ).model_dump(),
            )

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

                # 해당 영역의 항목 복제
                await cur.execute(
                    """
                    SELECT name, max_score, scoring_type, target_grade, requires_evidence
                    FROM evaluation_items
                    WHERE area_id = %s AND is_active = TRUE
                    """,
                    (s_area["area_id"],),
                )
                source_items = await cur.fetchall()

                for s_item in source_items:
                    await cur.execute(
                        """
                        INSERT INTO evaluation_items (
                            area_id, name, max_score, scoring_type, target_grade, requires_evidence, is_active
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                        """,
                        (
                            t_area_id,
                            s_item["name"],
                            s_item["max_score"],
                            s_item["scoring_type"],
                            s_item["target_grade"],
                            s_item["requires_evidence"],
                        ),
                    )
                    copied_items_count += 1

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

        updated_name = req.name if req.name is not None else existing["name"]
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
    scoring_type = req.scoring_type if req.scoring_type is not None else (req.scoringType or 1)
    target_grade = req.target_grade if req.target_grade is not None else (req.targetGrade or 1)
    requires_ev = req.requires_evidence if req.requires_evidence is not None else (req.requiresEvidence if req.requiresEvidence is not None else True)

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
            (area_id, req.name, max_score, scoring_type, target_grade, requires_ev),
        )
        await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
        last_row = await cur.fetchone()
        item_id = last_row[0] if isinstance(last_row, (list, tuple)) else last_row["last_id"]

    return SrFormat(
        status_code=201,
        success=True,
        data={
            "itemId": item_id,
            "areaId": area_id,
            "name": req.name,
            "maxScore": max_score,
            "scoringType": scoring_type,
            "targetGrade": target_grade,
            "requiresEvidence": requires_ev,
        },
    ).model_dump()


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

        new_name = req.name if req.name is not None else existing["name"]
        new_max_score = req.max_score if req.max_score is not None else (req.maxScore if req.maxScore is not None else float(existing["max_score"]))
        new_type = req.scoring_type if req.scoring_type is not None else (req.scoringType if req.scoringType is not None else existing["scoring_type"])
        new_grade = req.target_grade if req.target_grade is not None else (req.targetGrade if req.targetGrade is not None else existing["target_grade"])
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

        # 2. 제출 상태별 카운트
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
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id AND ca.year_id = %s
            WHERE s.is_deleted = FALSE
            """,
            (year_id,),
        )
        sub_stats = await cur.fetchone() or {}

        # 3. 인증 영역별 제출 수 통계
        await cur.execute(
            """
            SELECT ca.area_id AS areaId, ca.name AS areaName, COUNT(s.submission_id) AS submissionCount
            FROM certification_areas ca
            LEFT JOIN evaluation_items ei ON ca.area_id = ei.area_id
            LEFT JOIN submissions s ON ei.item_id = s.item_id AND s.is_deleted = FALSE
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
