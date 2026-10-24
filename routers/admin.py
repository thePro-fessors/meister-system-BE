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
from core.security import error_response, get_current_user, normalize_role, revoke_all_user_tokens
from database import get_db, get_redis
from sr_format import Error, SrFormat

# ------------------------------------------------------------------------------
# 라우터 정의
# ------------------------------------------------------------------------------
from routers.admin_criteria import (
    years_router,
    criteria_router,
    admin_router as _admin_criteria_router,
    copy_year_criteria,
    CreateYearRequest,
    PatchYearRequest,
    PatchAreaRequest,
    CreateItemRequest,
    UpdateItemRequest,
    RecalculateBatchRequest,
)
from routers.admin_batch import admin_router as _admin_batch_router

admin_router = APIRouter(prefix="/api/admin", tags=["admin"])
admin_router.include_router(_admin_criteria_router)
admin_router.include_router(_admin_batch_router)


def check_admin_access(current_user: Dict[str, Any]) -> Optional[JSONResponse]:
    """관리자 권한 필수 검증 의존성 헬퍼"""
    if normalize_role(current_user.get("role")) != "admin":
        return error_response(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "관리자 권한이 필요합니다.",
        )
    return None

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
                return error_response(404, "ITEM_NOT_FOUND", "등록된 학년도가 없습니다.")
            target_year = y_row["year"]
            year_id = y_row["year_id"]
        else:
            target_year = year
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "ITEM_NOT_FOUND", f"{target_year} 학년도가 존재하지 않습니다.")
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

        # 4. 학생별 인증 상태 집계 (statusCounts)
        await cur.execute(
            """
            SELECT st.student_id, sar.grade
            FROM students st
            JOIN student_academic_records sar ON st.student_id = sar.student_id
            WHERE sar.year_id = %s AND st.is_deleted = FALSE
            """,
            (year_id,),
        )
        students = await cur.fetchall() or []

        await cur.execute(
            """
            SELECT ca.area_id, ca.grade, ca.name AS area_name, ca.max_score AS area_max_score,
                   ei.item_id, ei.name AS item_name, ei.max_score AS item_max_score, ei.scoring_type, ei.is_active
            FROM certification_areas ca
            LEFT JOIN evaluation_items ei ON ca.area_id = ei.area_id
            WHERE ca.year_id = %s
            ORDER BY ca.grade ASC, ca.area_id ASC, ei.item_id ASC
            """,
            (year_id,),
        )
        criteria_rows = await cur.fetchall() or []

        cert_counts = {"인증 가능": 0, "검토중": 0, "보완 필요": 0, "미달성": 0}
        try:
            areas_by_grade: Dict[int, Dict[int, Dict[str, Any]]] = {}
            for row in criteria_rows:
                if not isinstance(row, dict):
                    continue
                g = row.get("grade", 1)
                aid = row.get("area_id") or row.get("id")
                if aid is None:
                    continue
                a_name = row.get("area_name") or row.get("name") or ""
                a_max = float(row.get("area_max_score") or row.get("max_score") or 0)
                if g not in areas_by_grade:
                    areas_by_grade[g] = {}
                if aid not in areas_by_grade[g]:
                    areas_by_grade[g][aid] = {
                        "area_id": aid,
                        "id": aid,
                        "area": a_name,
                        "name": a_name,
                        "max_score": a_max,
                        "maxScore": a_max,
                        "items": [],
                    }
                if row.get("item_id"):
                    areas_by_grade[g][aid]["items"].append({
                        "item_id": row["item_id"],
                        "id": row["item_id"],
                        "name": row.get("item_name") or row.get("name") or "",
                        "item_max_score": float(row.get("item_max_score") or row.get("max_score") or 0),
                        "maxScore": float(row.get("item_max_score") or row.get("max_score") or 0),
                        "scoring_type": row.get("scoring_type"),
                        "scoringType": row.get("scoring_type"),
                        "is_active": bool(row.get("is_active", True)),
                        "isActive": bool(row.get("is_active", True)),
                    })

            areas_list_by_grade = {g: list(a_dict.values()) for g, a_dict in areas_by_grade.items()}

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

            start_date_str = f"{target_year}-03-01"
            end_date_str = f"{target_year + 1}-03-01"
            await cur.execute(
                """
                SELECT merits_point_id, student_id, type, points, related_area
                FROM merits
                WHERE is_reflected = TRUE AND is_deleted = FALSE
                  AND (occurred_at IS NULL OR (occurred_at >= %s AND occurred_at < %s))
                """,
                (start_date_str, end_date_str),
            )
            all_merits = await cur.fetchall() or []

            subs_by_student: Dict[int, List[Dict[str, Any]]] = {}
            for s in all_submissions:
                if isinstance(s, dict) and "student_id" in s:
                    subs_by_student.setdefault(s["student_id"], []).append(s)

            merits_by_student: Dict[int, List[Dict[str, Any]]] = {}
            for m in all_merits:
                if isinstance(m, dict) and "student_id" in m:
                    merits_by_student.setdefault(m["student_id"], []).append(m)

            if students:
                for st in students:
                    sid = st.get("student_id") if isinstance(st, dict) else st[0]
                    st_grade = st.get("grade", 1) if isinstance(st, dict) else 1
                    st_areas = areas_list_by_grade.get(st_grade, [])
                    calc = calculate_student_certification(st_areas, subs_by_student.get(sid, []), merits_by_student.get(sid, []))
                    st_status = calc.get("certStatus", "미달성")
                    cert_counts[st_status] = cert_counts.get(st_status, 0) + 1
            elif total_students > 0:
                pending_total = (sub_stats.get("submitted_count", 0) or 0) + (sub_stats.get("reviewing_count", 0) or 0)
                if pending_total > 0:
                    cert_counts["검토중"] = min(total_students, pending_total)
                rem = max(0, total_students - cert_counts["검토중"])
                cert_counts["미달성"] = rem
        except Exception:
            pending_total = (sub_stats.get("submitted_count", 0) or 0) + (sub_stats.get("reviewing_count", 0) or 0)
            if pending_total > 0:
                cert_counts["검토중"] = min(total_students, pending_total)
            rem = max(0, total_students - cert_counts["검토중"])
            cert_counts["미달성"] = rem

        status_counts = {
            "certified": cert_counts.get("인증 가능", 0),
            "supplement": cert_counts.get("보완 필요", 0),
            "reviewing": cert_counts.get("검토중", 0),
            "uncertified": cert_counts.get("미달성", 0),
        }

        sub_counts_dict = {
            "total": sub_stats.get("total_submissions", 0),
            "pending": (sub_stats.get("submitted_count", 0) or 0) + (sub_stats.get("reviewing_count", 0) or 0),
            "approved": sub_stats.get("approved_count", 0) or 0,
            "rejected": sub_stats.get("rejected_count", 0) or 0,
            "resubmitRequested": sub_stats.get("resubmit_count", 0) or 0,
        }

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "year": target_year,
            "yearId": year_id,
            "totalStudents": total_students,
            "statusCounts": status_counts,
            "submissions": sub_counts_dict,
            "submissionCounts": sub_counts_dict,
            "areaDistribution": area_distribution,
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
    6: "이의신청",
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
        return error_response(400, "INVALID_LOG_TYPE", "log_type은 'ALL', 'SUBMISSION', 'MERIT' 중 하나여야 합니다.")

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
            "log_id": r["original_log_id"],
            "user_id": r.get("modifier_uuid") or (r.get("modifier_name") or "알 수 없음"),
            "action": r["action_type"],
            "created_at": c_at_str,
            "details": r.get("reason") or "",
        }
        formatted_items.append(log_item)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "logs": formatted_items,
            "total": total_count,
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

