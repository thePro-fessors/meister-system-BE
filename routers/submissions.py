"""
routers/submissions.py - 마이스터 역량인증제 증빙자료 제출 및 심사 API 라우터

명세 및 연동 기준:
- Tech_spec.md 2.2 (증빙자료 제출: POST /api/submissions)
- TODO.md 2.2 (증빙자료 신규 제출 API: POST /api/submissions)
"""

import html
import os
from datetime import date, datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

from core.logger import logger
from core.security import get_current_user
from database import get_db, get_redis
from routers.students import handle_submit_evidence, handle_get_submissions
from pydantic import BaseModel
from sr_format import Error, SrFormat
import redis.asyncio as aioredis

SUBMISSION_STATUS_MAP = {
    1: "제출완료",
    2: "검토중",
    3: "인정완료",
    4: "반려",
    5: "재제출요청",
}


class ApproveSubmissionRequest(BaseModel):
    score: Optional[float] = None
    granted_score: Optional[float] = None
    grantedScore: Optional[float] = None
    comment: Optional[str] = None
    teacher_comment: Optional[str] = None
    teacherComment: Optional[str] = None


class RejectSubmissionRequest(BaseModel):
    comment: Optional[str] = None
    reason: Optional[str] = None
    teacher_comment: Optional[str] = None
    teacherComment: Optional[str] = None
    status_code: Optional[int] = None
    statusCode: Optional[int] = None


class ModifyScoreRequest(BaseModel):
    score: Optional[float] = None
    granted_score: Optional[float] = None
    grantedScore: Optional[float] = None
    reason: Optional[str] = None
    comment: Optional[str] = None
    teacher_comment: Optional[str] = None
    teacherComment: Optional[str] = None


router = APIRouter(
    prefix="/api/submissions",
    tags=["submissions"],
)


