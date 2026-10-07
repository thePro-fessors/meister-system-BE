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
- 3.3 특정 학생 상세 현황 조회 (GET /api/teacher/students/{studentId} & /api/teachers/students/{studentId})
  - 담당 권한 범위 내 학생인지 서버 검증 (타 학급 접근 시 403 FORBIDDEN 차단)
  - 학생의 5대 영역별 세부 인증 현황(취득점수, 배점, 등급, 상태) 및 증빙 제출 전체 목록 반환
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

from core.calculator import (
    calculate_student_certification,
    round_decimal,
)
from core.academic import get_current_year, get_year_id_by_year
from core.security import get_current_user
from database import get_db
from routers.students import SUBMISSION_STATUS_MAP
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


class TeacherStudentDetailAreaItem(BaseModel):
    """3.3 특정 학생 상세 현황 5대 영역별 세부 인증 결과 DTO (Tech_spec.md 2.1 & 3.3)"""
    area: str = Field(..., description="인증 영역명")
    areaId: Optional[int] = Field(None, description="영역 고유 ID")
    score: float = Field(..., description="학생 취득 점수")
    maxScore: float = Field(..., description="영역 최대 배점")
    grade: str = Field(..., description="영역 등급 (S, A, B, 미달성)")
    status: str = Field(..., description="영역 진행 상태 (달성, 검토중, 보완 필요)")


class TeacherStudentDetailSubmissionItem(BaseModel):
    """3.3 특정 학생 상세 현황 증빙 제출 목록 DTO (Tech_spec.md 2.3 & 3.3)"""
    id: int = Field(..., description="제출 건 ID")
    submissionId: Optional[int] = Field(None, description="제출 건 ID alias")
    studentId: int = Field(..., description="학생 ID")
    year: int = Field(..., description="학년도")
    area: str = Field(..., description="영역명")
    areaId: Optional[int] = Field(None, description="영역 ID")
    itemId: int = Field(..., description="평가 항목 ID")
    itemName: str = Field(..., description="평가 항목명")
    detail: str = Field(..., description="세부 항목명/활동명")
    activityDate: Optional[str] = Field(None, description="활동 일자 (YYYY-MM-DD)")
    description: Optional[str] = Field(None, description="자료 설명")
    filePath: Optional[str] = Field(None, description="파일 경로")
    fileUrl: Optional[str] = Field(None, description="파일 다운로드/열람 URL")
    originalFilename: Optional[str] = Field(None, description="업로드 원본 파일명")
    linkUrl: Optional[str] = Field(None, description="증빙 링크 URL")
    link: Optional[str] = Field(None, description="증빙 링크 alias")
    status: str = Field(..., description="제출 상태 문자열 (제출완료, 검토중, 인정완료, 반려, 재제출요청)")
    statusCode: int = Field(..., description="제출 상태 코드 (1~5)")
    score: Optional[float] = Field(None, description="인정 점수")
    grantedScore: Optional[float] = Field(None, description="인정 점수 alias")
    teacherComment: Optional[str] = Field(None, description="교사 검토 의견")
    submittedAt: Optional[str] = Field(None, description="제출 일시 (ISO 8601)")
    reviewedAt: Optional[str] = Field(None, description="검토 일시 (ISO 8601)")
    reviewerId: Optional[int] = Field(None, description="검토 교사 ID")


