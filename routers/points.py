"""
routers/points.py - 마이스터 역량인증제 상벌점 내역 조회 API 라우터

명세 및 연동 기준:
- Tech_spec.md 2.5 (상벌점 내역 조회: GET /api/points)
- TODO.md 2.5 (내 상벌점 내역 조회 API: GET /api/points)
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from core.security import get_current_user
from database import get_db
from routers.students import handle_get_points
from sr_format import SrFormat

router = APIRouter(
    prefix="/api/points",
    tags=["points"],
)


@router.get(
    "",
    summary="내 상벌점 내역 조회 API (GET /api/points)",
    response_model=SrFormat,
)
async def get_my_points(
    student_id: Optional[int] = Query(None, alias="studentId", description="학생 식별자 (선택)"),
    year: Optional[int] = Query(None, description="학년도 필터 (선택)"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 본인 상벌점 내역 조회 엔드포인트
    
    엔드포인트: GET /api/points
    - 학생 본인 데이터 강제 필터링 (타 학생 ID 조회 시 403 Forbidden 차단)
    - 교사/관리자는 studentId 지정 시 해당 학생 내역 조회 허용
    - year: 선택적 학사년도 필터링
    """
    return await handle_get_points(
        conn=conn,
        current_user=current_user,
        student_id_param=student_id,
        year_val=year,
    )


from pydantic import BaseModel
from fastapi.responses import JSONResponse
from sr_format import Error
try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore


class CreatePointRequest(BaseModel):
    student_id: Optional[int] = None
    studentId: Optional[int] = None
    type: str  # "MERIT" or "DEMERIT" or "+" or "-" or "상점" or "벌점"
    points: Optional[float] = None
    score: Optional[float] = None
    reason: str
    occurred_at: Optional[str] = None
    occurredAt: Optional[str] = None
    date: Optional[str] = None
    issued_date: Optional[str] = None
    issuedDate: Optional[str] = None
    related_area: Optional[str] = None
    relatedArea: Optional[str] = None
    reflected_area: Optional[str] = None
    reflectedArea: Optional[str] = None


class UpdatePointRequest(BaseModel):
    points: Optional[float] = None
    reason: Optional[str] = None
    type: Optional[str] = None
    occurred_at: Optional[str] = None
    occurredAt: Optional[str] = None
    related_area: Optional[str] = None
    relatedArea: Optional[str] = None


