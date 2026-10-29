from core.security import error_response, normalize_role
"""
core/authorization.py - 중앙 집중식 학생 리소스 인가(Authorization) 및 권한 제어 모듈
(SEC-01: 모든 학생 리소스에 동일한 교사 권한 범위 적용)
"""

from typing import Any, Dict, Optional, Set, Tuple, Union
from fastapi.responses import JSONResponse
from asyncmy.cursors import DictCursor

from sr_format import Error, SrFormat


async def resolve_student_id(conn: Any, identifier: Any) -> Optional[int]:
    """
    정수 PK(student_id), 문자열 숫자("101"), 로그인 ID(users.id e.g. "student1"), UUID, 이메일 등
    다양한 형태의 식별자로부터 실제 students.student_id 정수 PK를 안전하게 매핑 및 반환합니다.
    """
    if identifier is None:
        return None

    s_val = str(identifier).strip()
    if not s_val:
        return None

    # 1. 정수형이거나 숫자 문자열인 경우 오직 students.student_id PK만 확인 (숫자형 로그인 ID와의 모호성 및 충돌 원천 차단)
    if isinstance(identifier, int) or s_val.isdigit():
        try:
            int_id = int(s_val)
            async with conn.cursor(cursor=DictCursor) as cur:
                await cur.execute(
                    "SELECT student_id FROM students WHERE student_id = %s AND is_deleted = FALSE LIMIT 1",
                    (int_id,),
                )
                row = await cur.fetchone()
                if row:
                    return row["student_id"] if isinstance(row, dict) else row[0]
        except Exception:
            pass
        return None

    # 2. 이메일 형식(@ 포함)인 경우 이메일로만 매핑
    if "@" in s_val:
        async with conn.cursor(cursor=DictCursor) as cur:
            try:
                await cur.execute(
                    "SELECT student_id FROM students WHERE email = %s AND is_deleted = FALSE LIMIT 1",
                    (s_val,),
                )
                row = await cur.fetchone()
                if row:
                    return row["student_id"] if isinstance(row, dict) else row[0]
            except Exception:
                pass
            try:
                await cur.execute(
                    """
                    SELECT s.student_id 
                    FROM students s 
                    JOIN users u ON s.uuid = u.uuid 
                    WHERE u.email = %s AND s.is_deleted = FALSE 
                    LIMIT 1
                    """,
                    (s_val,),
                )
                row = await cur.fetchone()
                if row:
                    return row["student_id"] if isinstance(row, dict) else row[0]
            except Exception:
                pass
        return None

    # 3. UUID 또는 로그인 ID(users.id) 매핑
    async with conn.cursor(cursor=DictCursor) as cur:
        # (1) users 테이블의 로그인 ID(id)로 학생 매핑
        try:
            await cur.execute(
                """
                SELECT s.student_id 
                FROM students s 
                JOIN users u ON s.uuid = u.uuid 
                WHERE u.id = %s AND s.is_deleted = FALSE 
                LIMIT 1
                """,
                (s_val,),
            )
            row = await cur.fetchone()
            if row:
                return row["student_id"] if isinstance(row, dict) else row[0]
        except Exception:
            pass

        # (2) students.uuid 직접 매핑
        try:
            await cur.execute(
                "SELECT student_id FROM students WHERE uuid = %s AND is_deleted = FALSE LIMIT 1",
                (s_val,),
            )
            row = await cur.fetchone()
            if row:
                return row["student_id"] if isinstance(row, dict) else row[0]
        except Exception:
            pass

    return None