class TeacherStudentDetailResponse(BaseModel):
    """3.3 특정 학생 상세 현황 응답 DTO"""
    studentId: int = Field(..., description="학생 고유 ID")
    id: Optional[int] = Field(None, description="학생 고유 ID alias")
    name: str = Field(..., description="학생 이름")
    grade: int = Field(..., description="학년")
    classNo: int = Field(..., description="학급 반")
    number: int = Field(..., description="번호")
    studentNo: Optional[int] = Field(None, description="번호 alias")
    email: Optional[str] = Field(None, description="학생 이메일")
    year: int = Field(..., description="조회 학년도")
    areas: List[TeacherStudentDetailAreaItem] = Field(..., description="5대 영역별 세부 인증 현황")
    totalScore: float = Field(..., description="취득 점수 합계")
    certStatus: str = Field(..., description="종합 역량인증 상태 (인증 가능, 검토중, 보완 필요, 미달성)")
    pointTotal: float = Field(..., description="상벌점 합계")
    pendingCount: int = Field(..., description="검토 대기 증빙 건수")
    submissions: List[TeacherStudentDetailSubmissionItem] = Field(..., description="증빙 제출 전체 목록")
    student: Optional[Dict[str, Any]] = Field(None, description="학생 정보 객체 (호환용)")


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

    # 4. 검토 대기, 재제출, 미부여 통계 단일 쿼리 집계 (DATA-03: 제출 학년도 격리)
    stats_sql = """
        SELECT
            COUNT(CASE WHEN s.status_code IN (1, 2) THEN 1 END) AS pending_count,
            COUNT(CASE WHEN s.status_code IN (1, 2) AND EXISTS (
                SELECT 1 FROM submissions_logs sl
                WHERE sl.submission_id = s.submission_id AND sl.action_type IN ('재제출', 'RESUBMIT')
            ) THEN 1 END) AS resubmitted_count,
            COUNT(CASE WHEN s.status_code IN (1, 2) AND s.granted_score IS NULL THEN 1 END) AS unscored_count
        FROM submissions s
        JOIN evaluation_items ei ON s.item_id = ei.item_id
        JOIN certification_areas ca ON ei.area_id = ca.area_id AND ca.year_id = %s
        JOIN students st ON s.student_id = st.student_id AND st.is_deleted = FALSE
        JOIN student_academic_records sar ON sar.student_id = st.student_id AND sar.year_id = ca.year_id
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

    # 4-1. 담당 학생 수 집계 (DATA-03: 제출이 없는 담당 학생도 포함)
    student_count_sql = """
        SELECT COUNT(DISTINCT sar.student_id) AS student_count
        FROM student_academic_records sar
        JOIN students st ON sar.student_id = st.student_id AND st.is_deleted = FALSE
        WHERE sar.year_id = %s
    """
    student_count_params: List[Any] = [year_id]
    if has_homeroom:
        student_count_sql += " AND sar.grade = %s AND sar.class = %s"
        student_count_params.extend([teacher_grade, teacher_class])

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(student_count_sql, tuple(student_count_params))
        sc_row = await cur.fetchone()
    student_count = sc_row.get("student_count", 0) if sc_row else 0

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
                WHERE sl.submission_id = s.submission_id AND sl.action_type IN ('재제출', 'RESUBMIT')
            ) AS is_resubmitted
        FROM submissions s
        JOIN evaluation_items ei ON s.item_id = ei.item_id
        JOIN certification_areas ca ON ei.area_id = ca.area_id AND ca.year_id = %s
        JOIN students st ON s.student_id = st.student_id AND st.is_deleted = FALSE
        JOIN student_academic_records sar ON sar.student_id = st.student_id AND sar.year_id = ca.year_id
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
            "studentCount": student_count,
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
    [GET] /api/teacher/students

    교사의 담당 학급 또는 권한 범위 내 학생 목록과 각 학생의 인증 현황, 점수, 상벌점 요약을 조회합니다.

    보안 및 인가 규칙 (Tech_spec.md 3.2 & TODO.md 3.2):
    1. 호출자 역할: 교사(teacher) 또는 관리자(admin)만 허용 (학생 접근 시 403 FORBIDDEN).
    2. 교사의 담당 학급(grade, class)이 배정된 경우, 클라이언트의 grade/class 쿼리 파라미터와 무관하게
       담당 학급으로 강제 격리(Scope Isolation)하여 타 학급 정보 조회를 원천 차단합니다.
    3. 비담임 교과 교사 또는 관리자 계정은 grade, class 쿼리 파라미터를 통해 전체 학급을 필터링 및 조회할 수 있습니다.
    """
    user_role = current_user.get("role")
    user_uuid = current_user.get("uuid")

    # 1. 교사/관리자 RBAC 인가 검증
    if user_role not in ("teacher", "admin", "1", "2", 1, 2):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=SrFormat(
                status_code=status.HTTP_403_FORBIDDEN,
                success=False,
                data=None,
                error=Error(code="FORBIDDEN", message="교사 또는 관리자 권한이 필요합니다."),
            ).model_dump(),
        )

    is_admin = user_role in ("admin", "2", 2)

    # 2. 교사 정보 및 담당 학급 범위 조회
    teacher_row = None
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT teachers_id, name, grade, class AS class_num, subject
            FROM teachers
            WHERE uuid = %s AND is_deleted = FALSE
            LIMIT 1
            """,
            (user_uuid,),
        )
        teacher_row = await cur.fetchone()

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

    teacher_grade = teacher_row.get("grade") if teacher_row else None
    teacher_class = teacher_row.get("class_num") if teacher_row else None
    if teacher_class is None and teacher_row:
        teacher_class = teacher_row.get("class")

    has_homeroom = (not is_admin) and (teacher_grade is not None and teacher_class is not None)

    # 3. 학년도 및 year_id 조회
    target_year = year if year is not None else await get_current_year()
    year_id = await get_year_id_by_year(conn, target_year)

    if not year_id:
        return SrFormat(
            status_code=status.HTTP_200_OK,
            success=True,
            data=[],
            error=None,
        ).model_dump()

    # 4. 담임 권한 범위 강제 적용 및 필터 해석
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

    # 5. 학생 학적 기본 정보 조회
    student_sql = """
        SELECT 
            st.student_id,
            st.name,
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
            # SQL LIKE 와일드카드(%, _, \) 이스케이프 (Full Table Scan 및 패턴 주입 방어)
            escaped_name = clean_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            student_sql += " AND st.name LIKE %s"
            student_params.append(f"%{escaped_name}%")

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
                occurred_at,
                is_reflected
            FROM merits
            WHERE student_id IN ({placeholders})
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

        # 학년별 영역 배점 필터링 (SCORE-01: 공통 계산 엔진 단일화)
        student_areas = [a for a in all_areas if a.get("grade") == st_grade]
        if not student_areas:
            student_areas = [a for a in all_areas if a.get("grade") is None] or all_areas

        calc_res = calculate_student_certification(student_areas, st_subs, st_merits)
        area_results = calc_res["areas"]
        total_score = calc_res["totalScore"]
        overall_cert_status = calc_res["certStatus"]
        pending_count = calc_res["pendingCount"]

        # 전체 상벌점 합계 연산 (SCORE-02)
        total_merit_points = 0.0
        for m in st_merits:
            m_type = m.get("type")
            pts = float(m.get("points") or 0.0)
            signed_pts = -abs(pts) if m_type in ("-", "벌점") else abs(pts)
            total_merit_points += signed_pts
        point_total = round_decimal(total_merit_points, 1)

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

        # (3) hasPoints 필터 (SCORE-02: 조회 범위 내 기록 존재 여부 판정)
        if hasPoints is not None:
            clean_hp = str(hasPoints).strip().lower()
            has_records = len(st_merits) > 0 or point_total != 0.0
            if clean_hp in ("true", "1", "t", "y"):
                if not has_records:
                    continue
            elif clean_hp in ("false", "0", "f", "n"):
                if has_records:
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


