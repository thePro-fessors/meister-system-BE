"""
routers/teachers.py - 마이스터 역량인증제 교사용 API 라우터
관련 명세: Tech_spec.md 3장 (교사 API), TODO.md 3장
- 3.1 교사 대시보드 조회 (GET /api/teacher/dashboard & /api/teachers/dashboard)
  - 검토 대기(pendingCount), 재제출(resubmittedCount), 미부여(unscoredCount) 집계
  - 담당 학급/담당 교과 권한 범위(scopeLabel) 및 최근 제출 목록(recentSubmissions[])
- 3.2 담당 학생 목록 및 제출 현황 검색 (GET /api/teacher/students & /api/teachers/students)
  - 쿼리 필터: year, grade, classNo, name, studentNo, area, status, hasPoints
  - 교사 담당 학급/권한 범위 강제 필터링 (클라이언트 우회 차단)
  - 학생별 취득 점수, 인증 상태, 대기 증빙 건수, 상벌점 합계 반환
"""

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

from core.security import get_current_user
from database import get_db
from routers.auth import get_current_year
from routers.students import (
    SUBMISSION_STATUS_MAP,
    calculate_area_grade,
    calculate_area_status,
    calculate_cert_status,
    get_year_id_by_year,
)
from sr_format import Error, SrFormat

logger = logging.getLogger("meister.teachers")

router = APIRouter(tags=["Teacher"])


# ==============================================================================
# Pydantic Schemas (DTO)
# ==============================================================================

class TeacherStudentItem(BaseModel):
    """3.2 담당 학생 목록 및 제출 현황 검색 API 응답 아이템 (Tech_spec.md 3.2 & TODO.md 3.2)"""
    studentId: int = Field(..., description="학생 고유 ID")
    id: Optional[int] = Field(None, description="학생 고유 ID (호환용 alias)")
    name: str = Field(..., description="학생 이름")
    grade: int = Field(..., description="학년")
    classNo: int = Field(..., description="학급 반")
    number: int = Field(..., description="번호")
    studentNo: Optional[int] = Field(None, description="번호 (호환용 alias)")
    totalScore: float = Field(..., description="취득 점수 합계 (상벌점 반영 및 소수점 1자리 반올림)")
    certStatus: str = Field(..., description="인증 상태 (인증 가능, 검토중, 보완 필요, 미달성)")
    pendingCount: int = Field(..., description="검토 대기 증빙 건수")
    pointTotal: float = Field(..., description="상벌점 합계")