async def get_student_academic_record(
    student_id: Union[int, str],
    conn: Any,
    year: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """학생의 특정 학년도(또는 활성/최신 학년도) 학적 정보를 조회합니다."""
    if not isinstance(student_id, int):
        resolved = await resolve_student_id(conn, student_id)
        if resolved is None:
            return None
        student_id = resolved

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
    student_id: Union[int, str],
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
    if not isinstance(student_id, int):
        resolved = await resolve_student_id(conn, student_id)
        if resolved is None:
            return error_response(404, "USER_NOT_FOUND", "학생 계정 정보를 찾을 수 없습니다.")
        student_id = resolved
    actor_role = actor.get("role")
    actor_uuid = actor.get("uuid") or actor.get("sub")

    # 1. 역할 정규화
    is_admin = normalize_role(actor_role) == "admin"
    is_teacher = normalize_role(actor_role) == "teacher"
    is_student = normalize_role(actor_role) == "student"

    if is_admin:
        return None

    # 2. 학생 역할 인가 검증
    if is_student:
        if action in ("review", "approve", "reject", "score_modify", "manage_points"):
            return error_response(403, "FORBIDDEN", "학생은 심사 또는 상벌점 관리 권한이 없습니다.")

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
                    return error_response(403, "FORBIDDEN", "본인의 데이터만 접근할 수 있습니다.")
                return error_response(404, "USER_NOT_FOUND", "학생 계정 정보를 찾을 수 없습니다.")
            if st_row["student_id"] != student_id:
                return error_response(403, "FORBIDDEN", "본인의 데이터만 접근할 수 있습니다.")
        return None

    # 3. 교사 역할 인가 검증
    if is_teacher:
        if action in ("student_submit", "student_resubmit", "student_delete"):
            return error_response(403, "FORBIDDEN", "교사는 학생 본인 제출/삭제 작업을 직접 수행할 수 없습니다.")

        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute(
                "SELECT teachers_id, grade, class, position, department, subject FROM teachers WHERE uuid = %s AND is_deleted = FALSE LIMIT 1",
                (actor_uuid,),
            )
            t_row = await cur.fetchone()
            if not t_row:
                return error_response(404, "TEACHER_NOT_FOUND", "교사 정보를 찾을 수 없습니다.")

        t_grade = t_row.get("grade")
        t_class = t_row.get("class")
        t_position = (t_row.get("position") or "").strip()
        t_department = (t_row.get("department") or "").strip()
        t_subject = (t_row.get("subject") or "").strip()

        # [정책 1] 모든 교사에게 전교생 상벌점 부여 및 조회 허용
        if action in ("manage_points", "read_points"):
            return None

        # [정책 2] 총괄 부서장: 전교생 접근 허용
        if t_position == "부서부장" and t_department in ("마이스터부", "교무기획부"):
            return None

        # [정책 3] 학년부장: 담당 학년(grade) 전체 학생 접근 및 관리 허용
        if t_position == "학년부장" and t_grade is not None:
            academic_record = await get_student_academic_record(student_id, conn, year)
            if not academic_record or academic_record.get("grade") != t_grade:
                return error_response(403, "FORBIDDEN", f"{t_grade}학년 학생의 데이터만 관리할 수 있습니다.")
            return None

        # [정책 4] 담임교사(정담임, 부담임, 임시담임): 담당 학급 학생 접근 허용
        if t_grade is not None and t_class is not None:
            # 교과/부서가 배정된 교사인 경우 담당 과제 조회를 위해 타 학급 열람 허용 (의심-02, 의심-03)
            if action == "read" and (t_subject or t_department or t_position == "부서부장"):
                pass
            else:
                academic_record = await get_student_academic_record(student_id, conn, year)
                if not academic_record:
                    return error_response(404, "USER_NOT_FOUND", "해당 학년도의 학생 학적 정보를 찾을 수 없습니다.")

                st_grade = academic_record.get("grade")
                st_class = academic_record.get("class_no")
                if st_grade != t_grade or st_class != t_class:
                    return error_response(403, "FORBIDDEN", "담당 학급 학생의 데이터만 접근할 수 있습니다.")

        return None

    # 4. 알 수 없는 역할 차단
    return error_response(403, "FORBIDDEN", "유효하지 않은 권한 역할입니다.")


async def authorize_point_mutation(
    actor: Dict[str, Any],
    point_row: Dict[str, Any],
    conn: Any,
    action: str = "modify",
) -> Optional[JSONResponse]:
    """
    상벌점 수정(PATCH) 및 삭제(DELETE) 권한 검증 (의심-04 결정 반영):
    - [본인이 부여한 건 + 담임교사 + 학년부장 + 학생생활안전부 + 총괄부서장 + 관리자]만 허용.
    """
    actor_role = actor.get("role")
    actor_uuid = actor.get("uuid") or actor.get("sub")
    is_admin = normalize_role(actor_role) == "admin"
    if is_admin:
        return None

    is_teacher = normalize_role(actor_role) == "teacher"
    if not is_teacher:
        return error_response(403, "FORBIDDEN", "상벌점 수정/삭제 권한이 없습니다.")

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            "SELECT teachers_id, grade, class, position, department FROM teachers WHERE uuid = %s AND is_deleted = FALSE LIMIT 1",
            (actor_uuid,),
        )
        t_row = await cur.fetchone()
        if not t_row or "teachers_id" not in t_row:
            return error_response(403, "FORBIDDEN", "교원 정보가 존재하지 않거나 상벌점 관리 권한이 없습니다.")

    t_id = t_row.get("teachers_id")
    t_grade = t_row.get("grade")
    t_class = t_row.get("class")
    t_position = (t_row.get("position") or "").strip()
    t_department = (t_row.get("department") or "").strip()

    # 1. 본인이 부여한 건 허용
    if point_row.get("teachers_id") and point_row.get("teachers_id") == t_id:
        return None

    # 2. 총괄 부서장(마이스터부, 교무기획부) 허용
    if t_position == "부서부장" and t_department in ("마이스터부", "교무기획부"):
        return None

    # 3. 학생생활안전부 소속 교사 허용
    if t_department == "학생생활안전부":
        return None

    # 4. 대상 학생의 학적 확인 (담임 / 학년부장 검증용)
    student_id = point_row.get("student_id")
    if student_id:
        academic_record = await get_student_academic_record(student_id, conn)
        if academic_record:
            st_grade = academic_record.get("grade")
            st_class = academic_record.get("class_no")
            # 4-1. 학년부장 (해당 학년)
            if t_position == "학년부장" and t_grade is not None and t_grade == st_grade:
                return None
            # 4-2. 담임교사 (해당 학급)
            if t_grade is not None and t_class is not None and t_grade == st_grade and t_class == st_class:
                return None

    act_name = "삭제" if action == "delete" else "수정"
    return error_response(403, "FORBIDDEN", f"상벌점 {act_name} 권한이 없습니다. (본인이 부여한 건, 담임교사, 학년부장, 학생생활안전부 교사, 관리자만 {act_name} 가능)")


