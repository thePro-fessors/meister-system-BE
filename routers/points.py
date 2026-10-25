"""
routers/points.py - 마이스터 역량인증제 상벌점 내역 조회 API 라우터

명세 및 연동 기준:
- Tech_spec.md 2.5 (상벌점 내역 조회: GET /api/points)
- TODO.md 2.5 (내 상벌점 내역 조회 API: GET /api/points)
"""

from datetime import datetime
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, Depends, Query

import core.authorization as authz
from core.security import error_response, normalize_role, get_current_user
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
    student_id: Optional[Union[int, str]] = Query(None, alias="studentId", description="학생 식별자 (선택)"),
    student_id_alt: Optional[Union[int, str]] = Query(None, alias="student_id", description="학생 식별자 (선택)"),
    user_id: Optional[str] = Query(None, alias="userId", description="학생 로그인 ID (선택)"),
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
    target_id = student_id if student_id is not None else (
        student_id_alt if student_id_alt is not None else user_id
    )
    return await handle_get_points(
        conn=conn,
        current_user=current_user,
        student_id_param=target_id,
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
    student_id: Optional[Union[int, str]] = None
    studentId: Optional[Union[int, str]] = None
    user_id: Optional[str] = None
    userId: Optional[str] = None
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
    score: Optional[float] = None
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
    raw_target_id = req.student_id if req.student_id is not None else (
        req.studentId if req.studentId is not None else (
            req.user_id if req.user_id is not None else req.userId
        )
    )
    if raw_target_id is None:
        return error_response(400, "VALIDATION_ERROR", "학생 식별자(studentId)가 필요합니다.")

    target_student_id = await authz.resolve_student_id(conn, raw_target_id)
    if target_student_id is None:
        return error_response(404, "USER_NOT_FOUND", "해당 학생을 찾을 수 없습니다.")

    auth_err = await authz.authorize_student_access(current_user, target_student_id, conn, action="manage_points")
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
        return error_response(400, "VALIDATION_ERROR", "상벌점 점수(score/points)는 필수 입력 항목입니다.")

    # 점수 상하한선 및 정수 규칙 검증 (FE validPointMagnitude와 통일: 1~5 정수, 음수로 들어온 경우 절댓값 기준 허용)
    abs_points = abs(raw_points)
    if abs_points < 1 or abs_points > 5 or not float(abs_points).is_integer():
        return error_response(400, "VALIDATION_ERROR", "상벌점 점수는 1 이상 5 이하의 정수여야 합니다.")

    # 부호 및 구분 정규화 (SCORE-02 & FE 호환: 점수가 음수이면 자동으로 벌점으로 판정)
    raw_type = (req.type or "").strip().upper()
    if raw_points < 0:
        norm_type = "-"
        norm_score = -abs_points
    elif raw_type in ("MERIT", "+", "상점"):
        norm_type = "+"
        norm_score = abs_points
    elif raw_type in ("DEMERIT", "-", "벌점"):
        norm_type = "-"
        norm_score = -abs_points
    else:
        return error_response(400, "VALIDATION_ERROR", "상벌점 유형은 상점(MERIT/+) 또는 벌점(DEMERIT/-)이어야 합니다.")

    # 사유(reason) 필수 검증 (출력 시점 이스케이프 원칙에 따라 저장 시점 html.escape 제거)
    raw_reason = (req.reason or "").strip()
    if not raw_reason:
        return error_response(400, "VALIDATION_ERROR", "상벌점 부여 사유(reason)는 필수 입력 항목입니다.")
    escaped_reason = raw_reason

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

        try:
            from core.audit import record_audit_log
            await record_audit_log(
                conn=conn,
                actor=current_user,
                action_type="POINT_ADD",
                target_type="MERIT",
                target_id=point_id,
                target_name=f"{norm_type} {norm_score}점 ({rel_area or '기본'})",
                details={"student_id": target_student_id, "type": norm_type, "points": norm_score, "reason": escaped_reason},
            )
        except Exception:
            pass
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    # 학생 계정 ID(users.id) 및 성명 조회 (FE 호환성)
    student_user_id = None
    student_name = None
    try:
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute(
                """
                SELECT s.name, u.id AS user_id 
                FROM students s 
                LEFT JOIN users u ON s.uuid = u.uuid 
                WHERE s.student_id = %s 
                LIMIT 1
                """,
                (target_student_id,),
            )
            st_row = await cur.fetchone()
            if st_row:
                student_user_id = st_row.get("user_id") if isinstance(st_row, dict) else st_row[1]
                student_name = st_row.get("name") if isinstance(st_row, dict) else st_row[0]
    except Exception:
        pass

    final_user_id = student_user_id or req.user_id or req.userId or (str(raw_target_id) if not str(raw_target_id).isdigit() else None)

    return JSONResponse(
        status_code=201,
        content=SrFormat(
            status_code=201,
            success=True,
            data={
                "id": point_id,
                "pointId": point_id,
                "studentId": target_student_id,
                "userId": final_user_id,
                "user_id": final_user_id,
                "studentName": student_name,
                "name": student_name,
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
        return error_response(404, "ITEM_NOT_FOUND", "해당 상벌점 내역을 찾을 수 없습니다.")

    auth_err = await authz.authorize_student_access(current_user, existing["student_id"], conn, action="manage_points")
    if auth_err:
        return auth_err
    mut_err = await authz.authorize_point_mutation(current_user, existing, conn, action="modify")
    if mut_err:
        return mut_err

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

    input_points = req.points if req.points is not None else req.score
    if input_points is not None:
        abs_pts = abs(input_points)
        if abs_pts < 1 or abs_pts > 5 or not float(abs_pts).is_integer():
            return error_response(400, "VALIDATION_ERROR", "상벌점 점수는 1 이상 5 이하의 정수여야 합니다.")
        # FE가 score에 부호를 명시하여 전송한 경우 자동 타입 보정
        if req.score is not None and req.type is None:
            if req.score < 0:
                new_type = "-"
            elif req.score > 0:
                new_type = "+"
        if new_type == "+":
            new_points = abs_pts
        else:
            new_points = -abs_pts
    else:
        new_points = old_points

    if req.reason is not None:
        raw_reason = req.reason.strip()
        if not raw_reason:
            return error_response(400, "VALIDATION_ERROR", "수정 사유는 빈 문자열일 수 없습니다.")
        new_reason = raw_reason
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

            edit_history = []
            if hasattr(cur, "fetchall"):
                try:
                    await cur.execute(
                        """
                        SELECT modifier_uuid, modify_reason, created_at
                        FROM merits_log
                        WHERE merits_point_id = %s AND action_type = 'UPDATE'
                        ORDER BY created_at ASC, log_id ASC
                        """,
                        (point_id,),
                    )
                    history_rows = await cur.fetchall() or []
                    for h in history_rows:
                        if isinstance(h, dict):
                            c_at = h.get("created_at")
                            edit_history.append({
                                "editorId": h.get("modifier_uuid", ""),
                                "reason": h.get("modify_reason") or "",
                                "at": c_at.isoformat() if hasattr(c_at, "isoformat") else str(c_at or ""),
                            })
                except Exception:
                    pass

            if not edit_history:
                edit_history = [{
                    "editorId": user_uuid or "",
                    "reason": new_reason or "",
                    "at": datetime.now().isoformat(),
                }]

        await conn.commit()

        try:
            from core.audit import record_audit_log
            await record_audit_log(
                conn=conn,
                actor=current_user,
                action_type="POINT_DELETE",
                target_type="MERIT",
                target_id=point_id,
                target_name=f"{existing['type']} {existing['points']}점 삭제",
                details={"student_id": existing["student_id"], "points": existing["points"], "reason": del_reason},
            )
        except Exception:
            pass
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
            "editHistory": edit_history,
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
        return error_response(404, "ITEM_NOT_FOUND", "해당 상벌점 내역을 찾을 수 없습니다.")

    auth_err = await authz.authorize_student_access(current_user, existing["student_id"], conn, action="manage_points")
    if auth_err:
        return auth_err
    mut_err = await authz.authorize_point_mutation(current_user, existing, conn, action="delete")
    if mut_err:
        return mut_err

    user_uuid = current_user.get("uuid") or current_user.get("sub")
    del_reason = (reason or "상벌점 삭제").strip()

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

        try:
            from core.audit import record_audit_log
            await record_audit_log(
                conn=conn,
                actor=current_user,
                action_type="POINT_DELETE",
                target_type="MERIT",
                target_id=point_id,
                target_name=f"{existing['type']} {existing['points']}점 삭제",
                details={"student_id": existing["student_id"], "points": existing["points"], "reason": del_reason},
            )
        except Exception:
            pass
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
