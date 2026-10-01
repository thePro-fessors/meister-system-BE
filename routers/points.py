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