def _expand_tokens(dept: Optional[str], subj: Optional[str]) -> Set[str]:
    tokens = set()
    for raw in (dept, subj):
        if not raw:
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        tokens.add(cleaned)
        # 부서 접미사 처리 (예: 외국어교육부 -> 외국어교육, 외국어)
        if cleaned.endswith("부") and len(cleaned) > 2:
            no_bu = cleaned[:-1]
            tokens.add(no_bu)
            if no_bu.endswith("교육") and len(no_bu) > 3:
                tokens.add(no_bu[:-2])
        # 동의어/연계 카테고리 매핑 (외국어 <-> 영어, 마이스터 <-> 전공 등)
        if any(w in cleaned for w in ("외국어", "영어")):
            tokens.add("외국어")
            tokens.add("영어")
        if any(w in cleaned for w in ("마이스터", "전공")):
            tokens.add("마이스터")
            tokens.add("전공")
    return tokens


def evaluate_evaluator_access(
    teacher_id: int,
    teacher_dept: Optional[str],
    teacher_subj: Optional[str],
    assigned_teacher_ids: Set[int],
    target_subject: Optional[str],
    target_department: Optional[str],
    item_name: Optional[str] = None,
    area_name: Optional[str] = None,
    is_legacy_unrestricted: bool = False,
) -> Tuple[bool, str]:
    """
    평가자 우선순위 판정:
    1순위: 교사 개인 지정(전담 N명) -> 목록이 존재할 경우, 오직 해당 교사들만 심사 가능 (일치 시 PASS, 미포함 시 403 FAIL)
    2순위: 교과 지정 -> 1순위 전담 교사 목록이 없고 교과가 지정된 경우, 해당 교과 담당 교사만 심사 가능 (일치 시 PASS, 불일치 시 403 FAIL)
    3순위: 부서 지정 -> 1, 2순위가 없고 부서만 지정된 경우, 해당 소속 부서 교사만 심사 가능 (일치 시 PASS, 불일치 시 403 FAIL)
    기본: 모두 없으면 기본 권한(담임/학년부장)으로 폴백 (비담임/타학급 교사는 심사 불가)
    """
    # [1순위: 전담 교사 개인 N명 지정]
    if assigned_teacher_ids:
        if teacher_id in assigned_teacher_ids:
            return True, "1순위: 전담 교사 일치"
        return False, "1순위: 지정된 전담 교사만 심사할 수 있습니다."

    # [2순위: 교과 지정]
    if target_subject and target_subject.strip():
        t_tokens = _expand_tokens(None, teacher_subj)
        s_tokens = _expand_tokens(None, target_subject.strip())
        t_raw = (teacher_subj or "").strip()
        s_raw = target_subject.strip()
        if (t_tokens & s_tokens) or (t_raw and (t_raw in s_raw or s_raw in t_raw)) or any(tok in s_raw or s_raw in tok for tok in t_tokens if len(tok) >= 2):
            return True, "2순위: 담당 교과 일치"
        return False, f"2순위: {s_raw} 담당 교사만 심사할 수 있습니다."

    # [3순위: 부서 지정]
    if target_department and target_department.strip():
        t_tokens = _expand_tokens(teacher_dept, None)
        d_tokens = _expand_tokens(target_department.strip(), None)
        t_raw_dept = (teacher_dept or "").strip()
        d_raw = target_department.strip()
        if (t_tokens & d_tokens) or (t_raw_dept and (t_raw_dept in d_raw or d_raw in t_raw_dept)) or any(tok in d_raw or d_raw in tok for tok in t_tokens if len(tok) >= 2):
            return True, "3순위: 소속 부서 일치"
        return False, f"3순위: {d_raw} 소속 교사만 심사할 수 있습니다."

    # 1~3순위 명시 설정이 없는 경우: 영역명/항목명 텍스트 기반 매칭(레거시 호환)
    if (teacher_dept or teacher_subj) and _is_department_or_subject_matched(teacher_dept, teacher_subj, None, item_name, area_name):
        return True, "담당 교과/부서 영역 일치"

    # 직책/부서/교과/담당학급이 모두 없는 레거시 전교생 대상 교과교사 폴백
    if is_legacy_unrestricted:
        return True, "기본: 전교생 교과교사 권한"

    return False, "기본: 별도 평가자 미지정 (기본 담임/학년부장 심사)"