@router.post(
    "",
    summary="상벌점 신규 등록 API (API-02)",
    response_model=SrFormat,
    status_code=201,
)
async def create_point(
    req: CreatePointRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 상벌점 신규 부여 엔드포인트 (API-02)
    - 교사 및 관리자 전용
    - merits 및 merits_log 테이블 기록
    - 상점(+)/벌점(-) 부호 정규화 (SCORE-02)
    """
    target_student_id = req.student_id if req.student_id is not None else req.studentId
    if target_student_id is None:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="학생 식별자(studentId)가 필요합니다."),
            ).model_dump(),
        )

    from core.authorization import authorize_student_access
    auth_err = await authorize_student_access(current_user, target_student_id, conn, action="manage_points")
    if auth_err:
        return auth_err

    user_uuid = current_user.get("uuid") or current_user.get("sub")

    # 교사 식별자 확인
    reviewer_id = None
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT teachers_id FROM teachers WHERE uuid = %s AND is_deleted = FALSE LIMIT 1", (user_uuid,))
        t_row = await cur.fetchone()
        if t_row:
            reviewer_id = t_row["teachers_id"]

    # 입력 점수 추출 (points 또는 score)
    raw_points = req.points if req.points is not None else req.score
    if raw_points is None:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="상벌점 점수(score/points)는 필수 입력 항목입니다."),
            ).model_dump(),
        )

    # 점수 상하한선 방어 검증 (0 초과 100 이하)
    if raw_points <= 0 or raw_points > 100:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="상벌점 점수는 0 초과 100 이하의 숫자여야 합니다."),
            ).model_dump(),
        )

    # 부호 및 구분 정규화 (SCORE-02)
    raw_type = req.type.strip().upper()
    if raw_type in ("MERIT", "+", "상점"):
        norm_type = "+"
        norm_score = abs(raw_points)
    elif raw_type in ("DEMERIT", "-", "벌점"):
        norm_type = "-"
        norm_score = -abs(raw_points)
    else:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="상벌점 유형은 상점(MERIT/+) 또는 벌점(DEMERIT/-)이어야 합니다."),
            ).model_dump(),
        )

    # 사유(reason) 필수 검증 및 XSS 방어 (SECURITY_AND_AUDIT.md & TODO.md 3.7)
    raw_reason = (req.reason or "").strip()
    if not raw_reason:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="상벌점 부여 사유(reason)는 필수 입력 항목입니다."),
            ).model_dump(),
        )
    import html
    escaped_reason = html.escape(raw_reason)

    occ_date = req.occurred_at or req.occurredAt or req.date or req.issued_date or req.issuedDate
    rel_area = req.related_area or req.relatedArea or req.reflected_area or req.reflectedArea

    try:
        await conn.autocommit(False)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO merits (
                    student_id, teachers_id, type, points, reason, related_area, occurred_at, created_at, is_reflected, is_deleted
                )
                VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, CURDATE()), NOW(), TRUE, FALSE)
                """,
                (target_student_id, reviewer_id, norm_type, norm_score, escaped_reason, rel_area, occ_date),
            )
            await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
            last_row = await cur.fetchone()
            point_id = last_row[0] if isinstance(last_row, (list, tuple)) else last_row["last_id"]

            await cur.execute(
                """
                INSERT INTO merits_log (
                    merits_point_id, modifier_uuid, action_type, old_points, new_points, modify_reason, created_at
                )
                VALUES (%s, %s, 'CREATE', NULL, %s, %s, NOW())
                """,
                (point_id, user_uuid, norm_score, escaped_reason),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    from datetime import datetime
    return JSONResponse(
        status_code=201,
        content=SrFormat(
            status_code=201,
            success=True,
            data={
                "id": point_id,
                "pointId": point_id,
                "studentId": target_student_id,
                "teacherId": reviewer_id,
                "type": norm_type,
                "points": norm_score,
                "score": norm_score,
                "reason": escaped_reason,
                "occurredAt": occ_date,
                "date": occ_date,
                "reflectedArea": rel_area,
                "createdAt": datetime.now().isoformat(),
            },
        ).model_dump(),
    )


@router.patch(
    "/{point_id}",
    summary="상벌점 수정 API (API-02)",
    response_model=SrFormat,
)
async def update_point(
    point_id: int,
    req: UpdatePointRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 상벌점 내역 수정 엔드포인트 (API-02 / TODO.md 3.8)
    - 교사 및 관리자 전용
    - 수정 사유 필수 입력 및 XSS 방어
    - 수정 이력(merits_log) 보존
    """
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT merits_point_id, student_id, teachers_id, type, points, reason, related_area, occurred_at, is_deleted
            FROM merits
            WHERE merits_point_id = %s AND is_deleted = FALSE
            LIMIT 1
            """,
            (point_id,),
        )
        existing = await cur.fetchone()

    if not existing:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="ITEM_NOT_FOUND", message="해당 상벌점 내역을 찾을 수 없습니다."),
            ).model_dump(),
        )

    from core.authorization import authorize_student_access
    auth_err = await authorize_student_access(current_user, existing["student_id"], conn, action="manage_points")
    if auth_err:
        return auth_err

    user_uuid = current_user.get("uuid") or current_user.get("sub")

    old_points = float(existing["points"])
    old_type = existing["type"]

    new_type = old_type
    if req.type:
        t_clean = req.type.strip().upper()
        if t_clean in ("MERIT", "+", "상점"):
            new_type = "+"
        elif t_clean in ("DEMERIT", "-", "벌점"):
            new_type = "-"

    if req.points is not None:
        if req.points <= 0 or req.points > 100:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="VALIDATION_ERROR", message="상벌점 점수는 0 초과 100 이하의 숫자여야 합니다."),
                ).model_dump(),
            )
        if new_type == "+":
            new_points = abs(req.points)
        else:
            new_points = -abs(req.points)
    else:
        new_points = old_points

    if req.reason is not None:
        raw_reason = req.reason.strip()
        if not raw_reason:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="VALIDATION_ERROR", message="수정 사유는 빈 문자열일 수 없습니다."),
                ).model_dump(),
            )
        import html
        new_reason = html.escape(raw_reason)
    else:
        new_reason = existing["reason"]

    new_occ = req.occurred_at or req.occurredAt or existing["occurred_at"]
    new_area = req.related_area or req.relatedArea or existing["related_area"]

    try:
        await conn.autocommit(False)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE merits
                SET type = %s,
                    points = %s,
                    reason = %s,
                    related_area = %s,
                    occurred_at = %s
                WHERE merits_point_id = %s AND is_deleted = FALSE
                """,
                (new_type, new_points, new_reason, new_area, new_occ, point_id),
            )
            await cur.execute(
                """
                INSERT INTO merits_log (
                    merits_point_id, modifier_uuid, action_type, old_points, new_points, modify_reason, created_at
                )
                VALUES (%s, %s, 'UPDATE', %s, %s, %s, NOW())
                """,
                (point_id, user_uuid, old_points, new_points, new_reason),
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
            "id": point_id,
            "studentId": existing["student_id"],
            "type": new_type,
            "points": new_points,
            "reason": new_reason,
            "occurredAt": str(new_occ) if new_occ else None,
        },
    ).model_dump()


@router.delete(
    "/{point_id}",
    summary="상벌점 삭제 API (API-02 / TODO.md 3.8)",
    response_model=SrFormat,
)
async def delete_point(
    point_id: int,
    reason: Optional[str] = Query(None, description="삭제 사유 (선택)"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 상벌점 내역 안전 삭제(Soft-Delete) 엔드포인트 (API-02 / TODO.md 3.8)
    - 교사 및 관리자 전용
    - merits 테이블 is_deleted = TRUE 갱신
    - merits_log에 DELETE 감사 로그 원자적 기록
    """
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT merits_point_id, student_id, teachers_id, type, points, reason, is_deleted
            FROM merits
            WHERE merits_point_id = %s AND is_deleted = FALSE
            LIMIT 1
            """,
            (point_id,),
        )
        existing = await cur.fetchone()

    if not existing:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="ITEM_NOT_FOUND", message="해당 상벌점 내역을 찾을 수 없습니다."),
            ).model_dump(),
        )

    from core.authorization import authorize_student_access
    auth_err = await authorize_student_access(current_user, existing["student_id"], conn, action="manage_points")
    if auth_err:
        return auth_err

    user_uuid = current_user.get("uuid") or current_user.get("sub")
    import html
    del_reason = html.escape((reason or "상벌점 삭제").strip())

    try:
        await conn.autocommit(False)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE merits SET is_deleted = TRUE WHERE merits_point_id = %s",
                (point_id,),
            )
            await cur.execute(
                """
                INSERT INTO merits_log (
                    merits_point_id, modifier_uuid, action_type, old_points, new_points, modify_reason, created_at
                )
                VALUES (%s, %s, 'DELETE', %s, NULL, %s, NOW())
                """,
                (point_id, user_uuid, existing["points"], del_reason),
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
        data={"id": point_id, "deleted": True},
    ).model_dump()
