"""
core/academic.py - 학사력 및 학년도 캐시 공통 모듈

명세 및 목적:
- 3월 1일 기준 학사력 연도 계산 (get_current_academic_year)
- academic_years.year_id 인메모리 프로세스 캐시 (_YEAR_ID_CACHE) 단일화
- 관리자 학년도 변경 시 전체 프로세스 캐시 무효화 (clear_year_cache)
"""

from datetime import datetime
from typing import Any, Dict, Optional

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

_YEAR_ID_CACHE: Dict[int, int] = {}


def clear_year_cache() -> None:
    """학년도 캐시 무효화 (관리자 학년도 변경 시 호출)"""
    global _YEAR_ID_CACHE
    _YEAR_ID_CACHE.clear()


async def get_current_academic_year(target_date: Optional[datetime] = None) -> int:
    """
    3월 1일 학사력 기준 현재 학년도 계산 함수
    
    규칙:
    - 1월과 2월은 직전 학년도에 귀속 (예: 2026년 2월 -> 2025 학년도)
    - 3월~12월은 해당 연도 학년도 (예: 2026년 3월 -> 2026 학년도)
    """
    now = target_date or datetime.now()
    if now.month < 3:
        return now.year - 1
    return now.year


# 별칭 지원
get_current_year = get_current_academic_year


async def get_year_id_by_year(conn: Any, year: Any) -> Optional[int]:
    """
    학년도 정수(예: 2026)를 기반으로 academic_years 테이블의 year_id를 조회합니다.
    _YEAR_ID_CACHE를 먼저 조회하고 미스 시 DB 질의 후 캐시에 적재합니다.
    """
    global _YEAR_ID_CACHE
    try:
        y_int = int(year)
    except (TypeError, ValueError):
        return None

    if y_int in _YEAR_ID_CACHE:
        return _YEAR_ID_CACHE[y_int]

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            "SELECT year_id FROM academic_years WHERE year = %s LIMIT 1",
            (y_int,)
        )
        row = await cur.fetchone()
        if row:
            _YEAR_ID_CACHE[y_int] = row["year_id"]
            return row["year_id"]
    return None


# 별칭 지원
get_year_id = get_year_id_by_year
