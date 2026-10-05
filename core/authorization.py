"""
core/authorization.py - 중앙 집중식 학생 리소스 인가(Authorization) 및 권한 제어 모듈
(SEC-01: 모든 학생 리소스에 동일한 교사 권한 범위 적용)
"""

from typing import Any, Dict, Optional
from fastapi.responses import JSONResponse
from asyncmy.cursors import DictCursor

from sr_format import Error, SrFormat


async def get_student_academic_record(
    student_id: int,
    conn: Any,
    year: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """학생의 특정 학년도(또는 활성/최신 학년도) 학적 정보를 조회합니다."""
    async with conn.cursor(cursor=DictCursor) as cur:
        if year is not None:
            await cur.execute(
                """
                SELECT sar.grade, sar.class AS class_no, sar.number, ay.year, ay.year_id
                FROM student_academic_records sar
                JOIN academic_years ay ON sar.year_id = ay.year_id
                WHERE sar.student_id = %s AND ay.year = %s
                LIMIT 1
                """,
                (student_id, year),
            )
            row = await cur.fetchone()
            if row:
                return row

        # 학년도 미지정 시 활성 학년도 우선 조회, 없으면 최신 학년도 조회
        await cur.execute(
            """
            SELECT sar.grade, sar.class AS class_no, sar.number, ay.year, ay.year_id
            FROM student_academic_records sar
            JOIN academic_years ay ON sar.year_id = ay.year_id
            WHERE sar.student_id = %s
            ORDER BY ay.is_activated DESC, ay.year DESC
            LIMIT 1
            """,
            (student_id,),
        )
        return await cur.fetchone()


async def authorize_student_access(
    actor: Dict[str, Any],
    student_id: int,
    conn: Any,
    year: Optional[int] = None,
    action: str = "read",
) -> Optional[JSONResponse]:
    """
    모든 학생 리소스(인증현황, 제출목록, 상세, 티켓, 다운로드, 상벌점, 승인/반려 등)에 대해
    일관된 인가 검증을 수행합니다. (SEC-01)

    - 관리자(admin): 전면 허용
    - 학생(student): 본인 student_id 일치 시에만 허용, 타 학생 접근 시 403 FORBIDDEN
    - 교사(teacher):
        - 담임교사(grade, class 배정): 담당 학급(학년/반 일치) 학생만 접근 허용, 타 학급 403 FORBIDDEN
        - 교과교사(grade, class 미배정): 전교생 조회/심사 허용
        - 학생 전용 액션(직접 제출, 본인 삭제 등)은 교사 수행 불가 (403 FORBIDDEN)
    - 알 수 없는 역할: 403 FORBIDDEN
    """
    actor_role = actor.get("role")
    actor_uuid = actor.get("uuid") or actor.get("sub")

    # 1. 역할 정규화
    is_admin = actor_role in ("admin", 2, "2")
    is_teacher = actor_role in ("teacher", 1, "1")
    is_student = actor_role in ("student", 0, "0")

    if is_admin:
        return None

    # 2. 학생 역할 인가 검증
    if is_student:
        if action in ("review", "approve", "reject", "score_modify", "manage_points"):
            return JSONResponse(
                status_code=403,
                content=SrFormat(
                    status_code=403,
                    success=False,
                    data=None,
                    error=Error(code="FORBIDDEN", message="학생은 심사 또는 상벌점 관리 권한이 없습니다."),
                ).model_dump(),
            )

        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute(
                "SELECT student_id, uuid FROM students WHERE uuid = %s AND is_deleted = FALSE LIMIT 1",
                (actor_uuid,),
            )
            st_row = await cur.fetchone()
            if not st_row:
                await cur.execute(
                    "SELECT student_id, uuid FROM students WHERE student_id = %s AND is_deleted = FALSE LIMIT 1",
                    (student_id,),
                )
                target_st = await cur.fetchone()
                if target_st and target_st.get("uuid") == actor_uuid:
                    return None
                if target_st and target_st.get("uuid") != actor_uuid:
                    return JSONResponse(
                        status_code=403,
                        content=SrFormat(
                            status_code=403,
                            success=False,
                            data=None,
                            error=Error(code="FORBIDDEN", message="본인의 데이터만 접근할 수 있습니다."),
                        ).model_dump(),
                    )
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="USER_NOT_FOUND", message="학생 계정 정보를 찾을 수 없습니다."),
                    ).model_dump(),
                )
            if st_row["student_id"] != student_id:
                return JSONResponse(
                    status_code=403,
                    content=SrFormat(
                        status_code=403,
                        success=False,
                        data=None,
                        error=Error(code="FORBIDDEN", message="본인의 데이터만 접근할 수 있습니다."),
                    ).model_dump(),
                )
        return None

    # 3. 교사 역할 인가 검증
    if is_teacher:
        if action in ("student_submit", "student_resubmit", "student_delete"):
            return JSONResponse(
                status_code=403,
                content=SrFormat(
                    status_code=403,
                    success=False,
                    data=None,
                    error=Error(code="FORBIDDEN", message="교사는 학생 본인 제출/삭제 작업을 직접 수행할 수 없습니다."),
                ).model_dump(),
            )

        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute(
                "SELECT teachers_id, grade, class FROM teachers WHERE uuid = %s AND is_deleted = FALSE LIMIT 1",
                (actor_uuid,),
            )
            t_row = await cur.fetchone()
            if not t_row:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="TEACHER_NOT_FOUND", message="교사 정보를 찾을 수 없습니다."),
                    ).model_dump(),
                )

        t_grade = t_row.get("grade")
        t_class = t_row.get("class")

        # 담임교사 스코프 검증
        if t_grade is not None and t_class is not None:
            academic_record = await get_student_academic_record(student_id, conn, year)
            if not academic_record:
                return JSONResponse(
                    status_code=404,
                    content=SrFormat(
                        status_code=404,
                        success=False,
                        data=None,
                        error=Error(code="USER_NOT_FOUND", message="해당 학년도의 학생 학적 정보를 찾을 수 없습니다."),
                    ).model_dump(),
                )

            st_grade = academic_record.get("grade")
            st_class = academic_record.get("class_no")
            if st_grade != t_grade or st_class != t_class:
                return JSONResponse(
                    status_code=403,
                    content=SrFormat(
                        status_code=403,
                        success=False,
                        data=None,
                        error=Error(code="FORBIDDEN", message="담당 학급 학생의 데이터만 접근할 수 있습니다."),
                    ).model_dump(),
                )

        return None

    # 4. 알 수 없는 역할 차단
    return JSONResponse(
        status_code=403,
        content=SrFormat(
            status_code=403,
            success=False,
            data=None,
            error=Error(code="FORBIDDEN", message="유효하지 않은 권한 역할입니다."),
        ).model_dump(),
    )


