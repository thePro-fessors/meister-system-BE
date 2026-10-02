"""
routers/teachers.py - 마이스터 역량인증제 교사용 API 라우터
관련 명세: Tech_spec.md 3장 (교사 API), TODO.md 3장
- 3.1 교사 대시보드 조회 (GET /api/teacher/dashboard & /api/teachers/dashboard)
  - 검토 대기(pendingCount), 재제출(resubmittedCount), 미부여(unscoredCount) 집계
  - 담당 학급/담당 교과 권한 범위(scopeLabel) 및 최근 제출 목록(recentSubmissions[])
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

from core.security import get_current_user
from database import get_db
from routers.auth import get_current_year
from routers.students import SUBMISSION_STATUS_MAP, get_year_id_by_year
from sr_format import Error, SrFormat

logger = logging.getLogger("meister.teachers")

router = APIRouter(tags=["Teacher"])


@router.get(
    "/api/teacher/dashboard",
    summary="교사 대시보드 조회 API (Tech_spec.md 3.1 & TODO.md 3.1)",
    response_model=SrFormat,
)
@router.get(
    "/api/teachers/dashboard",
    summary="교사 대시보드 조회 API (복수형 별칭)",
    response_model=SrFormat,
)
async def get_teacher_dashboard(
    year: Optional[int] = Query(None, description="조회 학년도 (미지정 시 현재 학사학년도)"),
    limit: int = Query(10, ge=1, le=50, description="최근 제출건 조회 개수"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    conn: Any = Depends(get_db),
):
    """
    교사 대시보드 요약 지표 및 최근 증빙자료 제출 목록을 조회합니다.

    보안 및 인가 규칙 (Tech_spec.md 3.1 & 3.2):
    1. 호출 주체: teacher, admin (student 접근 시 403 FORBIDDEN 차단)
    2. 교사의 담당 학급(학년/반) 또는 관리자 권한 범위에 한정하여 집계 및 목록 반환
    3. 타 학급 학생의 데이터 노출 원천 차단 (Scope Isolation)
    """
    user_uuid = current_user.get("uuid")
    user_role = current_user.get("role")

    # 1. 교사/관리자 RBAC 인가 검증
    is_teacher_or_admin = user_role in ("teacher", "admin", "1", "2", 1, 2)
    if not is_teacher_or_admin:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=SrFormat(
                status_code=status.HTTP_403_FORBIDDEN,
                success=False,
                data=None,
                error=Error(code="FORBIDDEN", message="교사 또는 관리자 권한이 필요합니다."),
            ).model_dump(),
        )

    # 2. 교사 정보 및 담당 학급(Scope) 조회
    teacher_row = None
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT teachers_id, name, subject, grade, class, email
            FROM teachers
            WHERE uuid = %s AND is_deleted = FALSE
            LIMIT 1
            """,
            (user_uuid,),
        )
        teacher_row = await cur.fetchone()

    # 교사 테이블에 미등록된 관리자 계정 처리
    is_admin = user_role in ("admin", "2", 2)
    if not teacher_row and not is_admin:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=SrFormat(
                status_code=status.HTTP_404_NOT_FOUND,
                success=False,
                data=None,
                error=Error(code="TEACHER_NOT_FOUND", message="교사 정보를 찾을 수 없습니다."),
            ).model_dump(),
        )

    # 담당 학급 범위(Scope Label) 및 필터링 조건 설정
    teacher_grade = teacher_row.get("grade") if teacher_row else None
    teacher_class = teacher_row.get("class") if teacher_row else None
    teacher_subject = teacher_row.get("subject") if teacher_row else None

    has_homeroom = teacher_grade is not None and teacher_class is not None

    if has_homeroom:
        if teacher_subject:
            scope_label = f"{teacher_grade}학년 {teacher_class}반 ({teacher_subject})"
        else:
            scope_label = f"{teacher_grade}학년 {teacher_class}반"
    elif is_admin:
        scope_label = "전체 학급 (관리자)"
    elif teacher_subject:
        scope_label = f"{teacher_subject} 담당교사 (전체)"
    else:
        scope_label = "담당 학급 미지정 (전체)"

    # 3. 학년도 및 year_id 확인
    target_year = year or await get_current_year(datetime.now())
    year_id = await get_year_id_by_year(conn, target_year)

    if not year_id:
        return SrFormat(
            status_code=status.HTTP_200_OK,
            success=True,
            data={
                "pendingCount": 0,
                "resubmittedCount": 0,
                "unscoredCount": 0,
                "scopeLabel": scope_label,
                "teacher": {
                    "teachersId": teacher_row.get("teachers_id") if teacher_row else None,
                    "name": teacher_row.get("name") if teacher_row else "관리자",
                    "grade": teacher_grade,
                    "classNo": teacher_class,
                    "subject": teacher_subject,
                },
                "year": target_year,
                "recentSubmissions": [],
            },
        ).model_dump()

    # 4. 검토 대기, 재제출, 미부여 통계 단일 쿼리 집계 (범위 격리 적용)
    stats_sql = """
        SELECT
            COUNT(CASE WHEN s.status_code IN (1, 2) THEN 1 END) AS pending_count,
            COUNT(CASE WHEN s.status_code IN (1, 2) AND EXISTS (
                SELECT 1 FROM submissions_logs sl
                WHERE sl.submission_id = s.submission_id AND sl.action_type = '재제출'
            ) THEN 1 END) AS resubmitted_count,
            COUNT(CASE WHEN s.status_code IN (1, 2) AND s.granted_score IS NULL THEN 1 END) AS unscored_count
        FROM submissions s
        JOIN students st ON s.student_id = st.student_id AND st.is_deleted = FALSE
        JOIN student_academic_records sar ON sar.student_id = st.student_id AND sar.year_id = %s
        WHERE s.is_deleted = FALSE
    """
    stats_params: List[Any] = [year_id]

    if has_homeroom:
        stats_sql += " AND sar.grade = %s AND sar.class = %s"
        stats_params.extend([teacher_grade, teacher_class])

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(stats_sql, tuple(stats_params))
        stats_row = await cur.fetchone()

    pending_count = stats_row.get("pending_count", 0) if stats_row else 0
    resubmitted_count = stats_row.get("resubmitted_count", 0) if stats_row else 0
    unscored_count = stats_row.get("unscored_count", 0) if stats_row else 0

    # 5. 권한 범위 내 최근 제출 증빙자료 목록 조회
    recent_sql = """
        SELECT
            s.submission_id,
            s.student_id,
            st.name AS student_name,
            sar.grade,
            sar.class AS class_no,
            sar.number,
            ca.area_id,
            ca.name AS area_name,
            ei.item_id,
            ei.name AS item_name,
            s.detail,
            s.activity_date,
            s.file_path,
            s.link_url,
            s.description,
            s.status_code,
            s.granted_score,
            s.created_at,
            EXISTS (
                SELECT 1 FROM submissions_logs sl
                WHERE sl.submission_id = s.submission_id AND sl.action_type = '재제출'
            ) AS is_resubmitted
        FROM submissions s
        JOIN students st ON s.student_id = st.student_id AND st.is_deleted = FALSE
        JOIN student_academic_records sar ON sar.student_id = st.student_id AND sar.year_id = %s
        LEFT JOIN evaluation_items ei ON s.item_id = ei.item_id
        LEFT JOIN certification_areas ca ON ei.area_id = ca.area_id
        WHERE s.is_deleted = FALSE
    """
    recent_params: List[Any] = [year_id]

    if has_homeroom:
        recent_sql += " AND sar.grade = %s AND sar.class = %s"
        recent_params.extend([teacher_grade, teacher_class])

    recent_sql += " ORDER BY s.created_at DESC, s.submission_id DESC LIMIT %s"
    recent_params.append(limit)

    recent_submissions = []
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(recent_sql, tuple(recent_params))
        rows = await cur.fetchall()
        for r in rows:
            st_code = r.get("status_code", 1)
            status_text = SUBMISSION_STATUS_MAP.get(st_code, "제출완료")
            is_resub = bool(r.get("is_resubmitted"))

            created_val = r.get("created_at")
            submitted_at_str = created_val.isoformat() if hasattr(created_val, "isoformat") else str(created_val) if created_val else None

            act_date_val = r.get("activity_date")
            activity_date_str = str(act_date_val) if act_date_val else None

            recent_submissions.append({
                "id": r.get("submission_id"),
                "submissionId": r.get("submission_id"),
                "studentId": r.get("student_id"),
                "studentName": r.get("student_name"),
                "grade": r.get("grade"),
                "classNo": r.get("class_no"),
                "number": r.get("number"),
                "area": r.get("area_name"),
                "areaId": r.get("area_id"),
                "itemId": r.get("item_id"),
                "itemName": r.get("item_name"),
                "detail": r.get("detail"),
                "activityDate": activity_date_str,
                "description": r.get("description"),
                "status": status_text,
                "statusCode": st_code,
                "isResubmitted": is_resub,
                "grantedScore": float(r["granted_score"]) if r.get("granted_score") is not None else None,
                "submittedAt": submitted_at_str,
                "fileUrl": r.get("file_path"),
                "linkUrl": r.get("link_url"),
            })

    return SrFormat(
        status_code=status.HTTP_200_OK,
        success=True,
        data={
            "pendingCount": pending_count,
            "resubmittedCount": resubmitted_count,
            "unscoredCount": unscored_count,
            "scopeLabel": scope_label,
            "teacher": {
                "teachersId": teacher_row.get("teachers_id") if teacher_row else None,
                "name": teacher_row.get("name") if teacher_row else "관리자",
                "grade": teacher_grade,
                "classNo": teacher_class,
                "subject": teacher_subject,
            },
            "year": target_year,
            "recentSubmissions": recent_submissions,
        },
    ).model_dump()