# ==============================================================================
# 3.1 교사 대시보드 조회 API
# ==============================================================================

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

    has_homeroom = (not is_admin) and (teacher_grade is not None and teacher_class is not None)

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
            COUNT(CASE WHEN s.status_code IN (1, 2) AND EXISTS (\n                SELECT 1 FROM submissions_logs sl
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


# ==============================================================================
# 3.2 담당 학생 목록 및 제출 현황 검색 API (Tech_spec.md 3.2 & TODO.md 3.2)
# ==============================================================================

@router.get(
    "/api/teacher/students",
    summary="담당 학생 목록 및 제출 현황 검색 API (Tech_spec.md 3.2 & TODO.md 3.2)",
    response_model=SrFormat[List[TeacherStudentItem]],
)
@router.get(
    "/api/teachers/students",
    summary="담당 학생 목록 및 제출 현황 검색 API (복수형 별칭)",
    response_model=SrFormat[List[TeacherStudentItem]],
)
async def get_teacher_students(
    year: Optional[int] = Query(None, description="조회 학년도 (미지정 시 현재 학사학년도)"),
    grade: Optional[int] = Query(None, ge=1, le=3, description="학년 필터 (1~3)"),
    classNo: Optional[int] = Query(None, ge=1, description="반 필터"),
    class_no: Optional[int] = Query(None, alias="class", ge=1, description="반 필터 (alias)"),
    name: Optional[str] = Query(None, description="학생 성명 검색 (부분 일치)"),
    studentNo: Optional[int] = Query(None, ge=1, description="학생 번호 또는 학번 필터"),
    number: Optional[int] = Query(None, ge=1, description="학생 번호 필터 (alias)"),
    area: Optional[str] = Query(None, description="인증 영역 필터 (영역명 또는 areaId)"),
    status_filter: Optional[str] = Query(None, alias="status", description="인증 상태 또는 제출 상태 필터"),
    hasPoints: Optional[str] = Query(None, description="상벌점 보유 여부 필터 (true/false)"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    conn: Any = Depends(get_db),
):
    """
    교사 담당 학급 또는 권한 범위 내 학생 목록 및 제출/인증 현황을 검색 및 조회합니다.

    보안 및 인가 규칙 (Tech_spec.md 3.2 & TODO.md 3.2):
    1. 호출 주체: teacher, admin (student 호출 시 403 Forbidden 차단)
    2. 담임교사의 경우 본인 담당 학급(학년/반) 데이터만 강제 필터링하여 타 학급 열람 우회를 원천 차단합니다.
    3. 학생별 취득 점수(totalScore), 종합 인증 상태(certStatus), 심사 대기건수(pendingCount), 상벌점 합계(pointTotal)를 계산하여 반환합니다.
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

    # 3. 담임교사 권한 범위 강제 필터링 및 우회 차단 (Scope Isolation)
    teacher_grade = teacher_row.get("grade") if teacher_row else None
    teacher_class = teacher_row.get("class") if teacher_row else None
    has_homeroom = (not is_admin) and (teacher_grade is not None and teacher_class is not None)

    req_class = classNo if classNo is not None else class_no
    req_number = studentNo if studentNo is not None else number

    if has_homeroom:
        # 클라이언트가 본인 담임 학급 이외의 학년 또는 반을 지정하여 우회 조회를 시도하는 경우 403 Forbidden 차단
        if (grade is not None and grade != teacher_grade) or (req_class is not None and req_class != teacher_class):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content=SrFormat(
                    status_code=status.HTTP_403_FORBIDDEN,
                    success=False,
                    data=None,
                    error=Error(code="FORBIDDEN", message="담당 학급 외의 학생 목록은 조회할 수 없습니다."),
                ).model_dump(),
            )
        target_grade = teacher_grade
        target_class = teacher_class
    else:
        target_grade = grade
        target_class = req_class

    # 4. 대상 학년도 및 year_id 확인
    target_year = year or await get_current_year(datetime.now())
    year_id = await get_year_id_by_year(conn, target_year)

    if not year_id:
        return SrFormat(
            status_code=status.HTTP_200_OK,
            success=True,
            data=[],
            error=None,
        ).model_dump()

    # 5. 대상 학생 기본 정보 및 학적 조회 (DB 수준 필터링)
    student_sql = """
        SELECT
            st.student_id,
            st.name,
            st.uuid,
            sar.grade,
            sar.class AS class_no,
            sar.number
        FROM students st
        JOIN student_academic_records sar ON st.student_id = sar.student_id AND sar.year_id = %s
        WHERE st.is_deleted = FALSE
    """
    student_params: List[Any] = [year_id]

    if target_grade is not None:
        student_sql += " AND sar.grade = %s"
        student_params.append(target_grade)

    if target_class is not None:
        student_sql += " AND sar.class = %s"
        student_params.append(target_class)

    if name:
        clean_name = name.strip()
        if clean_name:
            student_sql += " AND st.name LIKE %s"
            student_params.append(f"%{clean_name}%")

    if req_number is not None:
        student_sql += " AND (sar.number = %s OR st.student_id = %s)"
        student_params.extend([req_number, req_number])

    student_sql += " ORDER BY sar.grade ASC, sar.class ASC, sar.number ASC, st.student_id ASC"

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(student_sql, tuple(student_params))
        student_rows = await cur.fetchall()

    if not student_rows:
        return SrFormat(
            status_code=status.HTTP_200_OK,
            success=True,
            data=[],
            error=None,
        ).model_dump()

    student_ids = [s["student_id"] for s in student_rows]
    placeholders = ", ".join(["%s"] * len(student_ids))

    # 6. 대상 학년도 영역(certification_areas), 학생들의 제출건(submissions), 상벌점(merits) 일괄 조회
    async with conn.cursor(cursor=DictCursor) as cur:
        # (1) 학년도 인증 영역 일괄 조회
        await cur.execute(
            """
            SELECT area_id, year_id, grade, name, max_score
            FROM certification_areas
            WHERE year_id = %s
            ORDER BY area_id ASC
            """,
            (year_id,),
        )
        all_areas = await cur.fetchall()

        # (2) 증빙자료 일괄 조회
        sub_sql = f"""
            SELECT 
                s.submission_id,
                s.student_id,
                s.item_id,
                s.status_code,
                s.granted_score,
                ca.area_id,
                ca.name AS area_name,
                ei.scoring_type,
                ei.max_score AS item_max_score
            FROM submissions s
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id AND ca.year_id = %s
            WHERE s.student_id IN ({placeholders})
              AND s.is_deleted = FALSE
        """
        await cur.execute(sub_sql, (year_id, *student_ids))
        all_subs = await cur.fetchall()

        # (3) 상벌점 일괄 조회 (3월 1일 ~ 익년 3월 1일 학사력 기준)
        merit_start = date(target_year, 3, 1)
        merit_end = date(target_year + 1, 3, 1)
        merit_sql = f"""
            SELECT 
                merits_point_id,
                student_id,
                type,
                points,
                related_area,
                occurred_at
            FROM merits
            WHERE student_id IN ({placeholders})
              AND is_reflected = TRUE
              AND is_deleted = FALSE
              AND occurred_at >= %s
              AND occurred_at < %s
        """
        await cur.execute(merit_sql, (*student_ids, merit_start, merit_end))
        all_merits = await cur.fetchall()

    # 학생별 매핑 정리
    subs_by_student: Dict[int, List[Dict[str, Any]]] = {}
    for sub in all_subs:
        sid = sub["student_id"]
        if sid not in subs_by_student:
            subs_by_student[sid] = []
        subs_by_student[sid].append(sub)

    merits_by_student: Dict[int, List[Dict[str, Any]]] = {}
    for m in all_merits:
        sid = m["student_id"]
        if sid not in merits_by_student:
            merits_by_student[sid] = []
        merits_by_student[sid].append(m)

    # 7. 학생별 역량 점수, 등급, 인증 상태, 대기 건수, 상벌점 합계 연산 (In-Memory Processing)
    result_students: List[Dict[str, Any]] = []

    for st in student_rows:
        sid = st["student_id"]
        st_grade = st["grade"]
        st_subs = subs_by_student.get(sid, [])
        st_merits = merits_by_student.get(sid, [])

        # 학년별 영역 배점 필터링 (Fallback: grade 구분이 없는 경우 전체)
        student_areas = [a for a in all_areas if a.get("grade") == st_grade]
        if not student_areas:
            student_areas = all_areas

        # 증빙 제출 집계
        area_item_scores: Dict[int, Dict[int, List[float]]] = {}
        area_item_meta: Dict[int, Dict[str, Any]] = {}
        area_pending_counts: Dict[int, int] = {}
        pending_count = 0
        has_any_pending = False

        for sub in st_subs:
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

            if st_code in (1, 2):
                has_any_pending = True
                pending_count += 1
                area_pending_counts[aid] = area_pending_counts.get(aid, 0) + 1
            elif st_code == 3:
                if aid not in area_item_scores:
                    area_item_scores[aid] = {}
                if iid not in area_item_scores[aid]:
                    area_item_scores[aid][iid] = []
                area_item_scores[aid][iid].append(g_score)

        # 상벌점 집계
        area_merit_points: Dict[str, float] = {}
        total_merit_points = 0.0
        for m in st_merits:
            m_type = m.get("type")
            pts = float(m.get("points") or 0.0)
            rel_area = (m.get("related_area") or "").strip()
            if m_type in ("-", "벌점"):
                signed_pts = -abs(pts)
            else:
                signed_pts = abs(pts)
            total_merit_points += signed_pts
            if rel_area:
                area_merit_points[rel_area] = area_merit_points.get(rel_area, 0.0) + signed_pts
        point_total = round(total_merit_points, 1)

        # 5대 핵심 영역별 점수 계산
        area_results: List[Dict[str, Any]] = []
        total_score = 0.0
        for area_info in student_areas:
            aid = area_info["area_id"]
            aname = area_info["name"]
            amax = float(area_info["max_score"])

            area_raw_score = 0.0
            items_dict = area_item_scores.get(aid, {})
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
                    sum_item = sum(scores)
                    if item_max_limit is not None:
                        sum_item = min(sum_item, item_max_limit)
                    area_raw_score += sum_item

            merit_adjustment = area_merit_points.get(aname, 0.0)
            adjusted_score = area_raw_score + merit_adjustment
            final_score = max(0.0, min(adjusted_score, amax))
            final_score = round(final_score, 1)

            grade_str = calculate_area_grade(final_score, amax)
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

        total_score = round(total_score, 1)
        overall_cert_status = calculate_cert_status(area_results, has_any_pending)

        # 8. 후속 필터링 (area, status, hasPoints)
        # (1) area 필터
        if area:
            clean_area = area.strip()
            matches_area = any(
                sub.get("area_name") == clean_area or str(sub.get("area_id")) == clean_area
                for sub in st_subs
            ) or any(
                (m.get("related_area") or "").strip() == clean_area
                for m in st_merits
            )
            if not matches_area:
                continue

        # (2) status 필터 (인증 상태 또는 제출 상태)
        if status_filter:
            clean_status = status_filter.strip()
            sub_status_names = {SUBMISSION_STATUS_MAP.get(s["status_code"], "") for s in st_subs}
            sub_status_codes = {str(s["status_code"]) for s in st_subs}

            matches_cert_status = overall_cert_status == clean_status
            matches_sub_status = (clean_status in sub_status_names) or (clean_status in sub_status_codes)
            if clean_status == "검토중" and pending_count > 0:
                matches_cert_status = True

            if not (matches_cert_status or matches_sub_status):
                continue

        # (3) hasPoints 필터
        if hasPoints is not None:
            clean_hp = str(hasPoints).strip().lower()
            if clean_hp in ("true", "1", "t", "y"):
                if len(st_merits) == 0 and point_total == 0.0:
                    continue
            elif clean_hp in ("false", "0", "f", "n"):
                if len(st_merits) > 0 or point_total != 0.0:
                    continue

        result_students.append({
            "studentId": sid,
            "id": sid,
            "name": st["name"],
            "grade": st["grade"],
            "classNo": st["class_no"],
            "number": st["number"],
            "studentNo": st["number"],
            "totalScore": total_score,
            "certStatus": overall_cert_status,
            "pendingCount": pending_count,
            "pointTotal": point_total,
        })

    return SrFormat(
        status_code=status.HTTP_200_OK,
        success=True,
        data=result_students,
        error=None,
    ).model_dump()
