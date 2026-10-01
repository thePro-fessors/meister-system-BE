"""
routers/submissions.py - 마이스터 역량인증제 증빙자료 제출 및 심사 API 라우터

명세 및 연동 기준:
- Tech_spec.md 2.2 (증빙자료 제출: POST /api/submissions)
- TODO.md 2.2 (증빙자료 신규 제출 API: POST /api/submissions)
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

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