async def authorize_submission_access(
    actor: Dict[str, Any],
    submission_id: int,
    conn: Any,
    action: str = "read",
) -> Optional[JSONResponse]:
    """
    제출 ID(submission_id)를 통해 역조회한 뒤 authorize_student_access를 적용합니다. (SEC-01)
    """
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT s.submission_id, s.student_id, st.uuid AS student_uuid,
                   COALESCE(ay.year, 2026) AS year, s.is_deleted, s.status_code
            FROM submissions s
            JOIN students st ON s.student_id = st.student_id
            LEFT JOIN evaluation_items ei ON s.item_id = ei.item_id
            LEFT JOIN certification_areas ca ON ei.area_id = ca.area_id
            LEFT JOIN academic_years ay ON ca.year_id = ay.year_id
            WHERE s.submission_id = %s
            LIMIT 1
            """,
            (submission_id,),
        )
        sub_row = await cur.fetchone()
        if not sub_row or sub_row.get("is_deleted"):
            return JSONResponse(
                status_code=404,
                content=SrFormat(
                    status_code=404,
                    success=False,
                    data=None,
                    error=Error(code="ITEM_NOT_FOUND", message="해당 증빙자료를 찾을 수 없습니다."),
                ).model_dump(),
            )

    return await authorize_student_access(
        actor=actor,
        student_id=sub_row["student_id"],
        conn=conn,
        year=sub_row.get("year"),
        action=action,
    )