@router.get(
    "",
    summary="내 제출 내역 조회 API (GET /api/submissions)",
    response_model=SrFormat,
)
async def get_my_submissions(
    student_id: Optional[int] = Query(None, alias="studentId", description="학생 식별자 (선택)"),
    year: Optional[int] = Query(None, description="학년도 필터 (선택)"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 본인 증빙 제출 내역 조회 엔드포인트
    
    엔드포인트: GET /api/submissions
    - 학생 본인 제출 건 강제 필터링 (타 학생 ID 조회 시 403 Forbidden 차단)
    - 교사/관리자는 studentId 지정 시 해당 학생 제출 목록 조회 허용
    - year: 선택적 학사년도 필터링
    """
    return await handle_get_submissions(
        conn=conn,
        current_user=current_user,
        student_id_param=student_id,
        year_val=year,
    )



@router.post(
    "",
    summary="증빙자료 신규 제출 API (POST /api/submissions)",
    response_model=SrFormat,
)
async def submit_evidence_direct(
    item_id: Optional[int] = Form(None),
    itemId: Optional[int] = Form(None),
    detail: Optional[str] = Form(None),
    activity_date: Optional[str] = Form(None),
    activityDate: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    link_url: Optional[str] = Form(None),
    link: Optional[str] = Form(None),
    student_id: Optional[int] = Form(None),
    studentId: Optional[int] = Form(None),
    year: Optional[int] = Form(None),
    area: Optional[str] = Form(None),
    areaId: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    증빙자료 신규 제출 엔드포인트 (JWT 토큰 기반 학생 식별)
    
    엔드포인트: POST /api/submissions
    """
    target_student_id = student_id if student_id is not None else studentId
    target_item_id = item_id if item_id is not None else itemId
    target_date = activity_date if activity_date is not None else activityDate
    target_link = link_url if link_url is not None else link
    target_area = area if area is not None else areaId

    return await handle_submit_evidence(
        conn=conn,
        current_user=current_user,
        student_id_param=target_student_id,
        item_id_val=target_item_id,
        detail_val=detail,
        activity_date_val=target_date,
        description_val=description,
        link_url_val=target_link,
        file_val=file,
        year_val=year,
        area_val=target_area,
        redis=redis,
    )


@router.patch(
    "/{submission_id}/resubmit",
    summary="증빙자료 재제출 API (PATCH /api/submissions/{submissionId}/resubmit)",
    response_model=SrFormat,
)
async def resubmit_evidence(
    submission_id: int,
    description: Optional[str] = Form(None),
    link_url: Optional[str] = Form(None),
    link: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    반려된 증빙자료 재제출 엔드포인트 (TODO.md 2.6 및 Tech_spec.md 2.4)
    
    보안 및 안정성 제어:
    1. 학생 본인 소유권 검증 (타 학생 증빙 수정 시 403 Forbidden 차단)
    2. 인정 완료(status=3) 건 수정 차단 (400 CANNOT_RESUBMIT_APPROVED)
    3. 반려(4) 또는 재제출요청(5) 상태에서만 수정 허용
    4. 신규 파일 업로드 시 기존 디스크 파일 안전 삭제(고아 파일 방지 GC 연동)
    5. DB 에러 시 신규 업로드 파일 자동 롤백 및 삭제
    6. 상태를 검토중(1)으로 변경하고 submissions_logs에 재제출 이력 기록
    """
    from fastapi.responses import JSONResponse
    from core.storage import save_upload_file, delete_uploaded_file, cleanup_old_file_on_resubmit
    from routers.students import is_valid_evidence_url
    from sr_format import Error

    user_role = current_user.get("role")
    user_uuid = current_user.get("uuid")

    if user_role not in ("student", "0", 0):
        return JSONResponse(
            status_code=403,
            content=SrFormat(
                status_code=403,
                success=False,
                data=None,
                error=Error(code="FORBIDDEN", message="학생 본인만 증빙자료를 재제출할 수 있습니다."),
            ).model_dump(),
        )

    # 재제출 동시성 경합(Race Condition) 방어 분산 락
    lock_acquired = False
    lock_key = f"lock:submission:resubmit:{submission_id}"
    if redis is not None:
        try:
            lock_acquired = await redis.set(lock_key, "1", nx=True, ex=10)
        except Exception:
            lock_acquired = True
        if not lock_acquired:
            return JSONResponse(
                status_code=409,
                content=SrFormat(
                    status_code=409,
                    success=False,
                    data=None,
                    error=Error(code="CONCURRENT_REQUEST", message="이미 재제출 처리가 진행 중입니다. 잠시 후 다시 시도해주세요."),
                ).model_dump(),
            )

    try:
        # 1. 기존 제출 건 조회
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute(
                """
                SELECT s.submission_id, s.student_id, s.status_code, s.granted_score, s.file_path, s.link_url, s.description, st.uuid
                FROM submissions s
                JOIN students st ON s.student_id = st.student_id
                WHERE s.submission_id = %s AND s.is_deleted = FALSE
                LIMIT 1
                """,
                (submission_id,),
            )
            existing = await cur.fetchone()

        if not existing:
            return JSONResponse(
                status_code=404,
                content=SrFormat(
                    status_code=404,
                    success=False,
                    data=None,
                    error=Error(code="NOT_FOUND", message="해당 증빙자료를 찾을 수 없습니다."),
                ).model_dump(),
            )

        # 2. 소유권 검증 (IDOR 방어)
        if existing["uuid"] != user_uuid:
            return JSONResponse(
                status_code=403,
                content=SrFormat(
                    status_code=403,
                    success=False,
                    data=None,
                    error=Error(code="FORBIDDEN", message="본인이 제출한 증빙자료만 재제출할 수 있습니다."),
                ).model_dump(),
            )

        # 3. 상태 검증: 이미 인정 완료된 건 수정 차단
        if existing["status_code"] == 3:
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="CANNOT_RESUBMIT_APPROVED", message="이미 승인 완료된 증빙자료는 수정할 수 없습니다."),
                ).model_dump(),
            )

        if existing["status_code"] not in (4, 5):
            return JSONResponse(
                status_code=400,
                content=SrFormat(
                    status_code=400,
                    success=False,
                    data=None,
                    error=Error(code="INVALID_STATUS", message="반려(4) 또는 재제출요청(5) 상태의 증빙자료만 재제출할 수 있습니다."),
                ).model_dump(),
            )

        # 링크 유효성 검증
        if link_url or link:
            raw_link = (link_url or link or "").strip()
            if raw_link:
                if not is_valid_evidence_url(raw_link):
                    return JSONResponse(
                        status_code=400,
                        content=SrFormat(
                            status_code=400,
                            success=False,
                            data=None,
                            error=Error(
                                code="VALIDATION_ERROR",
                                message="외부 링크는 http:// 또는 https:// 형식의 올바른 웹 주소(도메인 포함)여야 합니다.",
                            ),
                        ).model_dump(),
                    )
                if len(raw_link) > 500:
                    return JSONResponse(
                        status_code=400,
                        content=SrFormat(
                            status_code=400,
                            success=False,
                            data=None,
                            error=Error(code="VALIDATION_ERROR", message="외부 링크는 최대 500자까지 입력 가능합니다."),
                        ).model_dump(),
                    )

        raw_desc = (description or "").strip()
        new_desc = html.escape(raw_desc) if raw_desc else existing["description"]
        target_link = (link_url or link or "").strip() or existing["link_url"]
        old_file_path = existing["file_path"]

        new_file_path = old_file_path
        new_disk_path = None

        # 신규 파일 업로드 처리
        new_orig_filename = None
        if file and file.filename:
            save_res = await save_upload_file(file, subfolder="submissions")
            new_file_path = save_res["file_path"]
            new_disk_path = save_res["disk_path"]
            new_orig_filename = save_res.get("original_filename") or file.filename

        try:
            await conn.autocommit(False)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE submissions
                    SET description = %s,
                        file_path = %s,
                        original_filename = COALESCE(%s, original_filename),
                        link_url = %s,
                        status_code = 2,
                        granted_score = NULL,
                        reviewer_id = NULL,
                        reviewed_at = NULL,
                        teacher_comment = NULL,
                        updated_at = NOW()
                    WHERE submission_id = %s AND status_code IN (4, 5)
                    """,
                    (new_desc, new_file_path, new_orig_filename, target_link, submission_id),
                )

                if cur.rowcount == 0:
                    await conn.rollback()
                    if new_disk_path:
                        delete_uploaded_file(new_disk_path)
                    return JSONResponse(
                        status_code=409,
                        content=SrFormat(
                            status_code=409,
                            success=False,
                            data=None,
                            error=Error(code="CONFLICT", message="이미 상태가 변경되었거나 재제출 가능한 상태가 아닙니다."),
                        ).model_dump(),
                    )

                # submissions_logs 스키마 준수 이력 기록 (DATA-01)
                await cur.execute(
                    """
                    INSERT INTO submissions_logs (
                        submission_id, modifier_uuid, action_type,
                        old_status_code, new_status_code, old_score, new_score, comment, created_at
                    )
                    VALUES (%s, %s, '재제출', %s, 2, %s, NULL, %s, NOW())
                    """,
                    (submission_id, user_uuid, existing["status_code"], existing.get("granted_score"), new_desc),
                )
            await conn.commit()
        except Exception as e:
            await conn.rollback()
            # 트랜잭션 실패 시 방금 업로드된 새 파일 즉시 롤백 삭제 (고아 파일 방지)
            if new_disk_path:
                delete_uploaded_file(new_disk_path)
            raise
        finally:
            await conn.autocommit(True)

        # 신규 파일이 정상 등록되었고 이전 파일이 존재한다면 구 파일 디스크 정리 (고아 파일 GC)
        if new_disk_path and old_file_path and old_file_path != new_file_path:
            cleanup_old_file_on_resubmit(old_file_path)

        return SrFormat(
            status_code=200,
            success=True,
            data={
                "id": submission_id,
                "status": "검토중",
                "statusCode": 2,
                "filePath": new_file_path,
                "linkUrl": target_link,
                "description": new_desc,
            },
        ).model_dump()
    finally:
        if lock_acquired and redis is not None:
            try:
                await redis.delete(lock_key)
            except Exception:
                pass


@router.post(
    "/{submission_id}/download-ticket",
    summary="증빙자료 안전 다운로드 일회용 티켓 발급 API",
    response_model=SrFormat,
)
async def get_submission_download_ticket(
    submission_id: int,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
):
    """
    개별 증빙자료 파일에 대해 안전하게 소비할 수 있는 60초 일회용 티켓을 발급합니다.
    (SECURITY_AND_AUDIT.md 1.2 & TODO.md 6.1 P1 / SEC-01 권한 격리 적용)
    """
    from fastapi.responses import JSONResponse
    from core.security import create_download_ticket
    from core.authorization import authorize_submission_access
    from sr_format import Error

    auth_err = await authorize_submission_access(current_user, submission_id, conn, action="read")
    if auth_err:
        return auth_err

    user_role = current_user.get("role")
    user_uuid = current_user.get("uuid")

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT s.submission_id, s.file_path, st.uuid
            FROM submissions s
            JOIN students st ON s.student_id = st.student_id
            WHERE s.submission_id = %s AND s.is_deleted = FALSE
            LIMIT 1
            """,
            (submission_id,),
        )
        row = await cur.fetchone()

    if not row or not row.get("file_path"):
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="FILE_NOT_FOUND", message="첨부된 증빙 파일이 없습니다."),
            ).model_dump(),
        )

    file_path = row["file_path"]
    raw_p = file_path.strip().lstrip("/\\")
    if raw_p.startswith("uploads/"):
        raw_p = raw_p[8:]
    clean_rel = os.path.normpath(raw_p).lstrip("/\\")

    ticket_id = await create_download_ticket(
        user_uuid=user_uuid,
        role=str(user_role),
        file_path=clean_rel,
        redis=redis,
        ttl_seconds=60,
    )
    from core.security import create_signed_download_token
    signed_token = create_signed_download_token(
        user_uuid=user_uuid,
        role=str(user_role),
        file_path=clean_rel,
        ttl_seconds=60,
    )

    url_path = f"/uploads/{clean_rel.replace(os.sep, '/')}"
    return SrFormat(
        status_code=200,
        success=True,
        data={
            "ticket": ticket_id,
            "signedToken": signed_token,
            "expiresIn": 60,
            "downloadUrl": f"{url_path}?ticket={ticket_id}",
            "fileUrl": f"{url_path}?ticket={ticket_id}",
            "signedUrl": f"{url_path}?token={signed_token}",
        },
    ).model_dump()


@router.delete(
    "/{submission_id}",
    summary="증빙자료 삭제 API (DELETE /api/submissions/{submissionId})",
    response_model=SrFormat,
)
async def delete_submission(
    submission_id: int,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    증빙자료 안전 삭제(Soft-Delete) 및 첨부파일 디스크 정리 엔드포인트
    - 학생 본인 전용 (교사 직접 삭제 불가 403 차단)
    """
    from fastapi.responses import JSONResponse
    from core.storage import cleanup_old_file_on_resubmit
    from core.authorization import authorize_submission_access
    from sr_format import Error

    auth_err = await authorize_submission_access(current_user, submission_id, conn, action="student_delete")
    if auth_err:
        return auth_err

    user_role = current_user.get("role")
    user_uuid = current_user.get("uuid")

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT s.submission_id, s.status_code, s.granted_score, s.file_path, st.uuid
            FROM submissions s
            JOIN students st ON s.student_id = st.student_id
            WHERE s.submission_id = %s AND s.is_deleted = FALSE
            LIMIT 1
            """,
            (submission_id,),
        )
        row = await cur.fetchone()

    if not row:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="NOT_FOUND", message="해당 증빙자료를 찾을 수 없습니다."),
            ).model_dump(),
        )

    if row["status_code"] == 3:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="CANNOT_DELETE_APPROVED", message="이미 승인 완료된 증빙자료는 삭제할 수 없습니다."),
            ).model_dump(),
        )

    file_to_clean = row.get("file_path")

    try:
        await conn.autocommit(False)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE submissions SET is_deleted = TRUE, updated_at = NOW() WHERE submission_id = %s",
                (submission_id,),
            )
            await cur.execute(
                """
                INSERT INTO submissions_logs (
                    submission_id, modifier_uuid, action_type,
                    old_status_code, new_status_code, old_score, new_score, comment, created_at
                )
                VALUES (%s, %s, '삭제', %s, %s, %s, NULL, '사용자 요청에 의한 증빙자료 삭제', NOW())
                """,
                (submission_id, user_uuid, row["status_code"], row["status_code"], row.get("granted_score")),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    # 증빙 삭제 시 디스크 고아 파일 즉시 정리 (SECURITY_AND_AUDIT.md 4.1 권고)
    if file_to_clean:
        cleanup_old_file_on_resubmit(file_to_clean)

    return SrFormat(
        status_code=200,
        success=True,
        data={"submissionId": submission_id, "deleted": True},
    ).model_dump()


@router.get(
    "/{submission_id}",
    summary="증빙자료 단건 상세 조회 API (API-01)",
    response_model=SrFormat,
)
async def get_submission_detail(
    submission_id: int,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    증빙자료 단건 상세 조회 엔드포인트 (API-01)
    - 학생 본인, 교사(담임/교과), 관리자 인가 적용 (SEC-01)
    """
    from core.authorization import authorize_submission_access
    auth_err = await authorize_submission_access(current_user, submission_id, conn, action="read")
    if auth_err:
        return auth_err

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT 
                s.submission_id AS id,
                s.student_id AS studentId,
                st.name AS studentName,
                sar.grade,
                sar.class AS classNo,
                sar.number,
                s.item_id AS itemId,
                ei.name AS itemName,
                ca.area_id AS areaId,
                ca.name AS areaName,
                s.detail,
                s.activity_date AS activityDate,
                s.description,
                s.link_url AS linkUrl,
                s.file_path AS filePath,
                s.original_filename AS originalFilename,
                s.status_code AS statusCode,
                s.granted_score AS grantedScore,
                ei.max_score AS maxScore,
                s.reviewer_id AS reviewerId,
                t.name AS reviewerName,
                s.reviewed_at AS reviewedAt,
                s.teacher_comment AS teacherComment,
                s.created_at AS createdAt,
                s.updated_at AS updatedAt
            FROM submissions s
            JOIN students st ON s.student_id = st.student_id
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id
            JOIN academic_years ay ON ca.year_id = ay.year_id
            LEFT JOIN student_academic_records sar ON sar.student_id = st.student_id AND sar.year_id = ay.year_id
            LEFT JOIN teachers t ON s.reviewer_id = t.teachers_id
            WHERE s.submission_id = %s AND s.is_deleted = FALSE
            LIMIT 1
            """,
            (submission_id,),
        )
        row = await cur.fetchone()

    if not row:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="ITEM_NOT_FOUND", message="증빙자료를 찾을 수 없습니다."),
            ).model_dump(),
        )

    st_code = row.get("statusCode", 1)
    row["status"] = SUBMISSION_STATUS_MAP.get(st_code, "제출완료")
    for date_field in ("activityDate", "reviewedAt", "createdAt", "updatedAt"):
        if row.get(date_field) and hasattr(row[date_field], "isoformat"):
            row[date_field] = row[date_field].isoformat()
        elif row.get(date_field):
            row[date_field] = str(row[date_field])

    if row.get("grantedScore") is not None:
        row["grantedScore"] = float(row["grantedScore"])
    if row.get("maxScore") is not None:
        row["maxScore"] = float(row["maxScore"])

    return SrFormat(
        status_code=200,
        success=True,
        data=row,
    ).model_dump()


async def _calculate_student_totals(
    student_id: int,
    year_id: Optional[int],
    target_year: int,
    conn: Any,
) -> Optional[Dict[str, Any]]:
    """심사 액션(승인/반려/점수수정) 후 학생 인증 현황 총점 및 상태 자동 재계산 (SCORE-01 공통화)"""
    try:
        from core.calculator import calculate_student_certification, round_decimal
        from datetime import date
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute(
                """
                SELECT sar.grade, sar.class AS class_no, sar.number
                FROM student_academic_records sar
                WHERE sar.student_id = %s AND sar.year_id = %s
                LIMIT 1
                """,
                (student_id, year_id),
            )
            sar_row = await cur.fetchone()
            st_grade = sar_row.get("grade", 1) if sar_row else 1

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

            await cur.execute(
                """
                SELECT 
                    s.submission_id,
                    s.item_id,
                    s.status_code,
                    s.granted_score,
                    ei.area_id,
                    ei.scoring_type,
                    ei.max_score AS item_max_score
                FROM submissions s
                JOIN evaluation_items ei ON s.item_id = ei.item_id
                JOIN certification_areas ca ON ei.area_id = ca.area_id AND ca.year_id = %s
                WHERE s.student_id = %s AND s.is_deleted = FALSE
                """,
                (year_id, student_id),
            )
            all_submissions = await cur.fetchall()

            merit_start = date(target_year, 3, 1)
            merit_end = date(target_year + 1, 3, 1)
            await cur.execute(
                """
                SELECT merits_point_id, type, points, related_area, occurred_at
                FROM merits
                WHERE student_id = %s
                  AND is_reflected = TRUE
                  AND is_deleted = FALSE
                  AND occurred_at >= %s
                  AND occurred_at < %s
                """,
                (student_id, merit_start, merit_end),
            )
            all_merits = await cur.fetchall()

        if areas:
            calc_res = calculate_student_certification(areas, all_submissions, all_merits)
            total_merit_points = 0.0
            for m in all_merits:
                m_type = m.get("type")
                pts = float(m.get("points") or 0.0)
                signed_pts = -abs(pts) if m_type in ("-", "벌점") else abs(pts)
                total_merit_points += signed_pts

            return {
                "totalScore": calc_res["totalScore"],
                "certStatus": calc_res["certStatus"],
                "pointTotal": round_decimal(total_merit_points, 1),
                "areas": calc_res["areas"],
                "pendingCount": calc_res["pendingCount"],
            }
    except Exception as e:
        logger.warning(f"Failed to calculate student_totals for student {student_id}: {e}")
    return None


@router.patch(
    "/{submission_id}/approve",
    summary="증빙자료 심사 승인 API (API-01)",
    response_model=SrFormat,
)
async def approve_submission(
    submission_id: int,
    req: ApproveSubmissionRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    증빙자료 심사 승인 엔드포인트 (API-01)
    - 교사(담임/교과) 및 관리자 전용
    - 상태 전이 원자적 검증(status_code IN (1, 2) -> 3)
    """
    from core.authorization import authorize_submission_access
    auth_err = await authorize_submission_access(current_user, submission_id, conn, action="approve")
    if auth_err:
        return auth_err

    user_uuid = current_user.get("uuid") or current_user.get("sub")

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT s.submission_id, s.student_id, s.status_code, s.granted_score, 
                   ei.max_score, ca.year_id, ay.year
            FROM submissions s
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id
            JOIN academic_years ay ON ca.year_id = ay.year_id
            WHERE s.submission_id = %s AND s.is_deleted = FALSE
            LIMIT 1
            """,
            (submission_id,),
        )
        row = await cur.fetchone()

    if not row:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="ITEM_NOT_FOUND", message="증빙자료를 찾을 수 없습니다."),
            ).model_dump(),
        )

    if row["status_code"] not in (1, 2):
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="INVALID_STATUS", message="제출완료(1) 또는 검토중(2) 상태의 증빙만 승인할 수 있습니다."),
            ).model_dump(),
        )

    max_score = float(row["max_score"]) if row.get("max_score") is not None else float(row.get("item_max_score", 100.0))
    score = req.score if req.score is not None else (req.granted_score if req.granted_score is not None else req.grantedScore)
    if score is None:
        score = max_score

    if score < 0 or score > max_score:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message=f"인정 점수는 0 이상 {max_score} 이하이어야 합니다."),
            ).model_dump(),
        )

    raw_comment = req.comment if req.comment is not None else (req.teacher_comment if req.teacher_comment is not None else req.teacherComment)
    import html
    comment = html.escape(raw_comment.strip()) if raw_comment else None

    reviewer_id = None
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT teachers_id FROM teachers WHERE uuid = %s AND is_deleted = FALSE LIMIT 1", (user_uuid,))
        t_row = await cur.fetchone()
        if t_row:
            reviewer_id = t_row["teachers_id"]

    try:
        await conn.autocommit(False)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE submissions
                SET status_code = 3,
                    granted_score = %s,
                    reviewer_id = %s,
                    reviewed_at = NOW(),
                    teacher_comment = %s,
                    updated_at = NOW()
                WHERE submission_id = %s AND status_code IN (1, 2)
                """,
                (score, reviewer_id, comment, submission_id),
            )
            if cur.rowcount == 0:
                await conn.rollback()
                return JSONResponse(
                    status_code=409,
                    content=SrFormat(
                        status_code=409,
                        success=False,
                        data=None,
                        error=Error(code="CONFLICT", message="다른 사용자에 의해 이미 심사 처리되었거나 상태가 변경되었습니다."),
                    ).model_dump(),
                )

            await cur.execute(
                """
                INSERT INTO submissions_logs (
                    submission_id, modifier_uuid, action_type,
                    old_status_code, new_status_code, old_score, new_score, comment, created_at
                )
                VALUES (%s, %s, 'APPROVE', %s, 3, %s, %s, %s, NOW())
                """,
                (submission_id, user_uuid, row["status_code"], row.get("granted_score"), score, comment),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    # 학생 인증 현황 총점 자동 갱신 및 계산 (Tech_spec.md 3.4 & TODO.md 3.4)
    student_id = row["student_id"]
    year_id = row.get("year_id")
    target_year = row.get("year", 2026)
    student_totals = await _calculate_student_totals(student_id, year_id, target_year, conn)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "id": submission_id,
            "submissionId": submission_id,
            "status": "인정완료",
            "statusCode": 3,
            "score": score,
            "grantedScore": score,
            "teacherComment": comment,
            "reviewerId": reviewer_id,
            "studentTotals": student_totals,
        },
    ).model_dump()


