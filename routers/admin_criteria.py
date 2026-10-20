"""
routers/admin_criteria.py - 학년도 및 평가 기준 관리 API 서브라우터
"""

"""
routers/admin.py - 학년도 관리, 평가 기준 관리, 관리자 통계 개요 API 라우터 (API-03)
"""

import io
import json
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

from core.academic import clear_year_cache, get_current_academic_year, get_current_year
from core.calculator import calculate_student_certification
from core.excel_parser import (
    EMAIL_REGEX,
    parse_homeroom_field,
    parse_student_batch_file,
    parse_student_number_field,
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
from core.security import error_response, get_current_user, normalize_role
from database import get_db, get_redis
from sr_format import Error, SrFormat

# ------------------------------------------------------------------------------
# 라우터 정의
# ------------------------------------------------------------------------------
years_router = APIRouter(prefix="/api/years", tags=["years"])
criteria_router = APIRouter(prefix="/api/criteria", tags=["criteria"])
admin_router = APIRouter(tags=["admin"])


def check_admin_access(current_user: Dict[str, Any]) -> Optional[JSONResponse]:
    """관리자 권한 필수 검증 의존성 헬퍼"""
    if normalize_role(current_user.get("role")) != "admin":
        return error_response(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "관리자 권한이 필요합니다.",
        )
    return None


# ------------------------------------------------------------------------------
# DTO 스키마
# ------------------------------------------------------------------------------
class CreateYearRequest(BaseModel):
    year: int
    is_activated: Optional[bool] = None
    isActivated: Optional[bool] = None
    active: Optional[bool] = None

    model_config = {"populate_by_name": True}


class PatchYearRequest(BaseModel):
    is_activated: Optional[bool] = None
    isActivated: Optional[bool] = None
    active: Optional[bool] = None

    model_config = {"populate_by_name": True}


class PatchAreaRequest(BaseModel):
    area_id: Optional[int] = None
    areaId: Optional[int] = None
    name: Optional[str] = None
    max_score: Optional[float] = None
    maxScore: Optional[float] = None
    subject: Optional[str] = None
    department: Optional[str] = None
    teacher_ids: Optional[List[int]] = None
    teacherIds: Optional[List[int]] = None


class CreateItemRequest(BaseModel):
    name: str
    max_score: Optional[float] = None
    maxScore: Optional[float] = None
    subject: Optional[str] = None
    department: Optional[str] = None
    scoring_type: Optional[int] = None
    scoringType: Optional[int] = None
    target_grade: Optional[int] = None
    targetGrade: Optional[int] = None
    requires_evidence: Optional[bool] = None
    requiresEvidence: Optional[bool] = None
    teacher_ids: Optional[List[int]] = None
    teacherIds: Optional[List[int]] = None


class UpdateItemRequest(BaseModel):
    name: Optional[str] = None
    max_score: Optional[float] = None
    maxScore: Optional[float] = None
    subject: Optional[str] = None
    department: Optional[str] = None
    scoring_type: Optional[int] = None
    scoringType: Optional[int] = None
    target_grade: Optional[int] = None
    targetGrade: Optional[int] = None
    requires_evidence: Optional[bool] = None
    requiresEvidence: Optional[bool] = None
    is_active: Optional[bool] = None
    isActive: Optional[bool] = None
    teacher_ids: Optional[List[int]] = None
    teacherIds: Optional[List[int]] = None


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
    position: Optional[str] = None
    department: Optional[str] = None
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


class RecalculateBatchRequest(BaseModel):
    year: Optional[int] = None
    grade: Optional[int] = None
    student_id: Optional[int] = None
    studentId: Optional[int] = None
    dry_run: Optional[bool] = False
    dryRun: Optional[bool] = False
    fix_anomalies: Optional[bool] = False
    fixAnomalies: Optional[bool] = False


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
        return error_response(400, "VALIDATION_ERROR", "유효하지 않은 학년도입니다 (1900~2100).")

    is_act = req.is_activated if req.is_activated is not None else (req.isActivated if req.isActivated is not None else (req.active if req.active is not None else False))

    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (req.year,))
            if await cur.fetchone():
                return error_response(409, "DUPLICATE_YEAR", f"{req.year} 학년도가 이미 등록되어 있습니다.")

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
        return error_response(400, "VALIDATION_ERROR", "유효하지 않은 학년도입니다 (1900~2100).")

    is_act = req.is_activated if req.is_activated is not None else (req.isActivated if req.isActivated is not None else (req.active if req.active is not None else True))

    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (year,))
            if not await cur.fetchone():
                return error_response(404, "ITEM_NOT_FOUND", f"{year} 학년도를 찾을 수 없습니다.")

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
        return error_response(400, "VALIDATION_ERROR", "동일한 학년도로부터 기준을 복제할 수 없습니다.")

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (source_year,))
        s_row = await cur.fetchone()
        await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
        t_row = await cur.fetchone()

    if not s_row:
        return error_response(404, "ITEM_NOT_FOUND", f"복제 원본 {source_year} 학년도가 존재하지 않습니다.")
    if not t_row:
        return error_response(404, "ITEM_NOT_FOUND", f"복제 대상 {target_year} 학년도가 존재하지 않습니다.")

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
            return error_response(409, "CONFLICT", f"{target_year} 학년도에 이미 평가 기준이 설정되어 있습니다.")

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
        return error_response(404, "CRITERIA_NOT_CONFIGURED", f"{source_year} 학년도에 설정된 평가 기준이 없습니다.")

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
                t_area_id = cur.lastrowid
                if not t_area_id:
                    await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
                    last_id_row = await cur.fetchone()
                    if last_id_row:
                        t_area_id = last_id_row.get("last_id") if isinstance(last_id_row, dict) else last_id_row[0]
                if not t_area_id:
                    await cur.execute(
                        "SELECT area_id FROM certification_areas WHERE year_id = %s AND grade = %s AND name = %s LIMIT 1",
                        (t_year_id, s_area["grade"], s_area["name"]),
                    )
                    fb_row = await cur.fetchone()
                    if fb_row:
                        t_area_id = fb_row.get("area_id") if isinstance(fb_row, dict) else fb_row[0]
                copied_areas_count += 1

                # 해당 영역의 항목 복제 (벌크 배치 최적화 및 subject, scoring_rules, teacher 보존: SECURITY_AND_AUDIT.md 3.15)
                await cur.execute(
                    """
                    SELECT item_id, name, subject, department, max_score, scoring_type, target_grade, requires_evidence
                    FROM evaluation_items
                    WHERE area_id = %s AND is_active = TRUE
                    """,
                    (s_area["area_id"],),
                )
                source_items = await cur.fetchall()

                if source_items:
                    for s_item in source_items:
                        await cur.execute(
                            """
                            INSERT INTO evaluation_items (
                                area_id, name, department, max_score, scoring_type, target_grade, requires_evidence, subject, is_active
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                            """,
                            (
                                t_area_id,
                                s_item["name"],
                                s_item.get("department"),
                                s_item["max_score"],
                                s_item["scoring_type"],
                                s_item["target_grade"],
                                s_item["requires_evidence"],
                                s_item.get("subject"),
                            ),
                        )
                        new_item_id = cur.lastrowid
                        if not new_item_id:
                            await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
                            last_id_row = await cur.fetchone()
                            if last_id_row:
                                new_item_id = last_id_row.get("last_id") if isinstance(last_id_row, dict) else last_id_row[0]
                        if not new_item_id and t_area_id:
                            await cur.execute(
                                "SELECT item_id FROM evaluation_items WHERE area_id = %s AND name = %s ORDER BY item_id DESC LIMIT 1",
                                (t_area_id, s_item["name"]),
                            )
                            fb_item_row = await cur.fetchone()
                            if fb_item_row:
                                new_item_id = fb_item_row.get("item_id") if isinstance(fb_item_row, dict) else fb_item_row[0]
                        copied_items_count += 1

                        # 세부 점수 기준(scoring_rules) 테이블 복제 보존
                        s_item_id = s_item.get("item_id") if isinstance(s_item, dict) else s_item[0]
                        if s_item_id and new_item_id:
                            await cur.execute(
                                """
                                SELECT condition_value, score
                                FROM scoring_rules
                                WHERE item_id = %s
                                """,
                                (s_item_id,),
                            )
                            s_rules = await cur.fetchall() or []
                            valid_rules = []
                            for r in s_rules:
                                if isinstance(r, dict):
                                    c_val = r.get("condition_value")
                                    sc_val = r.get("score")
                                elif isinstance(r, (list, tuple)) and len(r) >= 2:
                                    c_val = r[0]
                                    sc_val = r[1]
                                else:
                                    continue
                                valid_rules.append((new_item_id, c_val, sc_val))

                            if valid_rules:
                                rules_sql = "INSERT INTO scoring_rules (item_id, condition_value, score) VALUES (%s, %s, %s)"
                                if hasattr(cur, "executemany"):
                                    await cur.executemany(rules_sql, valid_rules)
                                else:
                                    for rp in valid_rules:
                                        await cur.execute(rules_sql, rp)

                            # 1순위 평가 항목 전담 교사(evaluation_item_teachers) 재직 교사 대상 보존 복제
                            try:
                                await cur.execute(
                                    """
                                    SELECT eit.teacher_id
                                    FROM evaluation_item_teachers eit
                                    JOIN teachers t ON eit.teacher_id = t.teachers_id
                                    WHERE eit.item_id = %s AND t.is_deleted = FALSE
                                    """,
                                    (s_item_id,),
                                )
                                s_teachers = await cur.fetchall() or []
                                valid_tids = [
                                    (t.get("teacher_id") if isinstance(t, dict) else t[0])
                                    for t in s_teachers
                                    if (t.get("teacher_id") if isinstance(t, dict) else t[0]) is not None
                                ]
                                if valid_tids:
                                    t_params = [(new_item_id, tid) for tid in valid_tids]
                                    t_sql = "INSERT INTO evaluation_item_teachers (item_id, teacher_id) VALUES (%s, %s)"
                                    if hasattr(cur, "executemany"):
                                        await cur.executemany(t_sql, t_params)
                                    else:
                                        for tp in t_params:
                                            await cur.execute(t_sql, tp)
                            except Exception as te_err:
                                logger.warning(f"전담 교사 매핑 복제 생략: {te_err}")

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