def _sanitize_spreadsheet_cell(val: Any) -> Any:
    """XLSX 및 CSV 셀 수식 인젝션(Formula Injection) 방어: 선행 공백/탭 뒤 수식 기호 포함"""
    if val is None or isinstance(val, (int, float, bool)):
        return val
    s = str(val)
    if s.lstrip(" \t\r\n").startswith(("=", "+", "-", "@", "|")) or s.startswith(("\t", "\r")):
        return f"'{s}"
    return s


def _escape_csv_val(val: Any) -> str:
    """CSV 셀 내 따옴표, 콤마 및 수식 인젝션 방어용 이스케이프"""
    if val is None:
        return ""
    s = str(_sanitize_spreadsheet_cell(val)).replace('"', '""')
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
        return error_response(400, "INVALID_FORMAT", "지원하지 않는 출력 형식입니다. 'csv', 'xlsx', 'pdf' 중 하나여야 합니다.")

    # 2. 대상 학년도 결정
    target_year = year
    year_id = None
    async with conn.cursor(cursor=DictCursor) as cur:
        if target_year is None:
            await cur.execute("SELECT year_id, year FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "ACADEMIC_YEAR_NOT_FOUND", "등록된 학년도가 없습니다.")
            year_id = y_row["year_id"]
            target_year = y_row["year"]
        else:
            await cur.execute("SELECT year_id, year FROM academic_years WHERE year = %s", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "ACADEMIC_YEAR_NOT_FOUND", f"{target_year} 학년도가 존재하지 않습니다.")
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

        start_date_str = f"{target_year}-03-01"
        end_date_str = f"{target_year + 1}-03-01"
        await cur.execute(
            """
            SELECT merits_point_id, student_id, type, points, related_area
            FROM merits
            WHERE is_reflected = TRUE AND is_deleted = FALSE
              AND (occurred_at IS NULL OR (occurred_at >= %s AND occurred_at < %s))
            """,
            (start_date_str, end_date_str),
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
            "point_total": calc.get("totalMeritScore", calc.get("pointTotal", 0.0)),
            "area_scores": area_score_map,
        })

    # 정렬된 영역 명칭 목록
    area_names = [a["name"] for a in areas_list]

    # ==========================================================================
    # 포맷별 파일 생성 및 스트리밍 응답
    # ==========================================================================

    # 8. CSV 형식 출력
    if norm_format == "csv":
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
        import openpyxl
        from openpyxl.chart import BarChart, Reference

        wb = openpyxl.Workbook()

        # Sheet 1: 학생별 인증 상세
        ws_students = wb.active
        ws_students.title = "학생별 인증 결과"

        headers = ["학번", "학년", "반", "번호", "이름", "이메일", "취득총점", "인증상태", "상벌점합계", "검토대기건수"] + area_names
        ws_students.append([_sanitize_spreadsheet_cell(h) for h in headers])

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
            ws_students.append([_sanitize_spreadsheet_cell(v) for v in row])

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