# ==============================================================================
# 3.3 교사용 특정 학생 상세 현황 및 제출 목록 조회 API (Tech_spec.md 3.3 & TODO.md 3.3)
# ==============================================================================

@router.get(
    "/api/teacher/students/{student_id}",
    summary="특정 학생 상세 현황 및 제출 목록 조회 (Tech_spec.md 3.3 & TODO.md 3.3)",
    response_model=SrFormat[TeacherStudentDetailResponse],
)
@router.get(
    "/api/teachers/students/{student_id}",
    summary="특정 학생 상세 현황 및 제출 목록 조회 (복수형 별칭)",
    response_model=SrFormat[TeacherStudentDetailResponse],
    include_in_schema=False,
)
async def get_teacher_student_detail(
    student_id: int,
    year: Optional[int] = Query(None, description="조회 학년도 (생략 시 현재 학사년도)"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    conn: Any = Depends(get_db),
):
    """
    [GET] /api/teacher/students/{student_id}

    특정 학생의 5대 영역별 세부 인증 현황(취득점수, 배점, 성취등급, 상태)과 
    증빙자료 제출 전체 목록을 교사 권한으로 조회합니다.

    보안 및 권한 규칙 (Tech_spec.md 3.3 & TODO.md 3.3):
    1. 호출자 역할: 교사(teacher) 또는 관리자(admin)만 허용 (학생 접근 시 403 FORBIDDEN).
    2. 교사 계정 확인: teachers 테이블에 등록된 교사여야 함 (미등록 교사 시 404 TEACHER_NOT_FOUND).
    3. 담임 권한 격리 (IDOR 방어):
       - 담임 교사(grade 및 class 배정)인 경우, 오직 본인 담당 학급 학생만 열람 가능.
       - 타 학급/타 학년 학생 조회 시 403 FORBIDDEN (FORBIDDEN, "담당 학급 학생만 상세 조회할 수 있습니다.").
       - 비담임 교과 교사 및 관리자는 전교생 열람 가능.
    4. 대상 학생 및 학적 존재 검증:
       - 학생 미존재 시 404 USER_NOT_FOUND
       - 학년도 미존재 시 404 ITEM_NOT_FOUND
       - 해당 학년도 학적 미존재 시 404 USER_NOT_FOUND
    """
    user_role = current_user.get("role")
    user_uuid = current_user.get("uuid")

    # 1. RBAC 검증: 교사 또는 관리자 권한 필수
    if user_role not in ("teacher", "admin", "1", "2", 1, 2):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=SrFormat(
                status_code=status.HTTP_403_FORBIDDEN,
                success=False,
                data=None,
                error=Error(code="FORBIDDEN", message="교사 또는 관리자 권한이 필요합니다."),
            ).model_dump(),
        )

    is_admin = user_role in ("admin", "2", 2)

    # 2. 교사 프로필 및 담임 권한 범위 확인
    teacher_row = None
    teacher_grade = None
    teacher_class = None

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT teachers_id, name, grade, class AS class_num, subject
            FROM teachers
            WHERE uuid = %s AND is_deleted = FALSE
            LIMIT 1
            """,
            (user_uuid,),
        )
        teacher_row = await cur.fetchone()

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

    if teacher_row:
        teacher_grade = teacher_row.get("grade")
        teacher_class = teacher_row.get("class_num")
        if teacher_class is None:
            teacher_class = teacher_row.get("class")

    has_homeroom = (not is_admin) and (teacher_grade is not None and teacher_class is not None)

    # 3. 대상 학생 존재 여부 확인
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT student_id, uuid, name, email
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
                error=Error(code="USER_NOT_FOUND", message="해당 학생을 찾을 수 없습니다."),
            ).model_dump(),
        )

    # 4. 학년도 및 학적 정보 확인
    target_year = year if year is not None else await get_current_year()
    year_id = await get_year_id_by_year(conn, target_year)

    if not year_id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=SrFormat(
                status_code=status.HTTP_404_NOT_FOUND,
                success=False,
                data=None,
                error=Error(code="ITEM_NOT_FOUND", message=f"{target_year} 학년도 정보가 시스템에 존재하지 않습니다."),
            ).model_dump(),
        )

    async with conn.cursor(cursor=DictCursor) as cur:
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
                error=Error(code="USER_NOT_FOUND", message=f"{target_year} 학년도에 등록된 해당 학생의 학적 정보를 찾을 수 없습니다."),
            ).model_dump(),
        )

    st_grade = academic_record["grade"]
    st_class = academic_record["class_no"]
    st_number = academic_record["number"]

    # 5. 담임 권한 범위 검증 (Scope Isolation & IDOR 방어)
    if has_homeroom:
        if st_grade != teacher_grade or st_class != teacher_class:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content=SrFormat(
                    status_code=status.HTTP_403_FORBIDDEN,
                    success=False,
                    data=None,
                    error=Error(code="FORBIDDEN", message="담당 학급 학생만 상세 조회할 수 있습니다."),
                ).model_dump(),
            )

    # 6. 대상 학년/학년도 인증 영역, 학생 제출건, 상벌점 조회
    async with conn.cursor(cursor=DictCursor) as cur:
        # (1) 인증 영역 조회
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

        if not areas:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=SrFormat(
                    status_code=status.HTTP_404_NOT_FOUND,
                    success=False,
                    data=None,
                    error=Error(
                        code="CRITERIA_NOT_CONFIGURED",
                        message=f"{target_year} 학년도 {st_grade}학년 평가 기준이 설정되지 않았습니다.",
                    ),
                ).model_dump(),
            )

        # (2) 학생 증빙 제출건 전체 조회 (최신 제출순 정렬)
        await cur.execute(
            """
            SELECT 
                s.submission_id,
                s.student_id,
                ay.year,
                ca.area_id,
                ca.name AS area_name,
                s.item_id,
                ei.name AS item_name,
                ei.scoring_type,
                ei.max_score AS item_max_score,
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
            JOIN certification_areas ca ON ei.area_id = ca.area_id AND ca.year_id = %s
            JOIN academic_years ay ON ca.year_id = ay.year_id
            WHERE s.student_id = %s
              AND s.is_deleted = FALSE
            ORDER BY s.created_at DESC, s.submission_id DESC
            """,
            (year_id, student_id),
        )
        all_submissions = await cur.fetchall()

        # (3) 상벌점 조회 (3월 1일 ~ 익년 3월 1일 학사력 기준)
        merit_start = date(target_year, 3, 1)
        merit_end = date(target_year + 1, 3, 1)

        await cur.execute(
            """
            SELECT 
                merits_point_id,
                type,
                points,
                related_area,
                occurred_at,
                is_reflected
            FROM merits
            WHERE student_id = %s
              AND is_deleted = FALSE
              AND occurred_at >= %s
              AND occurred_at < %s
            """,
            (student_id, merit_start, merit_end),
        )
        all_merits = await cur.fetchall()

    # 7. 응답용 제출 포맷 변환 (점수 산출은 calculate_student_certification에 위임)
    formatted_submissions: List[Dict[str, Any]] = []

    for sub in all_submissions:
        aid = sub["area_id"]
        iid = sub["item_id"]
        st_code = sub["status_code"]
        g_score = float(sub["granted_score"]) if sub["granted_score"] is not None else None

        # 제출 포맷 변환
        st_text = SUBMISSION_STATUS_MAP.get(st_code, "제출완료")
        act_date_val = sub["activity_date"]
        act_date_str = act_date_val.strftime("%Y-%m-%d") if hasattr(act_date_val, "strftime") else (str(act_date_val) if act_date_val else None)
        created_val = sub["created_at"]
        created_str = created_val.isoformat() if hasattr(created_val, "isoformat") else (str(created_val) if created_val else None)
        reviewed_val = sub["reviewed_at"]
        reviewed_str = reviewed_val.isoformat() if hasattr(reviewed_val, "isoformat") else (str(reviewed_val) if reviewed_val else None)

        formatted_submissions.append({
            "id": sub["submission_id"],
            "submissionId": sub["submission_id"],
            "studentId": sub["student_id"],
            "year": sub["year"],
            "area": sub["area_name"],
            "areaId": aid,
            "itemId": iid,
            "itemName": sub["item_name"],
            "detail": sub["detail"] or "",
            "activityDate": act_date_str,
            "description": sub["description"] or "",
            "filePath": sub["file_path"],
            "fileUrl": sub["file_path"],
            "originalFilename": sub["original_filename"],
            "linkUrl": sub["link_url"],
            "link": sub["link_url"],
            "status": st_text,
            "statusCode": st_code,
            "score": g_score,
            "grantedScore": g_score,
            "teacherComment": sub["teacher_comment"],
            "submittedAt": created_str,
            "reviewedAt": reviewed_str,
            "reviewerId": sub["reviewer_id"],
        })

    calc_res = calculate_student_certification(areas, all_submissions, all_merits)

    # 전체 상벌점 집계 (SCORE-02)
    total_merit_points = 0.0
    for m in all_merits:
        m_type = m.get("type")
        pts = float(m.get("points") or 0.0)
        signed_pts = -abs(pts) if m_type in ("-", "벌점") else abs(pts)
        total_merit_points += signed_pts
    point_total = round_decimal(total_merit_points, 1)

    response_data = {
        "studentId": student_id,
        "id": student_id,
        "name": target_student["name"],
        "grade": st_grade,
        "classNo": st_class,
        "number": st_number,
        "studentNo": st_number,
        "email": target_student.get("email"),
        "year": target_year,
        "areas": calc_res["areas"],
        "totalScore": calc_res["totalScore"],
        "certStatus": calc_res["certStatus"],
        "pointTotal": point_total,
        "pendingCount": calc_res["pendingCount"],
        "submissions": formatted_submissions,
        "student": {
            "studentId": student_id,
            "id": student_id,
            "name": target_student["name"],
            "grade": st_grade,
            "classNo": st_class,
            "number": st_number,
            "studentNo": st_number,
            "email": target_student.get("email"),
        },
    }

    return SrFormat(
        status_code=status.HTTP_200_OK,
        success=True,
        data=response_data,
        error=None,
    ).model_dump()