SCORING_TYPE_NAMES: Dict[int, str] = {
    1: "고정점수형",
    2: "등급환산형",
    3: "구간점수형",
    4: "시간계산형",
    5: "감점형",
    6: "가산점형",
    7: "최대점제한형",
    8: "최상위인정형",
}


@criteria_router.get("", summary="평가 기준 조회 (영역 및 항목) (API-03)", response_model=SrFormat)
@criteria_router.get("/", summary="평가 기준 조회 (영역 및 항목) (API-03)", response_model=SrFormat, include_in_schema=False)
@admin_router.get("/criteria", summary="평가 기준 조회 (영역 및 항목) (API-03) [Admin 별칭]", response_model=SrFormat)
@admin_router.get("/criteria/", summary="평가 기준 조회 (영역 및 항목) (API-03) [Admin 별칭]", response_model=SrFormat, include_in_schema=False)
async def get_criteria(
    year: Optional[int] = Query(None, description="조회 학년도 (미지정 시 활성/최신 학년도)"),
    grade: Optional[int] = Query(None, description="조회 학년 필터 (1, 2, 3) (미지정 시 전체 학년)"),
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
                return error_response(404, "ITEM_NOT_FOUND", "등록된 학년도가 없습니다.")
        else:
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "ITEM_NOT_FOUND", f"{target_year} 학년도가 존재하지 않습니다.")
            year_id = y_row["year_id"]

        # 영역 목록 조회 (학년 필터 적용 가능)
        area_sql = """
            SELECT area_id AS areaId, year_id AS yearId, grade, name, max_score AS maxScore,
                   subject, department
            FROM certification_areas
            WHERE year_id = %s
        """
        area_params: List[Any] = [year_id]
        if grade is not None:
            area_sql += " AND grade = %s"
            area_params.append(grade)
        area_sql += " ORDER BY grade ASC, area_id ASC"

        await cur.execute(area_sql, tuple(area_params))
        areas = await cur.fetchall()

        if not areas:
            return SrFormat(
                status_code=200,
                success=True,
                data={"year": target_year, "yearId": year_id, "areas": []},
            ).model_dump()

        area_ids = [(a.get("areaId") or a.get("area_id")) for a in areas]
        placeholders = ", ".join(["%s"] * len(area_ids))

        # 영역 전담 교사 N명 매핑 조회
        area_teachers_map: Dict[int, List[Dict[str, Any]]] = {}
        area_teacher_ids_map: Dict[int, List[int]] = {}
        try:
            await cur.execute(
                f"""
                SELECT cat.area_id, cat.teacher_id, t.name, t.subject, t.department
                FROM certification_area_teachers cat
                JOIN teachers t ON cat.teacher_id = t.teachers_id
                WHERE cat.area_id IN ({placeholders}) AND t.is_deleted = FALSE
                ORDER BY t.name ASC
                """,
                tuple(area_ids),
            )
            cat_rows = await cur.fetchall() or []
            for r in cat_rows:
                aid = r.get("area_id")
                tid = r.get("teacher_id")
                area_teacher_ids_map.setdefault(aid, []).append(tid)
                area_teachers_map.setdefault(aid, []).append({
                    "id": tid,
                    "teachersId": tid,
                    "name": r.get("name"),
                    "subject": r.get("subject"),
                    "department": r.get("department"),
                })
        except Exception:
            pass

        norm_areas = []
        for a in areas:
            a_id = a.get("areaId") or a.get("area_id")
            norm_areas.append({
                "areaId": a_id,
                "yearId": a.get("yearId") or a.get("year_id"),
                "grade": a.get("grade"),
                "name": a.get("name"),
                "subject": a.get("subject"),
                "department": a.get("department"),
                "maxScore": float(a.get("maxScore") if a.get("maxScore") is not None else a.get("max_score", 0.0)),
                "teacherIds": area_teacher_ids_map.get(a_id, []),
                "assignedTeachers": area_teachers_map.get(a_id, []),
            })

        await cur.execute(
            f"""
            SELECT 
                item_id AS itemId,
                area_id AS areaId,
                name,
                subject,
                department,
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
        items = await cur.fetchall() or []

        # 항목 전담 교사 N명 매핑 조회
        item_teachers_map: Dict[int, List[Dict[str, Any]]] = {}
        item_teacher_ids_map: Dict[int, List[int]] = {}
        if items:
            item_ids = [(it.get("itemId") or it.get("item_id")) for it in items]
            it_placeholders = ", ".join(["%s"] * len(item_ids))
            try:
                await cur.execute(
                    f"""
                    SELECT eit.item_id, eit.teacher_id, t.name, t.subject, t.department
                    FROM evaluation_item_teachers eit
                    JOIN teachers t ON eit.teacher_id = t.teachers_id
                    WHERE eit.item_id IN ({it_placeholders}) AND t.is_deleted = FALSE
                    ORDER BY t.name ASC
                    """,
                    tuple(item_ids),
                )
                eit_rows = await cur.fetchall() or []
                for r in eit_rows:
                    iid = r.get("item_id")
                    tid = r.get("teacher_id")
                    item_teacher_ids_map.setdefault(iid, []).append(tid)
                    item_teachers_map.setdefault(iid, []).append({
                        "id": tid,
                        "teachersId": tid,
                        "name": r.get("name"),
                        "subject": r.get("subject"),
                        "department": r.get("department"),
                    })
            except Exception:
                pass

    items_by_area: Dict[int, List[Dict[str, Any]]] = {}
    for it in items:
        it_area_id = it.get("areaId") or it.get("area_id")
        it_id = it.get("itemId") or it.get("item_id")
        sc_type_code = it.get("scoringType") or it.get("scoring_type")
        sc_type_name = SCORING_TYPE_NAMES.get(sc_type_code, "고정점수형") if isinstance(sc_type_code, int) else str(sc_type_code or "고정점수형")
        norm_it = {
            "itemId": it_id,
            "areaId": it_area_id,
            "name": it.get("name"),
            "subject": it.get("subject"),
            "department": it.get("department"),
            "maxScore": float(it.get("maxScore") if it.get("maxScore") is not None else it.get("max_score", 0.0)),
            "scoringType": sc_type_code,
            "scoringTypeName": sc_type_name,
            "targetGrade": it.get("targetGrade") or it.get("target_grade"),
            "requiresEvidence": bool(it.get("requiresEvidence") if "requiresEvidence" in it else it.get("requires_evidence", False)),
            "isActive": bool(it.get("isActive") if "isActive" in it else it.get("is_active", True)),
            "teacherIds": item_teacher_ids_map.get(it_id, []),
            "assignedTeachers": item_teachers_map.get(it_id, []),
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
@admin_router.patch("/criteria/areas/{area_id}", summary="평가 영역 배점/정보 수정 (API-03) [Admin 별칭]", response_model=SrFormat)
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
        return error_response(400, "VALIDATION_ERROR", "영역 배점은 0보다 크고 1000 이하여야 합니다.")

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT area_id, name, max_score, subject, department FROM certification_areas WHERE area_id = %s LIMIT 1", (area_id,))
        existing = await cur.fetchone()
        if not existing:
            return error_response(404, "ITEM_NOT_FOUND", "해당 영역을 찾을 수 없습니다.")

        if req.name is not None:
            raw_name = req.name.strip()
            if not raw_name:
                return error_response(400, "VALIDATION_ERROR", "영역 이름은 비어 있을 수 없습니다.")
            updated_name = raw_name
            if len(updated_name) > 30:
                return error_response(400, "VALIDATION_ERROR", "영역 이름은 최대 30자까지 가능합니다.")
        else:
            updated_name = existing["name"]

        if req.subject is not None:
            raw_subj = req.subject.strip()
            if raw_subj and len(raw_subj) > 40:
                return error_response(400, "VALIDATION_ERROR", "담당 교과명은 최대 40자까지 가능합니다.")
            updated_subject = raw_subj if raw_subj else None
        else:
            updated_subject = existing.get("subject")

        if req.department is not None:
            raw_dept = req.department.strip()
            if raw_dept and len(raw_dept) > 40:
                return error_response(400, "VALIDATION_ERROR", "담당 부서명은 최대 40자까지 가능합니다.")
            updated_department = raw_dept if raw_dept else None
        else:
            updated_department = existing.get("department")

        updated_score = new_score if new_score is not None else float(existing["max_score"])

        try:
            await cur.execute(
                "UPDATE certification_areas SET name = %s, max_score = %s, subject = %s, department = %s WHERE area_id = %s",
                (updated_name, updated_score, updated_subject, updated_department, area_id),
            )
        except Exception:
            await cur.execute(
                "UPDATE certification_areas SET name = %s, max_score = %s WHERE area_id = %s",
                (updated_name, updated_score, area_id),
            )

        tids = req.teacher_ids if req.teacher_ids is not None else req.teacherIds
        if tids is not None:
            try:
                await cur.execute("DELETE FROM certification_area_teachers WHERE area_id = %s", (area_id,))
                clean_tids = [tid for tid in set(tids) if isinstance(tid, int) and tid > 0]
                if clean_tids:
                    placeholders_t = ", ".join(["%s"] * len(clean_tids))
                    await cur.execute(
                        f"SELECT teachers_id FROM teachers WHERE teachers_id IN ({placeholders_t}) AND is_deleted = FALSE",
                        tuple(clean_tids),
                    )
                    valid_rows = await cur.fetchall() or []
                    valid_tids = [
                        (r[0] if isinstance(r, (list, tuple)) else r.get("teachers_id"))
                        for r in valid_rows
                    ]
                    for tid in valid_tids:
                        await cur.execute(
                            "INSERT INTO certification_area_teachers (area_id, teacher_id) VALUES (%s, %s)",
                            (area_id, tid),
                        )
            except Exception:
                pass

        assigned_teachers = []
        assigned_teacher_ids = []
        try:
            await cur.execute(
                """
                SELECT cat.teacher_id, t.name, t.subject, t.department
                FROM certification_area_teachers cat
                JOIN teachers t ON cat.teacher_id = t.teachers_id
                WHERE cat.area_id = %s AND t.is_deleted = FALSE
                ORDER BY t.name ASC
                """,
                (area_id,),
            )
            cat_rows = await cur.fetchall() or []
            for r in cat_rows:
                tid = r.get("teacher_id")
                assigned_teacher_ids.append(tid)
                assigned_teachers.append({
                    "id": tid,
                    "teachersId": tid,
                    "name": r.get("name"),
                    "subject": r.get("subject"),
                    "department": r.get("department"),
                })
        except Exception:
            pass

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "areaId": area_id,
            "name": updated_name,
            "maxScore": updated_score,
            "subject": updated_subject,
            "department": updated_department,
            "teacherIds": assigned_teacher_ids,
            "assignedTeachers": assigned_teachers,
        },
    ).model_dump()


@criteria_router.post("/areas/{area_id}/items", summary="평가 항목 추가 (API-03)", response_model=SrFormat)
@admin_router.post("/criteria/areas/{area_id}/items", summary="평가 항목 추가 (API-03) [Admin 별칭]", response_model=SrFormat)
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
        return error_response(400, "VALIDATION_ERROR", "항목 배점은 0보다 크고 1000 이하여야 합니다.")

    scoring_type = req.scoring_type if req.scoring_type is not None else (req.scoringType or 1)
    # [버그 3.11] targetGrade: 0 (전학년 공통) Falsy 연산 왜곡 방어
    target_grade = req.target_grade if req.target_grade is not None else (req.targetGrade if req.targetGrade is not None else 0)
    requires_ev = req.requires_evidence if req.requires_evidence is not None else (req.requiresEvidence if req.requiresEvidence is not None else True)
    raw_name = (req.name or "").strip()
    if not raw_name:
        return error_response(400, "VALIDATION_ERROR", "항목 이름은 필수입니다.")
    if len(raw_name) > 50:
        return error_response(400, "VALIDATION_ERROR", "항목 이름은 최대 50자까지 가능합니다.")
    clean_name = raw_name
    if len(clean_name) > 100:
        return error_response(400, "VALIDATION_ERROR", "항목 이름이 허용 길이를 초과했습니다.")

    if target_grade not in (0, 1, 2, 3):
        return error_response(400, "VALIDATION_ERROR", "대상 학년은 0(전학년), 1, 2, 3 중 하나여야 합니다.")

    raw_subj = (req.subject or "").strip() if req.subject is not None else None
    if raw_subj and len(raw_subj) > 40:
        return error_response(400, "VALIDATION_ERROR", "담당 교과명은 최대 40자까지 가능합니다.")
    clean_subj = raw_subj if raw_subj else None

    raw_dept = (req.department or "").strip() if req.department is not None else None
    if raw_dept and len(raw_dept) > 40:
        return error_response(400, "VALIDATION_ERROR", "담당 부서명은 최대 40자까지 가능합니다.")
    clean_dept = raw_dept if raw_dept else None

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT area_id FROM certification_areas WHERE area_id = %s LIMIT 1", (area_id,))
        if not await cur.fetchone():
            return error_response(404, "ITEM_NOT_FOUND", "해당 영역을 찾을 수 없습니다.")

        try:
            await cur.execute(
                """
                INSERT INTO evaluation_items (
                    area_id, name, subject, department, max_score, scoring_type, target_grade, requires_evidence, is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                """,
                (area_id, clean_name, clean_subj, clean_dept, max_score, scoring_type, target_grade, requires_ev),
            )
        except Exception:
            await cur.execute(
                """
                INSERT INTO evaluation_items (
                    area_id, name, department, max_score, scoring_type, target_grade, requires_evidence, is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                """,
                (area_id, clean_name, clean_dept, max_score, scoring_type, target_grade, requires_ev),
            )
        await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
        last_row = await cur.fetchone()
        item_id = last_row[0] if isinstance(last_row, (list, tuple)) else last_row["last_id"]

        tids = req.teacher_ids if req.teacher_ids is not None else req.teacherIds
        if tids:
            clean_tids = [tid for tid in set(tids) if isinstance(tid, int) and tid > 0]
            if clean_tids:
                try:
                    placeholders_t = ", ".join(["%s"] * len(clean_tids))
                    await cur.execute(
                        f"SELECT teachers_id FROM teachers WHERE teachers_id IN ({placeholders_t}) AND is_deleted = FALSE",
                        tuple(clean_tids),
                    )
                    valid_rows = await cur.fetchall() or []
                    valid_tids = [
                        (r[0] if isinstance(r, (list, tuple)) else r.get("teachers_id"))
                        for r in valid_rows
                    ]
                    for tid in valid_tids:
                        await cur.execute(
                            "INSERT INTO evaluation_item_teachers (item_id, teacher_id) VALUES (%s, %s)",
                            (item_id, tid),
                        )
                except Exception:
                    pass

        assigned_teachers = []
        assigned_teacher_ids = []
        try:
            await cur.execute(
                """
                SELECT eit.teacher_id, t.name, t.subject, t.department
                FROM evaluation_item_teachers eit
                JOIN teachers t ON eit.teacher_id = t.teachers_id
                WHERE eit.item_id = %s AND t.is_deleted = FALSE
                ORDER BY t.name ASC
                """,
                (item_id,),
            )
            eit_rows = await cur.fetchall() or []
            for r in eit_rows:
                tid = r.get("teacher_id")
                assigned_teacher_ids.append(tid)
                assigned_teachers.append({
                    "id": tid,
                    "teachersId": tid,
                    "name": r.get("name"),
                    "subject": r.get("subject"),
                    "department": r.get("department"),
                })
        except Exception:
            pass

    return JSONResponse(
        status_code=201,
        content=SrFormat(
            status_code=201,
            success=True,
            data={
                "itemId": item_id,
                "areaId": area_id,
                "name": clean_name,
                "subject": clean_subj,
                "department": clean_dept,
                "maxScore": max_score,
                "scoringType": scoring_type,
                "targetGrade": target_grade,
                "requiresEvidence": requires_ev,
                "teacherIds": assigned_teacher_ids,
                "assignedTeachers": assigned_teachers,
            },
        ).model_dump(),
    )


@criteria_router.patch("/items/{item_id}", summary="평가 항목 수정 (API-03)", response_model=SrFormat)
@admin_router.patch("/criteria/items/{item_id}", summary="평가 항목 수정 (API-03) [Admin 별칭]", response_model=SrFormat)
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
            return error_response(404, "ITEM_NOT_FOUND", "해당 평가 항목을 찾을 수 없습니다.")

        new_max_score = req.max_score if req.max_score is not None else (req.maxScore if req.maxScore is not None else float(existing.get("max_score", existing.get("maxScore", 10.0))))
        if new_max_score <= 0 or new_max_score > 1000:
            return error_response(400, "VALIDATION_ERROR", "항목 배점은 0보다 크고 1000 이하여야 합니다.")

        if req.name is not None:
            raw_name = req.name.strip()
            if not raw_name:
                return error_response(400, "VALIDATION_ERROR", "항목 이름은 비어 있을 수 없습니다.")
            if len(raw_name) > 50:
                return error_response(400, "VALIDATION_ERROR", "항목 이름은 최대 50자까지 가능합니다.")
            new_name = raw_name
            if len(new_name) > 100:
                return error_response(400, "VALIDATION_ERROR", "항목 이름이 허용 길이를 초과했습니다.")
        else:
            new_name = existing.get("name", "")

        if req.subject is not None:
            raw_subj = req.subject.strip()
            if raw_subj and len(raw_subj) > 40:
                return error_response(400, "VALIDATION_ERROR", "담당 교과명은 최대 40자까지 가능합니다.")
            new_subj = raw_subj if raw_subj else None
        else:
            new_subj = existing.get("subject")

        if req.department is not None:
            raw_dept = req.department.strip()
            if raw_dept and len(raw_dept) > 40:
                return error_response(400, "VALIDATION_ERROR", "담당 부서명은 최대 40자까지 가능합니다.")
            new_dept = raw_dept if raw_dept else None
        else:
            new_dept = existing.get("department")

        new_type = req.scoring_type if req.scoring_type is not None else (req.scoringType if req.scoringType is not None else existing.get("scoring_type", existing.get("scoringType", 1)))
        new_grade = req.target_grade if req.target_grade is not None else (req.targetGrade if req.targetGrade is not None else existing.get("target_grade", existing.get("targetGrade", 0)))
        if new_grade is not None and new_grade not in (0, 1, 2, 3):
            return error_response(400, "VALIDATION_ERROR", "대상 학년은 0(전학년), 1, 2, 3 중 하나여야 합니다.")
        new_req_ev = req.requires_evidence if req.requires_evidence is not None else (req.requiresEvidence if req.requiresEvidence is not None else existing.get("requires_evidence", existing.get("requiresEvidence", True)))
        new_active = req.is_active if req.is_active is not None else (req.isActive if req.isActive is not None else existing.get("is_active", existing.get("isActive", True)))

        try:
            await cur.execute(
                """
                UPDATE evaluation_items
                SET name = %s,
                    subject = %s,
                    department = %s,
                    max_score = %s,
                    scoring_type = %s,
                    target_grade = %s,
                    requires_evidence = %s,
                    is_active = %s
                WHERE item_id = %s
                """,
                (new_name, new_subj, new_dept, new_max_score, new_type, new_grade, new_req_ev, new_active, item_id),
            )
        except Exception:
            await cur.execute(
                """
                UPDATE evaluation_items
                SET name = %s,
                    department = %s,
                    max_score = %s,
                    scoring_type = %s,
                    target_grade = %s,
                    requires_evidence = %s,
                    is_active = %s
                WHERE item_id = %s
                """,
                (new_name, new_dept, new_max_score, new_type, new_grade, new_req_ev, new_active, item_id),
            )

        tids = req.teacher_ids if req.teacher_ids is not None else req.teacherIds
        if tids is not None:
            try:
                await cur.execute("DELETE FROM evaluation_item_teachers WHERE item_id = %s", (item_id,))
                clean_tids = [tid for tid in set(tids) if isinstance(tid, int) and tid > 0]
                if clean_tids:
                    placeholders_t = ", ".join(["%s"] * len(clean_tids))
                    await cur.execute(
                        f"SELECT teachers_id FROM teachers WHERE teachers_id IN ({placeholders_t}) AND is_deleted = FALSE",
                        tuple(clean_tids),
                    )
                    valid_rows = await cur.fetchall() or []
                    valid_tids = [
                        (r[0] if isinstance(r, (list, tuple)) else r.get("teachers_id"))
                        for r in valid_rows
                    ]
                    for tid in valid_tids:
                        await cur.execute(
                            "INSERT INTO evaluation_item_teachers (item_id, teacher_id) VALUES (%s, %s)",
                            (item_id, tid),
                        )
            except Exception:
                pass

        assigned_teachers = []
        assigned_teacher_ids = []
        try:
            await cur.execute(
                """
                SELECT eit.teacher_id, t.name, t.subject, t.department
                FROM evaluation_item_teachers eit
                JOIN teachers t ON eit.teacher_id = t.teachers_id
                WHERE eit.item_id = %s AND t.is_deleted = FALSE
                ORDER BY t.name ASC
                """,
                (item_id,),
            )
            eit_rows = await cur.fetchall() or []
            for r in eit_rows:
                tid = r.get("teacher_id")
                assigned_teacher_ids.append(tid)
                assigned_teachers.append({
                    "id": tid,
                    "teachersId": tid,
                    "name": r.get("name"),
                    "subject": r.get("subject"),
                    "department": r.get("department"),
                })
        except Exception:
            pass

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "itemId": item_id,
            "name": new_name,
            "subject": new_subj,
            "department": new_dept,
            "maxScore": new_max_score,
            "scoringType": new_type,
            "targetGrade": new_grade,
            "requiresEvidence": bool(new_req_ev),
            "isActive": bool(new_active),
            "teacherIds": assigned_teacher_ids,
            "assignedTeachers": assigned_teachers,
        },
    ).model_dump()


@criteria_router.delete("/items/{item_id}", summary="평가 항목 삭제 (소프트 삭제) (API-03)", response_model=SrFormat)
@admin_router.delete("/criteria/items/{item_id}", summary="평가 항목 삭제 (소프트 삭제) (API-03) [Admin 별칭]", response_model=SrFormat)
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
            return error_response(404, "ITEM_NOT_FOUND", "해당 평가 항목을 찾을 수 없습니다.")

    return SrFormat(
        status_code=200,
        success=True,
        data={"itemId": item_id, "deleted": True},
    ).model_dump()