# ==============================================================================
# 10. 관리자 데이터 정합성 검증 및 재계산 배치 (4.10) (POST /api/admin/recalculate)
# ==============================================================================

@admin_router.post("/recalculate", summary="관리자 수동 데이터 정합성 검증 및 재계산 배치 (4.10)", response_model=SrFormat)
async def recalculate_student_certifications(
    req: Optional[RecalculateBatchRequest] = None,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    관리자 수동 데이터 정합성 검증 및 재계산 배치 (4.10).
    기준 변경 또는 대량 제출/상벌점 변동 시 전교생 총점 및 인증 상태 배치 동기화,
    배점 한도 초과/음수 점수 등 이상치(Anomalies) 탐지 및 자동 정규화 지원.
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    payload = req or RecalculateBatchRequest()
    import time
    start_time = time.perf_counter()
    target_year = payload.year
    filter_grade = payload.grade
    filter_student_id = payload.studentId or payload.student_id
    is_dry_run = bool(payload.dryRun or payload.dry_run)
    fix_anomalies = bool(payload.fixAnomalies or payload.fix_anomalies)

    cur_kwargs = {"cursor": DictCursor} if DictCursor else {}
    async with conn.cursor(**cur_kwargs) as cur:
        # 1. 대상 학년도 식별
        if target_year is not None:
            await cur.execute("SELECT year_id, year FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "YEAR_NOT_FOUND", f"{target_year}학년도를 찾을 수 없습니다.")
            year_id = y_row["year_id"]
            target_year = y_row["year"]
        else:
            await cur.execute("SELECT year_id, year FROM academic_years WHERE is_activated = TRUE LIMIT 1")
            y_row = await cur.fetchone()
            if not y_row:
                await cur.execute("SELECT year_id, year FROM academic_years ORDER BY year DESC LIMIT 1")
                y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "YEAR_NOT_FOUND", "등록된 학년도가 없습니다.")
            year_id = y_row["year_id"]
            target_year = y_row["year"]

        # 2. 평가 기준(영역 및 평가 항목) 조회
        await cur.execute(
            """
            SELECT ca.area_id, ca.grade, ca.name AS area_name, ca.max_score AS area_max_score,
                   ei.item_id, ei.name AS item_name, ei.max_score AS item_max_score, ei.scoring_type, ei.is_active
            FROM certification_areas ca
            LEFT JOIN evaluation_items ei ON ca.area_id = ei.area_id
            WHERE ca.year_id = %s
            ORDER BY ca.grade ASC, ca.area_id ASC, ei.item_id ASC
            """,
            (year_id,),
        )
        criteria_rows = await cur.fetchall() or []

        areas_seen_by_grade: Dict[int, Dict[int, Dict[str, Any]]] = {}
        for row in criteria_rows:
            g = row.get("grade", 1)
            aid = row["area_id"]
            if g not in areas_seen_by_grade:
                areas_seen_by_grade[g] = {}
            if aid not in areas_seen_by_grade[g]:
                areas_seen_by_grade[g][aid] = {
                    "area_id": aid,
                    "name": row.get("area_name"),
                    "max_score": float(row.get("area_max_score") or 0.0),
                    "grade": g,
                }

        areas_by_grade: Dict[int, List[Dict[str, Any]]] = {
            g: list(adict.values()) for g, adict in areas_seen_by_grade.items()
        }

        # 3. 대상 학생 목록 조회
        st_sql = """
            SELECT st.student_id, st.name, st.email,
                   sar.grade, sar.class AS class_no, sar.number
            FROM student_academic_records sar
            JOIN students st ON sar.student_id = st.student_id
            WHERE sar.year_id = %s AND st.is_deleted = FALSE
        """
        st_params: List[Any] = [year_id]
        if filter_grade is not None:
            st_sql += " AND sar.grade = %s"
            st_params.append(filter_grade)
        if filter_student_id is not None:
            st_sql += " AND st.student_id = %s"
            st_params.append(filter_student_id)
        st_sql += " ORDER BY sar.grade ASC, sar.class ASC, sar.number ASC"

        await cur.execute(st_sql, tuple(st_params))
        students = await cur.fetchall() or []

        # 4. 제출 증빙 목록 조회
        sub_sql = """
            SELECT s.submission_id, s.student_id, s.item_id, s.status_code, s.granted_score,
                   ei.area_id, ei.scoring_type, ei.max_score AS item_max_score, ei.name AS item_name, ei.is_active
            FROM submissions s
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id
            WHERE ca.year_id = %s AND s.is_deleted = FALSE
        """
        sub_params: List[Any] = [year_id]
        if filter_student_id is not None:
            sub_sql += " AND s.student_id = %s"
            sub_params.append(filter_student_id)

        await cur.execute(sub_sql, tuple(sub_params))
        all_submissions = await cur.fetchall() or []

        # 5. 당해 학사년도(3.1~익년 3.1) 상벌점 조회
        start_date_str = f"{target_year}-03-01"
        end_date_str = f"{target_year + 1}-03-01"
        merit_sql = """
            SELECT merits_point_id, student_id, type, points, related_area
            FROM merits
            WHERE is_reflected = TRUE AND is_deleted = FALSE
              AND (occurred_at IS NULL OR (occurred_at >= %s AND occurred_at < %s))
        """
        merit_params: List[Any] = [start_date_str, end_date_str]
        if filter_student_id is not None:
            merit_sql += " AND student_id = %s"
            merit_params.append(filter_student_id)

        await cur.execute(merit_sql, tuple(merit_params))
        all_merits = await cur.fetchall() or []

        # 6. 이상치(Anomalies) 검출 및 자동 보정
        anomalies: List[Dict[str, Any]] = []
        fixed_count = 0
        admin_uuid = current_user.get("uuid") or current_user.get("sub") or "admin"

        for sub in all_submissions:
            sub_id = sub["submission_id"]
            st_id = sub["student_id"]
            status_code = sub.get("status_code", 1)
            g_score = float(sub.get("granted_score") or 0.0)
            it_max = float(sub.get("item_max_score") or 0.0)
            it_active = bool(sub.get("is_active", True))
            it_name = sub.get("item_name", "미확인 항목")

            # 승인 완료(3) 상태인 건의 점수 정합성 검증
            if status_code == 3:
                issue = None
                target_fix = None
                if it_max > 0 and g_score > it_max:
                    issue = "EXCEEDED_MAX_SCORE"
                    target_fix = it_max
                elif g_score < 0:
                    issue = "NEGATIVE_SCORE"
                    target_fix = 0.0
                elif not it_active:
                    issue = "INACTIVE_ITEM_SCORED"

                if issue:
                    anomalies.append({
                        "submissionId": sub_id,
                        "studentId": st_id,
                        "itemName": it_name,
                        "grantedScore": g_score,
                        "itemMaxScore": it_max,
                        "issueType": issue,
                    })

                    if fix_anomalies and not is_dry_run and target_fix is not None:
                        await cur.execute(
                            "UPDATE submissions SET granted_score = %s WHERE submission_id = %s",
                            (target_fix, sub_id),
                        )
                        await cur.execute(
                            """
                            INSERT INTO submissions_logs (submission_id, action, reviewer_id, old_score, granted_score, teacher_comment, created_at)
                            VALUES (%s, 'SCORE_MODIFY', %s, %s, %s, %s, NOW())
                            """,
                            (
                                sub_id,
                                admin_uuid,
                                g_score,
                                target_fix,
                                f"[관리자 재계산 배치 자동 보정] {issue} (기존 {g_score}점 -> {target_fix}점)",
                            ),
                        )
                        sub["granted_score"] = target_fix
                        fixed_count += 1

        if fix_anomalies and not is_dry_run and fixed_count > 0:
            if hasattr(conn, "commit"):
                await conn.commit()

        # 7. 학생별 데이터 그룹핑 및 calculate_student_certification 배치 실행
        subs_by_student: Dict[int, List[Dict[str, Any]]] = {}
        for s in all_submissions:
            sid = s["student_id"]
            subs_by_student.setdefault(sid, []).append(s)

        merits_by_student: Dict[int, List[Dict[str, Any]]] = {}
        for m in all_merits:
            sid = m["student_id"]
            merits_by_student.setdefault(sid, []).append(m)

        cert_status_counts = {"인증 가능": 0, "검토중": 0, "보완 필요": 0}
        grade_counts = {"S": 0, "A": 0, "B": 0, "미달성": 0}
        total_scores_sum = 0.0
        recalculated_students: List[Dict[str, Any]] = []

        for st in students:
            sid = st["student_id"]
            st_grade = st.get("grade", 1)
            areas_for_student = areas_by_grade.get(st_grade, [])
            st_subs = subs_by_student.get(sid, [])
            st_merits = merits_by_student.get(sid, [])

            calc = calculate_student_certification(areas_for_student, st_subs, st_merits)
            st_status = calc.get("certStatus", "보완 필요")
            st_total = float(calc.get("totalScore", 0.0))
            st_merit = float(calc.get("totalMeritScore", 0.0))
            st_pending = int(calc.get("pendingCount", 0))

            total_scores_sum += st_total
            cert_status_counts[st_status] = cert_status_counts.get(st_status, 0) + 1

            if st_status == "인증 가능":
                grade_counts["S" if st_total >= 90.0 else "A"] += 1
            elif st_status == "검토중":
                grade_counts["B"] += 1
            else:
                grade_counts["미달성"] += 1

            if len(recalculated_students) < 50:
                recalculated_students.append({
                    "studentId": sid,
                    "name": st["name"],
                    "grade": st_grade,
                    "classNo": st.get("class_no"),
                    "number": st.get("number"),
                    "totalScore": round(st_total, 1),
                    "certStatus": st_status,
                    "totalMeritScore": round(st_merit, 1),
                    "pendingCount": st_pending,
                })

        total_st_cnt = len(students)
        avg_score = round(total_scores_sum / total_st_cnt, 1) if total_st_cnt > 0 else 0.0
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        return SrFormat(
            status_code=200,
            success=True,
            data={
                "targetYear": target_year,
                "yearId": year_id,
                "totalProcessedStudents": total_st_cnt,
                "processed_students": total_st_cnt,
                "recalculated_scores": total_st_cnt,
                "duration_ms": duration_ms,
                "averageTotalScore": avg_score,
                "certStatusCounts": cert_status_counts,
                "gradeCounts": grade_counts,
                "anomaliesCount": len(anomalies),
                "anomaliesFixedCount": fixed_count,
                "anomaliesFound": anomalies[:100],
                "isDryRun": is_dry_run,
                "filter": {
                    "year": target_year,
                    "grade": filter_grade,
                    "studentId": filter_student_id,
                },
                "sampleStudents": recalculated_students,
            },
        ).model_dump()


# ------------------------------------------------------------------------------
# 관리자 난수 초대코드 발급 및 관리 API
# ------------------------------------------------------------------------------
class CreateInvitationRequest(BaseModel):
    role: str = Field(..., description="대상 역할 (student/학생, teacher/교사, admin/관리자)")
    valid_days: int = Field(default=1, ge=1, le=365, alias="validDays", description="유효기간(일), 기본 1일")
    max_uses: int = Field(default=1, ge=1, le=10000, alias="maxUses", description="사용 횟수, 기본 1회")
    year: Optional[int] = Field(default=None, description="대상 학년도 (생략 시 활성 학년도)")

    model_config = {"populate_by_name": True}


def _normalize_role(role_input: str) -> Tuple[str, str, int]:
    r = (role_input or "").strip().lower()
    if r in ("student", "학생", "stu", "0"):
        return "student", "STU", 0
    if r in ("teacher", "교사", "선생님", "tch", "1"):
        return "teacher", "TCH", 1
    if r in ("admin", "관리자", "adm", "2"):
        return "admin", "ADM", 2
    raise ValueError("지원하지 않는 역할입니다. (학생/교사/관리자 중 선택)")


@admin_router.post("/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation_code(
    req: CreateInvitationRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    conn: Any = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    admin_guard = check_admin_access(current_user)
    if admin_guard:
        return admin_guard

    try:
        role_name, role_prefix, role_code = _normalize_role(req.role)
    except ValueError as e:
        return error_response(status.HTTP_400_BAD_REQUEST, "VALIDATION_ERROR", str(e))

    target_year = req.year if req.year and (2000 <= req.year <= 2100) else await get_current_year(conn)
    if not target_year:
        target_year = datetime.now().year

    # 난수 초대코드 규격: XXX-XXXX-XXXXXX
    # 1. 첫 3자리 : 학생(STU), 교사(TCH), 관리자(ADM)
    # 2. 중간 4자리 : 16진수 학년도(2026학년도면 07EA)
    # 3. 뒷 6자리 : UUID v4로 생성된 UUID 중 맨 뒤 6자
    hex_year = f"{int(target_year):04X}"
    suffix = uuid.uuid4().hex[-6:].upper()
    code = f"{role_prefix}-{hex_year}-{suffix}"

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=req.valid_days)
    ttl_seconds = int(req.valid_days * 86400)

    payload = {
        "code": code,
        "role": role_name,
        "role_prefix": role_prefix,
        "target_role": role_code,
        "year": int(target_year),
        "max_uses": req.max_uses,
        "remaining_uses": req.max_uses,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_by": current_user.get("id") or current_user.get("user_id") or "admin",
    }

    await redis.setex(f"invitation_code:{code}", ttl_seconds, json.dumps(payload, ensure_ascii=False))
    await redis.sadd("invitation_codes:active", code)

    return SrFormat(status_code=201, success=True, data=payload).model_dump()


@admin_router.get("/invitations")
async def list_invitation_codes(
    current_user: Dict[str, Any] = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
):
    admin_guard = check_admin_access(current_user)
    if admin_guard:
        return admin_guard

    active_codes = await redis.smembers("invitation_codes:active")
    results = []
    for code_raw in active_codes:
        code_str = code_raw.decode("utf-8") if isinstance(code_raw, bytes) else str(code_raw)
        raw_val = await redis.get(f"invitation_code:{code_str}")
        if not raw_val:
            await redis.srem("invitation_codes:active", code_str)
            continue
        try:
            val_str = raw_val.decode("utf-8") if isinstance(raw_val, bytes) else str(raw_val)
            item = json.loads(val_str)
            results.append(item)
        except Exception:
            continue

    results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return SrFormat(
        status_code=200,
        success=True,
        data={"invitations": results, "total": len(results)},
    ).model_dump()


@admin_router.delete("/invitations/{code}")
async def revoke_invitation_code(
    code: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
):
    admin_guard = check_admin_access(current_user)
    if admin_guard:
        return admin_guard

    clean_code = (code or "").strip().upper()
    deleted = await redis.delete(f"invitation_code:{clean_code}")
    await redis.srem("invitation_codes:active", clean_code)

    if not deleted:
        return error_response(status.HTTP_404_NOT_FOUND, "NOT_FOUND", "존재하지 않거나 이미 만료된 초대 코드입니다.")

    return SrFormat(
        status_code=200,
        success=True,
        data={"revokedCode": clean_code},
    ).model_dump()


# ==============================================================================
# 전체 유저 탐색 및 관리 (소프트 삭제, 복원, 영구 삭제)
# ==============================================================================

@admin_router.get("/users")
async def list_admin_users(
    search: Optional[str] = Query(None, description="아이디, 이름, 이메일 검색"),
    role: Optional[str] = Query("all", description="student, teacher, admin, all"),
    status_filter: Optional[str] = Query("all", alias="status", description="active, deleted, all"),
    page: int = Query(1, ge=1, description="페이지 번호"),
    limit: int = Query(20, ge=1, le=100, description="페이지당 개수"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    conn = Depends(get_db),
):
    admin_guard = check_admin_access(current_user)
    if admin_guard:
        return admin_guard

    offset = (page - 1) * limit
    where_clauses: List[str] = []
    params: List[Any] = []

    # 1. role 필터
    clean_role = (role or "all").strip().lower()
    if clean_role in ("student", "0"):
        where_clauses.append("u.role = %s")
        params.append(0)
    elif clean_role in ("teacher", "1"):
        where_clauses.append("u.role = %s")
        params.append(1)
    elif clean_role in ("admin", "2"):
        where_clauses.append("u.role = %s")
        params.append(2)
    elif clean_role != "all":
        return error_response(status.HTTP_400_BAD_REQUEST, "INVALID_ROLE", "유효하지 않은 역할 필터입니다. (all, student, teacher, admin)")

    # 2. status 필터
    clean_status = (status_filter or "all").strip().lower()
    if clean_status == "active":
        where_clauses.append("u.is_deleted = FALSE")
    elif clean_status == "deleted":
        where_clauses.append("u.is_deleted = TRUE")
    elif clean_status != "all":
        return error_response(status.HTTP_400_BAD_REQUEST, "INVALID_STATUS", "유효하지 않은 상태 필터입니다. (all, active, deleted)")

    # 3. search 필터
    if search and search.strip():
        kw = f"%{search.strip()}%"
        where_clauses.append("(u.id LIKE %s OR u.email LIKE %s OR st.name LIKE %s OR t.name LIKE %s)")
        params.extend([kw, kw, kw, kw])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    async with conn.cursor(cursor=DictCursor) as cur:
        # 전체 필터 일치 수
        count_sql = f"""
            SELECT COUNT(DISTINCT u.uuid) AS total
            FROM users u
            LEFT JOIN students st ON st.uuid = u.uuid
            LEFT JOIN teachers t ON t.uuid = u.uuid
            {where_sql}
        """
        await cur.execute(count_sql, tuple(params))
        total_row = await cur.fetchone()
        total = total_row["total"] if total_row else 0

        # 삭제 대기 인원 총 수 (전체 시스템 기준)
        await cur.execute("SELECT COUNT(*) AS deleted_count FROM users WHERE is_deleted = TRUE")
        del_row = await cur.fetchone()
        deleted_count = del_row["deleted_count"] if del_row else 0

        # 유저 목록 조회
        data_sql = f"""
            SELECT 
                u.uuid,
                u.id,
                u.email,
                u.role,
                u.created_at,
                u.is_deleted,
                st.student_id,
                st.name AS student_name,
                sar.grade AS student_grade,
                sar.class AS student_class,
                sar.number AS student_number,
                t.teachers_id,
                t.name AS teacher_name,
                t.grade AS teacher_grade,
                t.class AS teacher_class,
                t.subject AS teacher_subject,
                t.department AS teacher_department,
                t.position AS teacher_position
            FROM users u
            LEFT JOIN students st ON st.uuid = u.uuid
            LEFT JOIN (
                SELECT sar1.student_id, sar1.grade, sar1.class, sar1.number
                FROM student_academic_records sar1
                INNER JOIN (
                    SELECT student_id, MAX(record_id) AS max_rec
                    FROM student_academic_records
                    GROUP BY student_id
                ) sar_max ON sar1.student_id = sar_max.student_id AND sar1.record_id = sar_max.max_rec
            ) sar ON sar.student_id = st.student_id
            LEFT JOIN teachers t ON t.uuid = u.uuid
            {where_sql}
            ORDER BY u.created_at DESC, u.uuid DESC
            LIMIT %s OFFSET %s
        """
        await cur.execute(data_sql, tuple(params + [limit, offset]))
        rows = await cur.fetchall()

    users_list = []
    for r in (rows or []):
        u_role = r.get("role")
        role_str = "student" if u_role == 0 else ("teacher" if u_role == 1 else "admin")

        st_name = r.get("student_name")
        t_name = r.get("teacher_name")
        if u_role == 0 and st_name:
            name = st_name
        elif u_role in (1, 2) and t_name:
            name = t_name
        else:
            name = st_name or t_name or r.get("id") or "사용자"

        extra: Dict[str, Any] = {}
        if u_role == 0:
            extra = {
                "studentId": r.get("student_id"),
                "grade": r.get("student_grade"),
                "class": r.get("student_class"),
                "number": r.get("student_number"),
            }
        elif u_role == 1:
            extra = {
                "teachersId": r.get("teachers_id"),
                "grade": r.get("teacher_grade"),
                "class": r.get("teacher_class"),
                "subject": r.get("teacher_subject"),
                "department": r.get("teacher_department"),
                "position": r.get("teacher_position"),
            }
        elif u_role == 2:
            if r.get("teachers_id"):
                extra = {
                    "teachersId": r.get("teachers_id"),
                    "grade": r.get("teacher_grade"),
                    "class": r.get("teacher_class"),
                    "subject": r.get("teacher_subject"),
                    "department": r.get("teacher_department"),
                    "position": r.get("teacher_position"),
                }

        raw_created = r.get("created_at")
        if isinstance(raw_created, datetime):
            created_str = raw_created.isoformat()
        elif raw_created:
            created_str = str(raw_created)
        else:
            created_str = ""

        is_del = bool(r.get("is_deleted"))

        users_list.append({
            "uuid": r.get("uuid"),
            "id": r.get("id"),
            "name": name,
            "email": r.get("email"),
            "role": role_str,
            "role_code": u_role,
            "is_deleted": is_del,
            "isDeleted": is_del,
            "created_at": created_str,
            "createdAt": created_str,
            "extra": extra,
        })

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "users": users_list,
            "total": total,
            "deletedCount": deleted_count,
            "deleted_count": deleted_count,
            "page": page,
            "limit": limit,
        },
    ).model_dump()