@router.patch(
    "/{submission_id}/reject",
    summary="증빙자료 심사 반려/재제출요청 API (API-01)",
    response_model=SrFormat,
)
async def reject_submission(
    submission_id: int,
    req: RejectSubmissionRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    증빙자료 심사 반려 및 재제출요청 엔드포인트 (API-01)
    - 교사 및 관리자 전용
    - 대상 상태: 4(반려) 또는 5(재제출요청)
    """
    from core.authorization import authorize_submission_access
    auth_err = await authorize_submission_access(current_user, submission_id, conn, action="reject")
    if auth_err:
        return auth_err

    user_uuid = current_user.get("uuid") or current_user.get("sub")
    target_status = req.status_code if req.status_code is not None else (req.statusCode or 4)
    if target_status not in (4, 5):
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="INVALID_STATUS", message="반려 대상 상태 코드는 4(반려) 또는 5(재제출요청)여야 합니다."),
            ).model_dump(),
        )

    # 1. 반려 사유 필수 입력 검증 (Tech_spec.md 3.5 & TODO.md 3.5)
    raw_comment = (
        req.comment
        if req.comment is not None
        else (
            req.reason
            if req.reason is not None
            else (
                req.teacher_comment
                if req.teacher_comment is not None
                else req.teacherComment
            )
        )
    )
    if not raw_comment or not raw_comment.strip():
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="반려 사유(comment)는 필수 입력 항목입니다."),
            ).model_dump(),
        )

    # 2. XSS 방어 HTML 이스케이프
    import html
    comment = html.escape(raw_comment.strip())

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT s.submission_id, s.student_id, s.status_code, s.granted_score,
                   ca.year_id, ay.year
            FROM submissions s
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id
            JOIN academic_years ay ON ca.year_id = ay.year_id
            WHERE s.submission_id = %s AND s.is_deleted = FALSE
            LIMIT 1
            """,
            (submission_id,),
        )
        row = await cur.fetchone()

    if not row:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="ITEM_NOT_FOUND", message="증빙자료를 찾을 수 없습니다."),
            ).model_dump(),
        )

    if row["status_code"] not in (1, 2):
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="INVALID_STATUS", message="제출완료(1) 또는 검토중(2) 상태의 증빙만 반려할 수 있습니다."),
            ).model_dump(),
        )

    reviewer_id = None
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT teachers_id FROM teachers WHERE uuid = %s AND is_deleted = FALSE LIMIT 1", (user_uuid,))
        t_row = await cur.fetchone()
        if t_row:
            reviewer_id = t_row["teachers_id"]

    act_type = "REJECT" if target_status == 4 else "REQUEST_RESUBMIT"

    try:
        await conn.autocommit(False)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE submissions
                SET status_code = %s,
                    granted_score = NULL,
                    reviewer_id = %s,
                    reviewed_at = NOW(),
                    teacher_comment = %s,
                    updated_at = NOW()
                WHERE submission_id = %s AND status_code IN (1, 2)
                """,
                (target_status, reviewer_id, comment, submission_id),
            )
            if cur.rowcount == 0:
                await conn.rollback()
                return JSONResponse(
                    status_code=409,
                    content=SrFormat(
                        status_code=409,
                        success=False,
                        data=None,
                        error=Error(code="CONFLICT", message="다른 사용자에 의해 이미 심사 처리되었거나 상태가 변경되었습니다."),
                    ).model_dump(),
                )

            await cur.execute(
                """
                INSERT INTO submissions_logs (
                    submission_id, modifier_uuid, action_type,
                    old_status_code, new_status_code, old_score, new_score, comment, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, NOW())
                """,
                (submission_id, user_uuid, act_type, row["status_code"], target_status, row.get("granted_score"), comment),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    # 3. 반려 후 학생 인증 현황 총점 자동 재계산 (Tech_spec.md & TODO.md 3.5)
    student_id = row["student_id"]
    year_id = row.get("year_id")
    target_year = row.get("year", 2026)
    student_totals = await _calculate_student_totals(student_id, year_id, target_year, conn)

    status_str = "반려" if target_status == 4 else "재제출요청"
    return SrFormat(
        status_code=200,
        success=True,
        data={
            "id": submission_id,
            "submissionId": submission_id,
            "status": status_str,
            "statusCode": target_status,
            "comment": comment,
            "teacherComment": comment,
            "reviewedAt": datetime.now().isoformat(),
            "reviewerId": reviewer_id,
            "studentTotals": student_totals,
        },
    ).model_dump()


@router.patch(
    "/{submission_id}/score",
    summary="증빙자료 인정 점수 수정 API (API-01)",
    response_model=SrFormat,
)
async def modify_submission_score(
    submission_id: int,
    req: ModifyScoreRequest,
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    증빙자료 인정 점수 사후 수정 엔드포인트 (API-01)
    - 교사 및 관리자 전용
    - 승인 완료(3) 상태 건에 대해서만 허용
    """
    from core.authorization import authorize_submission_access
    auth_err = await authorize_submission_access(current_user, submission_id, conn, action="score_modify")
    if auth_err:
        return auth_err

    user_uuid = current_user.get("uuid") or current_user.get("sub")

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT s.submission_id, s.student_id, s.status_code, s.granted_score, 
                   ei.max_score, ca.year_id, ay.year
            FROM submissions s
            JOIN evaluation_items ei ON s.item_id = ei.item_id
            JOIN certification_areas ca ON ei.area_id = ca.area_id
            JOIN academic_years ay ON ca.year_id = ay.year_id
            WHERE s.submission_id = %s AND s.is_deleted = FALSE
            LIMIT 1
            """,
            (submission_id,),
        )
        row = await cur.fetchone()

    if not row:
        return JSONResponse(
            status_code=404,
            content=SrFormat(
                status_code=404,
                success=False,
                data=None,
                error=Error(code="ITEM_NOT_FOUND", message="증빙자료를 찾을 수 없습니다."),
            ).model_dump(),
        )

    if row["status_code"] != 3:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="INVALID_STATUS", message="승인 완료(3) 상태의 증빙에 대해서만 점수를 수정할 수 있습니다."),
            ).model_dump(),
        )

    # 1. 점수 입력값 확인
    score = (
        req.score
        if req.score is not None
        else (req.granted_score if req.granted_score is not None else req.grantedScore)
    )
    if score is None:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="수정할 인정 점수(score)가 필요합니다."),
            ).model_dump(),
        )

    max_score = float(row["max_score"]) if row.get("max_score") is not None else float(row.get("item_max_score", 100.0))
    if score < 0 or score > max_score:
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message=f"인정 점수는 0 이상 {max_score} 이하이어야 합니다."),
            ).model_dump(),
        )

    # 2. 수정 사유(reason) 필수 입력 검증 (Tech_spec.md 3.4 & TODO.md 3.6)
    raw_reason = (
        req.reason
        if req.reason is not None
        else (
            req.comment
            if req.comment is not None
            else (
                req.teacher_comment
                if req.teacher_comment is not None
                else req.teacherComment
            )
        )
    )
    if not raw_reason or not raw_reason.strip():
        return JSONResponse(
            status_code=400,
            content=SrFormat(
                status_code=400,
                success=False,
                data=None,
                error=Error(code="VALIDATION_ERROR", message="점수 수정 사유(reason)는 필수 입력 항목입니다."),
            ).model_dump(),
        )

    # 3. HTML XSS 방어 이스케이프
    import html
    comment = html.escape(raw_reason.strip())

    reviewer_id = None
    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute("SELECT teachers_id FROM teachers WHERE uuid = %s AND is_deleted = FALSE LIMIT 1", (user_uuid,))
        t_row = await cur.fetchone()
        if t_row:
            reviewer_id = t_row["teachers_id"]

    try:
        await conn.autocommit(False)
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE submissions
                SET granted_score = %s,
                    teacher_comment = %s,
                    reviewer_id = COALESCE(%s, reviewer_id),
                    reviewed_at = NOW(),
                    updated_at = NOW()
                WHERE submission_id = %s AND status_code = 3
                """,
                (score, comment, reviewer_id, submission_id),
            )
            if cur.rowcount == 0:
                await conn.rollback()
                return JSONResponse(
                    status_code=409,
                    content=SrFormat(
                        status_code=409,
                        success=False,
                        data=None,
                        error=Error(code="CONFLICT", message="점수 수정 중 동시성 충돌이 발생했습니다."),
                    ).model_dump(),
                )

            await cur.execute(
                """
                INSERT INTO submissions_logs (
                    submission_id, modifier_uuid, action_type,
                    old_status_code, new_status_code, old_score, new_score, comment, created_at
                )
                VALUES (%s, %s, 'SCORE_MODIFY', 3, 3, %s, %s, %s, NOW())
                """,
                (submission_id, user_uuid, row.get("granted_score"), score, comment),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.autocommit(True)

    # 4. 수정 후 학생 인증 현황 총점 자동 재계산 (Tech_spec.md & TODO.md 3.6)
    student_id = row["student_id"]
    year_id = row.get("year_id")
    target_year = row.get("year", 2026)
    student_totals = await _calculate_student_totals(student_id, year_id, target_year, conn)

    return SrFormat(
        status_code=200,
        success=True,
        data={
            "id": submission_id,
            "submissionId": submission_id,
            "status": "인정완료",
            "statusCode": 3,
            "score": score,
            "grantedScore": score,
            "reason": comment,
            "teacherComment": comment,
            "reviewedAt": datetime.now().isoformat(),
            "reviewerId": reviewer_id,
            "studentTotals": student_totals,
        },
    ).model_dump()