def _is_department_or_subject_matched(
    teacher_dept: Optional[str],
    teacher_subj: Optional[str],
    item_dept: Optional[str],
    item_name: Optional[str],
    area_name: Optional[str],
) -> bool:
    """
    교사의 소속 부서(department) 또는 담당 교과(subject)가
    제출된 과제의 담당 부서(evaluation_items.department) 또는 항목/영역명(예: '영어', '외국어')과 일치하는지 검증합니다.
    """
    t_tokens = _expand_tokens(teacher_dept, teacher_subj)
    if not t_tokens:
        return False
    if item_dept and item_dept.strip():
        i_tokens = _expand_tokens(item_dept.strip(), None)
        if t_tokens & i_tokens:
            return True
        i_dept = item_dept.strip()
        return any(tok in i_dept or i_dept in tok for tok in t_tokens if len(tok) >= 2)
    target_text = f"{area_name or ''} {item_name or ''}"
    return any(tok in target_text for tok in t_tokens if len(tok) >= 2)


async def authorize_submission_access(
    actor: Dict[str, Any],
    submission_id: int,
    conn: Any,
    action: str = "read",
) -> Optional[JSONResponse]:
    """
    제출 ID(submission_id)에 대한 심사(approve/reject/score_modify) 및 열람 권한 검증:
    - 관리자 및 총괄 부서장(부서부장 + 마이스터부/교무기획부): 전교생 모든 과제 승인/반려/점수수정 허용
    - 학년부장: 담당 학년 전체 학생 모든 과제 승인/반려/점수수정 허용 (+ 본인 담당 교과/부서 매칭 시 타 학년도 허용)
    - 담임교사(정·부·임시): 담당 학급 학생 과제 전체 허용 (+ 본인 담당 부서/교과 과제면 타 학급도 허용)
    - 그 외 교사(교과전담/부서교사): 본인의 부서(department) 또는 담당교과(subject)에 매칭되는 과제만 승인/반려/점수수정 허용
    """
    actor_role = actor.get("role")
    actor_uuid = actor.get("uuid")

    async with conn.cursor(cursor=DictCursor) as cur:
        await cur.execute(
            """
            SELECT s.submission_id, s.student_id, st.uuid AS student_uuid,
                   COALESCE(ay.year, YEAR(CURDATE() - INTERVAL 2 MONTH)) AS year, s.is_deleted, s.status_code,
                   s.item_id,
                   ei.area_id,
                   ei.subject AS item_subject, ei.department AS item_department, ei.name AS item_name,
                   ca.subject AS area_subject, ca.department AS area_department, ca.name AS area_name
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
            return error_response(404, "ITEM_NOT_FOUND", "해당 증빙자료를 찾을 수 없습니다.")

        # 1순위: 전담 교사 N명 매핑 조회 (활성 교사만 필터링)
        item_id = sub_row.get("item_id")
        area_id = sub_row.get("area_id")
        item_teacher_ids: Set[int] = set()
        area_teacher_ids: Set[int] = set()

        if item_id:
            try:
                await cur.execute(
                    """
                    SELECT eit.teacher_id
                    FROM evaluation_item_teachers eit
                    JOIN teachers t ON eit.teacher_id = t.teachers_id
                    WHERE eit.item_id = %s AND t.is_deleted = FALSE
                    """,
                    (item_id,),
                )
                eit_rows = await cur.fetchall() or []
                item_teacher_ids = {
                    (r[0] if isinstance(r, (list, tuple)) else r.get("teacher_id"))
                    for r in eit_rows
                    if (r[0] if isinstance(r, (list, tuple)) else r.get("teacher_id")) is not None
                }
            except Exception:
                item_teacher_ids = set()

        if area_id:
            try:
                await cur.execute(
                    """
                    SELECT cat.teacher_id
                    FROM certification_area_teachers cat
                    JOIN teachers t ON cat.teacher_id = t.teachers_id
                    WHERE cat.area_id = %s AND t.is_deleted = FALSE
                    """,
                    (area_id,),
                )
                cat_rows = await cur.fetchall() or []
                area_teacher_ids = {
                    (r[0] if isinstance(r, (list, tuple)) else r.get("teacher_id"))
                    for r in cat_rows
                    if (r[0] if isinstance(r, (list, tuple)) else r.get("teacher_id")) is not None
                }
            except Exception:
                area_teacher_ids = set()

        item_subject = (sub_row.get("item_subject") or "").strip() or None
        item_department = (sub_row.get("item_department") or "").strip() or None
        area_subject = (sub_row.get("area_subject") or "").strip() or None
        area_department = (sub_row.get("area_department") or "").strip() or None

        # 계층적 상속: item_id에 1순위(교사), 2순위(교과), 3순위(부서)가 모두 없으면 상위 area_id의 설정 상속
        has_item_config = bool(item_teacher_ids) or bool(item_subject) or bool(item_department)

        if has_item_config:
            target_teacher_ids = item_teacher_ids
            target_subject = item_subject
            target_department = item_department
        else:
            target_teacher_ids = area_teacher_ids
            target_subject = area_subject
            target_department = area_department

    is_student = normalize_role(actor_role) == "student"
    if is_student:
        if action in ("approve", "reject", "score_modify"):
            return error_response(403, "FORBIDDEN", "학생은 심사 작업을 수행할 수 없습니다.")
        sub_student_uuid = sub_row.get("student_uuid") or sub_row.get("uuid")
        if sub_student_uuid != actor_uuid:
            return error_response(403, "FORBIDDEN", "본인의 증빙자료만 접근할 수 있습니다.")
        return None

    is_admin = normalize_role(actor_role) == "admin"
    if is_admin:
        if action in ("student_submit", "student_resubmit"):
            return error_response(403, "FORBIDDEN", "관리자는 학생 본인 제출 작업을 직접 수행할 수 없습니다.")
        # 관리자는 승인 완료건을 포함한 제출물 삭제(Soft Delete) 허용 (의심-10 결정 A)
        return None

    is_teacher = normalize_role(actor_role) == "teacher"
    if is_teacher:
        if action in ("student_submit", "student_resubmit", "student_delete"):
            return error_response(403, "FORBIDDEN", "교사는 학생 본인 제출/삭제 작업을 직접 수행할 수 없습니다. 잘못 승인한 경우 반려로 상태를 변경하십시오.")

        # [의심-02 결정 B] 모든 교사에게 전교생 증빙자료 및 다운로드 티켓 '열람(Read)' 허용
        if action == "read":
            return None

        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute(
                "SELECT teachers_id, name, subject, position, department, grade, class FROM teachers WHERE uuid = %s AND is_deleted = FALSE LIMIT 1",
                (actor_uuid,),
            )
            t_row = await cur.fetchone()
            if not t_row:
                return error_response(404, "TEACHER_NOT_FOUND", "교사 정보를 찾을 수 없습니다.")

        t_id = t_row.get("teachers_id")
        t_grade = t_row.get("grade")
        t_class = t_row.get("class")
        t_position = (t_row.get("position") or "").strip()
        t_department = (t_row.get("department") or "").strip()
        t_subject = (t_row.get("subject") or "").strip()
        sub_year = sub_row.get("year")
        target_student_id = sub_row["student_id"]
        is_legacy_unrestricted = (
            t_grade is None
            and t_class is None
            and not t_department
            and not t_subject
            and (not t_position or t_position != "교과전담")
        )
        sub_item_name = sub_row.get("item_name")
        sub_area_name = sub_row.get("area_name")

        # [교사 1] 총괄 부서장(부서부장 + 마이스터부/교무기획부): 전교생 모든 항목 심사 허용
        if t_position == "부서부장" and t_department in ("마이스터부", "교무기획부"):
            return None

        # [의심-01 결정 B] 1순위: 전담 교사 N명 지정 과제 (유형 A)
        # - 오직 해당 전담 교사들만 심사 가능 (담임교사, 학년부장이어도 심사 불가)
        if target_teacher_ids:
            if t_id in target_teacher_ids:
                return None
            return error_response(403, "FORBIDDEN", "1순위: 지정된 전담 교사만 심사할 수 있습니다.")

        # [의심-01 결정 B] 2순위: 담당 교과(subject) 지정 과제 (유형 B)
        # - 오직 해당 교과 담당 교사만 심사 가능 (타 교과 담임교사, 학년부장 심사 불가)
        if target_subject and target_subject.strip():
            t_tokens = _expand_tokens(None, t_subject)
            s_tokens = _expand_tokens(None, target_subject.strip())
            s_raw = target_subject.strip()
            if (t_tokens & s_tokens) or (t_subject and (t_subject in s_raw or s_raw in t_subject)) or any(tok in s_raw or s_raw in tok for tok in t_tokens if len(tok) >= 2):
                return None
            return error_response(403, "FORBIDDEN", f"2순위: {s_raw} 담당 교사만 심사할 수 있습니다.")

        # [의심-06 결정 B] 3순위: 담당 부서(department) 지정 과제 (유형 B)
        # - 해당 소속 부서 교사 또는 해당 부서 부서장(position='부서부장') 심사 가능
        if target_department and target_department.strip():
            d_raw = target_department.strip()
            t_tokens = _expand_tokens(t_department, None)
            d_tokens = _expand_tokens(d_raw, None)
            if (t_tokens & d_tokens) or (t_department and (t_department in d_raw or d_raw in t_department)) or any(tok in d_raw or d_raw in tok for tok in t_tokens if len(tok) >= 2):
                return None
            # 교무기획부/마이스터부 소속 학년부장이 담당 학년 과제 심사 시 허용
            if t_position == "학년부장" and t_department in ("교무기획부", "마이스터부") and t_grade is not None:
                academic_record = await get_student_academic_record(target_student_id, conn, sub_year)
                if academic_record and academic_record.get("grade") == t_grade:
                    return None
            msg = f"{t_grade}학년 학생의 과제 또는 지정된 심사 권한(전담/교과/부서)이 있는 과제만 심사할 수 있습니다." if (t_position == "학년부장" and t_grade is not None) else f"3순위: {d_raw} 소속 교사만 심사할 수 있습니다."
            return error_response(403, "FORBIDDEN", msg)

        # 교사의 소속 부서 또는 담당 교과가 항목/영역명(예: 외국어교육부 <-> 외국어능력)과 일치하는 경우 허용
        if (t_department or t_subject) and _is_department_or_subject_matched(t_department, t_subject, target_department, sub_item_name, sub_area_name):
            return None

        # [의심-07 결정 B] 1~3순위 명시 설정이 없는 일반 공통 과제 (유형 C)
        # - 오직 [해당 학생의 담임교사 + 학년부장 + 총괄부서장 + 관리자]만 심사 허용
        academic_record = await get_student_academic_record(target_student_id, conn, sub_year)
        st_grade = academic_record.get("grade") if academic_record else None
        st_class = academic_record.get("class_no") if academic_record else None

        # 담임교사 (우리 반)
        if t_grade is not None and t_class is not None and t_grade == st_grade and t_class == st_class:
            return None

        # 학년부장 (우리 학년)
        if t_position == "학년부장" and t_grade is not None and t_grade == st_grade:
            return None

        # 레거시 무제한 교과교사 폴백
        if is_legacy_unrestricted:
            return None

        if t_position == "학년부장" and t_grade is not None and st_grade is not None and t_grade != st_grade:
            return error_response(403, "FORBIDDEN", f"{t_grade}학년 담당 학년부장으로 타 학년({st_grade}학년) 학생의 증빙자료를 심사할 수 없습니다 (학년 권한 격리).")

        if t_grade is not None and t_class is not None:
            return error_response(403, "FORBIDDEN", f"담당 학급({t_grade}학년 {t_class}반) 외 타 학급 학생의 증빙자료를 심사할 수 없습니다 (담임 권한 격리).")

        return error_response(403, "FORBIDDEN", "전담교사 및 담당부서가 미지정된 일반 과제는 해당 학생의 담임교사 및 학년부장만 심사할 수 있습니다.")

    return error_response(403, "FORBIDDEN", "유효하지 않은 권한 역할입니다.")