@admin_router.patch("/users/{user_uuid}/soft-delete")
async def soft_delete_user(
    user_uuid: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    conn = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    admin_guard = check_admin_access(current_user)
    if admin_guard:
        return admin_guard

    caller_uuid = current_user.get("uuid") or current_user.get("sub")
    clean_uuid = (user_uuid or "").strip()
    if caller_uuid and str(caller_uuid).strip() == clean_uuid:
        return error_response(
            status.HTTP_400_BAD_REQUEST,
            "SELF_DELETION_PROHIBITED",
            "관리자 본인의 계정은 삭제할 수 없습니다.",
        )

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT uuid, id, role, is_deleted FROM users WHERE uuid = %s LIMIT 1", (clean_uuid,))
        target_user = await cur.fetchone()

    if not target_user:
        return error_response(status.HTTP_404_NOT_FOUND, "USER_NOT_FOUND", "해당 유저를 찾을 수 없습니다.")

    caller_id = current_user.get("id") or current_user.get("user_id")
    if caller_id and caller_id == target_user.get("id"):
        return error_response(
            status.HTTP_400_BAD_REQUEST,
            "SELF_DELETION_PROHIBITED",
            "관리자 본인의 계정은 삭제할 수 없습니다.",
        )

    try:
        if hasattr(conn, "autocommit"):
            await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("UPDATE users SET is_deleted = TRUE WHERE uuid = %s", (clean_uuid,))
            await cur.execute("UPDATE students SET is_deleted = TRUE WHERE uuid = %s", (clean_uuid,))
            await cur.execute("UPDATE teachers SET is_deleted = TRUE WHERE uuid = %s", (clean_uuid,))
        if hasattr(conn, "commit"):
            await conn.commit()
    except Exception:
        if hasattr(conn, "rollback"):
            await conn.rollback()
        raise
    finally:
        if hasattr(conn, "autocommit"):
            await conn.autocommit(True)

    try:
        await revoke_all_user_tokens(redis, clean_uuid)
    except Exception:
        pass

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "uuid": clean_uuid,
            "is_deleted": True,
            "isDeleted": True,
            "message": "유저가 삭제 대기 상태로 변경되었습니다.",
        },
    ).model_dump()


