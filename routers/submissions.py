"""
routers/submissions.py - 마이스터 역량인증제 증빙자료 제출 및 심사 API 라우터

명세 및 연동 기준:
- Tech_spec.md 2.2 (증빙자료 제출: POST /api/submissions)
- TODO.md 2.2 (증빙자료 신규 제출 API: POST /api/submissions)
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile

from core.security import get_current_user
from database import get_db, get_redis
from routers.students import handle_submit_evidence
from sr_format import SrFormat
import redis.asyncio as aioredis

router = APIRouter(
    prefix="/api/submissions",
    tags=["submissions"],
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

