"""
core/neis.py - NEIS(나이스) 교육정보 개방 포털 연동 및 학급 검증 모듈

설정 정보:
- 학교명: 부산소프트웨어마이스터고등학교 (BSSM)
- 관할 교육청: 부산광역시교육청 (C10)
- 표준학교코드: 7150658
- 공식 Open API: https://open.neis.go.kr/hub

개인정보보호법 및 NEIS 개방 정책에 따른 학생 명단 API 제공 불가 사유:
- 개인정보보호법 제18조 및 교육정보 개방 지침에 따라 개별 학생의 성명, 학번, 생년월일 등
  고유 식별 정보는 공공 Open API로 제공되지 않습니다.
- 따라서 본 모듈은 NEIS Open API를 활용하여 학교 기본정보 및 학년도별 정규 개설 학급 정보를
  조회하고, 업로드된 학생 명단(XLSX/CSV)의 학년/반이 실제 존재하는지 교차 검증하는 역할을 수행합니다.
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple
import httpx

logger = logging.getLogger("meister.neis")

# 부산소프트웨어마이스터고등학교 공식 NEIS 메타데이터
NEIS_OFFICE_CODE = "C10"  # 부산광역시교육청
NEIS_OFFICE_NAME = "부산광역시교육청"
NEIS_SCHOOL_CODE = "7150658"  # 부산소프트웨어마이스터고등학교 표준학교코드
NEIS_SCHOOL_NAME = "부산소프트웨어마이스터고등학교"

NEIS_BASE_URL = "https://open.neis.go.kr/hub"

# 학급 캐시 (메모리 캐싱: year -> Set[(grade, class_no)])
_NEIS_CLASS_CACHE: Dict[int, Set[Tuple[int, int]]] = {}


def explain_neis_student_api_limitation() -> Dict[str, Any]:
    """
    NEIS Open API를 통한 학생 명단 직접 조회/일괄 등록이 불가한 법적·기술적 근거를 반환합니다.
    """
    return {
        "supported": False,
        "schoolName": NEIS_SCHOOL_NAME,
        "officeName": NEIS_OFFICE_NAME,
        "officeCode": NEIS_OFFICE_CODE,
        "schoolCode": NEIS_SCHOOL_CODE,
        "legalAndTechnicalBasis": [
            {
                "category": "개인정보보호법 및 교육기본법 준수의무",
                "detail": (
                    "개인정보보호법 제18조(개인정보의 목적 외 이용·제공 제한)에 따라, 학생의 성명, "
                    "학번, 연락처, 생년월일 등 개인식별정보는 정보주체의 동의 없이 공공 Open API로 "
                    "외부에 개방되거나 전송될 수 없습니다."
                ),
            },
            {
                "category": "나이스(NEIS) 교육정보 개방 포털 정책적 한계",
                "detail": (
                    "open.neis.go.kr에서 제공하는 50여 종의 Open API는 학교기본정보(schoolInfo), "
                    "학급정보(classInfo), 학교일정(schoolSchedule), 급식식단(mealServiceDietInfo) 등 "
                    "공공 통계 및 학사 일반 정보로 엄격히 한정되어 있으며, 학생 개인 정보를 반환하는 API는 미존재합니다."
                ),
            },
            {
                "category": "시스템 폐쇄망 구조 및 인증 체계",
                "detail": (
                    "개별 학생의 학적 및 명단 데이터는 교육행정정보시스템(NEIS 인트라넷) 내부망에서 "
                    "교원 전자서명(GPKI) 인증을 거친 인가 사용자만 열람 및 엑셀 다운로드가 가능합니다."
                ),
            },
        ],
        "recommendedSolution": (
            "담당 교원/관리자가 NEIS 내부망(학적 > 학급별 명단 엑셀 다운로드)에서 추출한 "
            "표준 엑셀/CSV 파일을 본 시스템의 4.6 일괄 등록 API(POST /api/admin/students/batch)에 "
            "업로드하여 등록합니다. 업로드 시 본 시스템은 NEIS classInfo API와 실시간 교차 검증을 수행합니다."
        ),
    }


async def get_school_info(
    api_key: Optional[str] = None,
    timeout: float = 3.0,
) -> Optional[Dict[str, Any]]:
    """
    NEIS Open API를 통해 부산소프트웨어마이스터고등학교 기본 정보를 조회합니다.
    """
    params: Dict[str, Any] = {
        "Type": "json",
        "ATPT_OFCDC_SC_CODE": NEIS_OFFICE_CODE,
        "SD_SCHUL_CODE": NEIS_SCHOOL_CODE,
    }
    if api_key:
        params["KEY"] = api_key

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{NEIS_BASE_URL}/schoolInfo", params=params)
            if resp.status_code != 200:
                logger.warning(f"NEIS schoolInfo HTTP error: {resp.status_code}")
                return None
            data = resp.json()
            if "schoolInfo" in data and len(data["schoolInfo"]) > 1:
                rows = data["schoolInfo"][1].get("row", [])
                if rows:
                    return rows[0]
    except Exception as e:
        logger.warning(f"NEIS schoolInfo request failed: {e}")
    return None


async def get_classes_for_year(
    year: int,
    api_key: Optional[str] = None,
    timeout: float = 3.0,
) -> List[Dict[str, Any]]:
    """
    NEIS Open API를 통해 특정 학년도 부산소프트웨어마이스터고등학교 개설 학급 목록을 조회합니다.
    """
    params: Dict[str, Any] = {
        "Type": "json",
        "pSize": 100,
        "ATPT_OFCDC_SC_CODE": NEIS_OFFICE_CODE,
        "SD_SCHUL_CODE": NEIS_SCHOOL_CODE,
        "AY": str(year),
    }
    if api_key:
        params["KEY"] = api_key

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{NEIS_BASE_URL}/classInfo", params=params)
            if resp.status_code != 200:
                logger.warning(f"NEIS classInfo HTTP error: {resp.status_code}")
                return []
            data = resp.json()
            if "classInfo" in data and len(data["classInfo"]) > 1:
                rows = data["classInfo"][1].get("row", [])
                # 캐시 갱신
                class_set: Set[Tuple[int, int]] = set()
                for r in rows:
                    try:
                        g = int(r.get("GRADE", 0))
                        c = int(r.get("CLASS_NM", 0))
                        if g > 0 and c > 0:
                            class_set.add((g, c))
                    except (ValueError, TypeError):
                        continue
                if class_set:
                    _NEIS_CLASS_CACHE[year] = class_set
                return rows
    except Exception as e:
        logger.warning(f"NEIS classInfo request failed: {e}")

    return []


async def validate_class_against_neis(
    year: int,
    grade: int,
    class_no: int,
    api_key: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    해당 학년/반이 NEIS에 정규 편성된 학급인지 검증합니다.
    - NEIS 호출 실패 시 서비스 중단을 방지하기 위해 유효 범위(1~3학년, 1~4반) 기본 룰로 폴백합니다.
    """
    # 1. 캐시 확인
    if year in _NEIS_CLASS_CACHE:
        is_valid = (grade, class_no) in _NEIS_CLASS_CACHE[year]
        if not is_valid:
            return False, f"NEIS 학급 정보상 {year}학년도 {grade}학년 {class_no}반은 존재하지 않습니다."
        return True, None

    # 2. NEIS API 조회
    classes = await get_classes_for_year(year, api_key=api_key)
    if classes and year in _NEIS_CLASS_CACHE:
        is_valid = (grade, class_no) in _NEIS_CLASS_CACHE[year]
        if not is_valid:
            return False, f"NEIS 학급 정보상 {year}학년도 {grade}학년 {class_no}반은 존재하지 않습니다."
        return True, None

    # 3. 폴백 검증 (부산소프트웨어마이스터고 고교 기본 학제: 1~3학년, 학년당 1~4반)
    if 1 <= grade <= 3 and 1 <= class_no <= 4:
        return True, None
    return False, f"부산소프트웨어마이스터고 학제 기준(1~3학년 1~4반)을 벗어났습니다 ({grade}학년 {class_no}반)."