@admin_router.patch("/users/{user_uuid}/restore")
async def restore_user(
    user_uuid: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    conn = Depends(get_db),
):
    admin_guard = check_admin_access(current_user)
    if admin_guard:
        return admin_guard

    clean_uuid = (user_uuid or "").strip()
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT uuid, id, role, is_deleted FROM users WHERE uuid = %s LIMIT 1", (clean_uuid,))
        target_user = await cur.fetchone()

    if not target_user:
        return error_response(status.HTTP_404_NOT_FOUND, "USER_NOT_FOUND", "해당 유저를 찾을 수 없습니다.")

    try:
        if hasattr(conn, "autocommit"):
            await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("UPDATE users SET is_deleted = FALSE WHERE uuid = %s", (clean_uuid,))
            await cur.execute("UPDATE students SET is_deleted = FALSE WHERE uuid = %s", (clean_uuid,))
            await cur.execute("UPDATE teachers SET is_deleted = FALSE WHERE uuid = %s", (clean_uuid,))
        if hasattr(conn, "commit"):
            await conn.commit()
    except Exception:
        if hasattr(conn, "rollback"):
            await conn.rollback()
        raise
    finally:
        if hasattr(conn, "autocommit"):
            await conn.autocommit(True)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "uuid": clean_uuid,
            "is_deleted": False,
            "isDeleted": False,
            "message": "유저가 성공적으로 복구되었습니다.",
        },
    ).model_dump()


