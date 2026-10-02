"""
routers/submissions.py - 마이스터 역량인증제 증빙자료 제출 및 심사 API 라우터

명세 및 연동 기준:
- Tech_spec.md 2.2 (증빙자료 제출: POST /api/submissions)
- TODO.md 2.2 (증빙자료 신규 제출 API: POST /api/submissions)
"""

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

from core.security import get_current_user
from database import get_db, get_redis
from routers.students import handle_submit_evidence, handle_get_submissions
from sr_format import SrFormat
import redis.asyncio as aioredis

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

        new_desc = (description or "").strip() or existing["description"]
        target_link = (link_url or link or "").strip() or existing["link_url"]
        old_file_path = existing["file_path"]

        new_file_path = old_file_path
        new_disk_path = None

        # 신규 파일 업로드 처리
        if file and file.filename:
            save_res = await save_upload_file(file, subfolder="submissions")
            new_file_path = save_res["file_path"]
            new_disk_path = save_res["disk_path"]

        try:
            await conn.autocommit(False)
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE submissions
                    SET description = %s,
                        file_path = %s,
                        link_url = %s,
                        status_code = 1,
                        updated_at = NOW()
                    WHERE submission_id = %s
                    """,
                    (new_desc, new_file_path, target_link, submission_id),
                )

                # submissions_logs 스키마 준수 이력 기록
                await cur.execute(
                    """
                    INSERT INTO submissions_logs (
                        submission_id, modifier_uuid, action_type,
                        old_status_code, new_status_code, old_score, new_score, comment, created_at
                    )
                    VALUES (%s, %s, '재제출', %s, 1, %s, NULL, %s, NOW())
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
                "statusCode": 1,
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
    (SECURITY_AND_AUDIT.md 1.2 & TODO.md 6.1 P1)
    """
    from fastapi.responses import JSONResponse
    from core.security import create_download_ticket
    from sr_format import Error

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

    # 학생인 경우 본인 증빙인지 검증 (IDOR 방어)
    if user_role in ("student", "0", 0) and row["uuid"] != user_uuid:
        return JSONResponse(
            status_code=403,
            content=SrFormat(
                status_code=403,
                success=False,
                data=None,
                error=Error(code="FORBIDDEN", message="본인의 증빙 파일에 대해서만 티켓을 발급받을 수 있습니다."),
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
    """
    from fastapi.responses import JSONResponse
    from core.storage import cleanup_old_file_on_resubmit
    from sr_format import Error

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

    if user_role in ("student", "0", 0) and row["uuid"] != user_uuid:
        return JSONResponse(
            status_code=403,
            content=SrFormat(
                status_code=403,
                success=False,
                data=None,
                error=Error(code="FORBIDDEN", message="본인이 제출한 증빙자료만 삭제할 수 있습니다."),
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

    # 증빙 삭제 시 디스크 고아 파일 즉시 정리 (SECURITY_AND_AUDIT.md 4.1 권고)
    if file_to_clean:
        cleanup_old_file_on_resubmit(file_to_clean)

    return SrFormat(
        status_code=200,
        success=True,
        data={"submissionId": submission_id, "deleted": True},
    ).model_dump()