@admin_router.delete("/users/purge-deleted")
async def purge_deleted_users(
    user_uuid: Optional[str] = Query(None, description="특정 유저 단건 영구 삭제 시 UUID"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    conn = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    admin_guard = check_admin_access(current_user)
    if admin_guard:
        return admin_guard

    caller_uuid = current_user.get("uuid") or current_user.get("sub")
    caller_id = current_user.get("id") or current_user.get("user_id")

    async with conn.cursor(cursor=DictCursor) as cur:
        if user_uuid and user_uuid.strip():
            clean_uuid = user_uuid.strip()
            if caller_uuid and str(caller_uuid).strip() == clean_uuid:
                return error_response(
                    status.HTTP_400_BAD_REQUEST,
                    "SELF_DELETION_PROHIBITED",
                    "관리자 본인의 계정은 영구 삭제할 수 없습니다.",
                )
            await cur.execute("SELECT uuid, id, is_deleted FROM users WHERE uuid = %s LIMIT 1", (clean_uuid,))
            target = await cur.fetchone()
            if not target:
                return error_response(status.HTTP_404_NOT_FOUND, "USER_NOT_FOUND", "해당 유저를 찾을 수 없습니다.")
            if caller_id and target.get("id") == caller_id:
                return error_response(
                    status.HTTP_400_BAD_REQUEST,
                    "SELF_DELETION_PROHIBITED",
                    "관리자 본인의 계정은 영구 삭제할 수 없습니다.",
                )
            if not target.get("is_deleted"):
                return error_response(
                    status.HTTP_400_BAD_REQUEST,
                    "NOT_DELETED_USER",
                    "삭제 대기(소프트 삭제) 상태인 유저만 완전 삭제할 수 있습니다.",
                )
            target_uuids = [clean_uuid]
        else:
            await cur.execute("SELECT uuid, id FROM users WHERE is_deleted = TRUE")
            rows = await cur.fetchall()
            target_uuids = [
                r["uuid"]
                for r in (rows or [])
                if r.get("uuid")
                and (not caller_uuid or str(r["uuid"]).strip() != str(caller_uuid).strip())
                and (not caller_id or r.get("id") != caller_id)
            ]

    if not target_uuids:
        return SrFormat(
            status_code=200,
            success=True,
            data={
                "purgedCount": 0,
                "purged_count": 0,
                "purgedUuids": [],
                "message": "영구 삭제할 대상 유저가 없습니다.",
            },
        ).model_dump()

    CHUNK_SIZE = 500
    try:
        if hasattr(conn, "autocommit"):
            await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            for i in range(0, len(target_uuids), CHUNK_SIZE):
                chunk = target_uuids[i : i + CHUNK_SIZE]
                format_placeholders = ",".join(["%s"] * len(chunk))

                # 1. chat_messages 삭제
                chat_msg_sql = f"""
                    DELETE cm FROM chat_messages cm
                    JOIN chat_conversations cc ON cm.conversation_id = cc.conversation_id
                    WHERE cc.user_uuid IN ({format_placeholders})
                """
                await cur.execute(chat_msg_sql, tuple(chunk))

                # 2. chat_conversations 삭제
                chat_conv_sql = f"DELETE FROM chat_conversations WHERE user_uuid IN ({format_placeholders})"
                await cur.execute(chat_conv_sql, tuple(chunk))

                # 3. submissions_logs의 modifier_uuid = NULL
                sl_sql = f"UPDATE submissions_logs SET modifier_uuid = NULL WHERE modifier_uuid IN ({format_placeholders})"
                await cur.execute(sl_sql, tuple(chunk))

                # 4. merits_log의 modifier_uuid = NULL
                ml_sql = f"UPDATE merits_log SET modifier_uuid = NULL WHERE modifier_uuid IN ({format_placeholders})"
                await cur.execute(ml_sql, tuple(chunk))

                # 5. students의 uuid = NULL
                st_sql = f"UPDATE students SET uuid = NULL WHERE uuid IN ({format_placeholders})"
                await cur.execute(st_sql, tuple(chunk))

                # 6. teachers의 uuid = NULL
                tch_sql = f"UPDATE teachers SET uuid = NULL WHERE uuid IN ({format_placeholders})"
                await cur.execute(tch_sql, tuple(chunk))

                # 7. users 영구 삭제 (is_deleted = TRUE 인 건만 안전 삭제)
                del_users_sql = f"DELETE FROM users WHERE uuid IN ({format_placeholders}) AND is_deleted = TRUE"
                await cur.execute(del_users_sql, tuple(chunk))

        if hasattr(conn, "commit"):
            await conn.commit()
    except Exception:
        if hasattr(conn, "rollback"):
            await conn.rollback()
        raise
    finally:
        if hasattr(conn, "autocommit"):
            await conn.autocommit(True)

    try:
        for u in target_uuids:
            await revoke_all_user_tokens(redis, u)
    except Exception:
        pass

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "purgedCount": len(target_uuids),
            "purged_count": len(target_uuids),
            "purgedUuids": target_uuids,
            "message": f"{len(target_uuids)}명의 유저가 영구 삭제되었습니다.",
        },
    ).model_dump()





