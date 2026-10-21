"""
routers/admin_batch.py - NEIS 학급 연동 및 학생/교사 명단 일괄 등록 API 서브라우터
"""

"""
routers/admin.py - 학년도 관리, 평가 기준 관리, 관리자 통계 개요 API 라우터 (API-03)
"""

import io
import json
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
import redis.asyncio as aioredis

try:
    from asyncmy.cursors import DictCursor
except (ImportError, ModuleNotFoundError):
    DictCursor = Any  # type: ignore

from core.academic import clear_year_cache, get_current_academic_year, get_current_year
from core.batch_manager import batch_task_manager
from core.calculator import calculate_student_certification
from core.excel_parser import (
    EMAIL_REGEX,
    parse_homeroom_field,
    parse_student_batch_file,
    parse_student_number_field,
    parse_teacher_batch_file,
    sanitize_text,
)
from core.neis import (
    explain_neis_student_api_limitation,
    explain_neis_teacher_api_limitation,
    get_classes_for_year,
    get_school_info,
    validate_class_against_neis,
    NEIS_OFFICE_CODE,
    NEIS_OFFICE_NAME,
    NEIS_SCHOOL_CODE,
    NEIS_SCHOOL_NAME,
)
from core.security import error_response, get_current_user, normalize_role
from database import get_db, get_redis
from sr_format import Error, SrFormat

# ------------------------------------------------------------------------------
# 라우터 정의
# ------------------------------------------------------------------------------
years_router = APIRouter(prefix="/api/years", tags=["years"])
criteria_router = APIRouter(prefix="/api/criteria", tags=["criteria"])
admin_router = APIRouter(tags=["admin"])


def check_admin_access(current_user: Dict[str, Any]) -> Optional[JSONResponse]:
    """관리자 권한 필수 검증 의존성 헬퍼"""
    if normalize_role(current_user.get("role")) != "admin":
        return error_response(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "관리자 권한이 필요합니다.",
        )
    return None

# ==============================================================================
# 4. 학생 명단 엑셀/CSV 일괄 등록 & NEIS 연동 API (4.6)
# ==============================================================================

@admin_router.get("/neis/info", summary="NEIS 연동 정책 및 학생 명단 API 미제공 법적 근거 안내 (4.6)", response_model=SrFormat)
async def get_neis_integration_info(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    NEIS Open API 연동 정보 및 학생 명단 직접 조회 API 미제공에 대한 구체적 법적/기술적 근거를 반환합니다.
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    return SrFormat(
        status_code=200,
        success=True,
        data=explain_neis_student_api_limitation(),
    ).model_dump()


@admin_router.get("/neis/school-info", summary="부산소프트웨어마이스터고 NEIS 학교 기본정보 조회 (4.6)", response_model=SrFormat)
async def get_bssm_neis_school_info(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    NEIS Open API에서 부산소프트웨어마이스터고등학교 공식 학교 기본정보를 조회합니다.
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    info = await get_school_info()
    if not info:
        return error_response(502, "NEIS_API_ERROR", "NEIS Open API로부터 학교 정보를 조회할 수 없습니다.")

    return SrFormat(status_code=200, success=True, data=info).model_dump()


@admin_router.get("/neis/classes", summary="부산소프트웨어마이스터고 NEIS 학년도별 개설 학급 목록 조회 (4.6)", response_model=SrFormat)
async def get_bssm_neis_classes(
    year: Optional[int] = Query(None, description="조회 학년도 (미지정 시 활성 학년도)"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    NEIS Open API에서 부산소프트웨어마이스터고등학교의 해당 학년도 인가 학급 목록을 동적 조회합니다.
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    target_year = year
    if target_year is None:
        async with conn.cursor(cursor=DictCursor) as cur:
            await cur.execute("SELECT year FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            row = await cur.fetchone()
            target_year = row["year"] if row else get_current_academic_year()

    classes = await get_classes_for_year(target_year)
    return SrFormat(
        status_code=200,
        success=True,
        data={
            "schoolName": NEIS_SCHOOL_NAME,
            "schoolCode": NEIS_SCHOOL_CODE,
            "officeCode": NEIS_OFFICE_CODE,
            "year": target_year,
            "classCount": len(classes),
            "classes": classes,
        },
    ).model_dump()


@admin_router.post("/students/batch", summary="학생 명단 엑셀/CSV 일괄 등록 (4.6)", response_model=SrFormat)
@admin_router.post("/students/batch/neis", summary="NEIS 표준 양식 학생 명단 일괄 등록 (4.6)", response_model=SrFormat)
async def batch_register_students(
    request: Request,
    file: Optional[UploadFile] = File(None),
    year: Optional[int] = Form(None),
    overwrite: Optional[bool] = Form(False),
    validate_with_neis: Optional[bool] = Form(None),
    default_email_domain: Optional[str] = Form("bssm.hs.kr"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 명단 엑셀(XLSX) 또는 CSV 파일 일괄 등록 엔드포인트 (TODO.md 4.6)
    - 관리자 전용
    - XLSX / CSV 파일 자동 감지 및 멀티 인코딩(UTF-8, CP949, EUC-KR) 파싱
    - 수식 인젝션(Formula Injection) 방어 및 XSS 방어
    - 부산소프트웨어마이스터고 NEIS 학급 정보 실시간 교차 검증 (validate_with_neis)
    - 학생 계정(students) 및 학년도별 학적(student_academic_records) 단일 트랜잭션 원자적 등록
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    content_type = request.headers.get("content-type", "").lower()
    query_params = request.query_params

    # 1. 쿼리/폼/JSON 파라미터 우선순위 정규화
    target_year = year if year is not None else (int(query_params.get("year")) if query_params.get("year") else None)
    is_overwrite = bool(overwrite if overwrite is not None else query_params.get("overwrite", False))
    should_validate_neis = validate_with_neis if validate_with_neis is not None else True
    email_domain = default_email_domain or query_params.get("default_email_domain", "bssm.hs.kr")

    valid_records: List[Dict[str, Any]] = []
    parsing_errors: List[Dict[str, Any]] = []

    # 2. 파일 업로드 또는 JSON 바디 분기 처리
    if file is not None:
        file_bytes = await file.read()
        if len(file_bytes) > 10 * 1024 * 1024:
            return error_response(400, "FILE_TOO_LARGE", "파일 크기는 최대 10MB까지 허용됩니다.")
        try:
            valid_records, parsing_errors = parse_student_batch_file(
                file_bytes=file_bytes,
                filename=file.filename or "students.xlsx",
                default_email_domain=email_domain,
            )
        except Exception as e:
            return error_response(400, "FILE_PARSE_ERROR", f"파일 파싱 실패: {str(e)}")
    elif "application/json" in content_type:
        try:
            body = await request.json()
            if "year" in body and target_year is None:
                target_year = int(body["year"])
            if "overwrite" in body:
                is_overwrite = bool(body["overwrite"])
            if "validateWithNeis" in body:
                should_validate_neis = bool(body["validateWithNeis"])
            elif "validate_with_neis" in body:
                should_validate_neis = bool(body["validate_with_neis"])
            if "default_email_domain" in body:
                email_domain = body["default_email_domain"]

            raw_students = body.get("students", [])

            seen_students = set()
            seen_emails = set()

            for idx, item in enumerate(raw_students, start=1):
                clean_name = sanitize_text(item.get("name"))
                if not clean_name:
                    parsing_errors.append({"row": idx, "name": "", "reason": "학생 성명이 누락되었습니다."})
                    continue
                if len(clean_name) > 20:
                    parsing_errors.append({"row": idx, "name": clean_name, "reason": "학생 성명은 20자를 초과할 수 없습니다."})
                    continue
                name = clean_name

                grade = item.get("grade")
                class_no = item.get("class_no") if item.get("class_no") is not None else item.get("classNo")
                student_no = item.get("student_no") if item.get("student_no") is not None else item.get("studentNo")

                snum = item.get("student_number") or item.get("studentNumber")
                if snum:
                    g, c, n = parse_student_number_field(snum)
                    if g and c and n:
                        grade, class_no, student_no = g, c, n

                if grade is None or not (1 <= grade <= 3):
                    parsing_errors.append({"row": idx, "name": name, "reason": f"유효하지 않은 학년입니다 ({grade})."})
                    continue
                if class_no is None or not (1 <= class_no <= 20):
                    parsing_errors.append({"row": idx, "name": name, "reason": f"유효하지 않은 반입니다 ({class_no})."})
                    continue
                if student_no is None or not (1 <= student_no <= 50):
                    parsing_errors.append({"row": idx, "name": name, "reason": f"유효하지 않은 번호입니다 ({student_no})."})
                    continue

                email = (item.get("email") or "").strip().lower()
                if email:
                    if not EMAIL_REGEX.match(email):
                        parsing_errors.append({"row": idx, "name": name, "reason": f"올바르지 않은 이메일 형식입니다: {email}"})
                        continue
                else:
                    email = f"s{grade}{class_no:02d}{student_no:02d}@{email_domain}"

                st_key = (grade, class_no, student_no)
                if st_key in seen_students:
                    parsing_errors.append({"row": idx, "name": name, "reason": f"중복된 학년/반/번호 ({grade}-{class_no}-{student_no})"})
                    continue
                seen_students.add(st_key)

                if email in seen_emails:
                    parsing_errors.append({"row": idx, "name": name, "reason": f"중복된 이메일 ({email})"})
                    continue
                seen_emails.add(email)

                valid_records.append({
                    "row_number": idx,
                    "name": name,
                    "grade": grade,
                    "class_no": class_no,
                    "student_no": student_no,
                    "email": email,
                })
        except Exception as e:
            return error_response(400, "VALIDATION_ERROR", f"JSON 바디 파싱 실패: {str(e)}")
    else:
        return error_response(400, "VALIDATION_ERROR", "업로드할 엑셀/CSV 파일(file) 또는 학생 명단 JSON(students)이 필요합니다.")

    # 3. 대상 학년도(year_id) 확인
    async with conn.cursor(cursor=DictCursor) as cur:
        if target_year is None:
            await cur.execute("SELECT year_id, year FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "ITEM_NOT_FOUND", "등록된 학년도가 없습니다. 먼저 학년도를 등록해주세요.")
            target_year_id = y_row["year_id"]
            target_year = y_row["year"]
        else:
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "ITEM_NOT_FOUND", f"{target_year} 학년도가 시스템에 존재하지 않습니다.")
            target_year_id = y_row["year_id"]

    # 4. NEIS 학급 실시간 교차 검증 (validate_with_neis = True인 경우)
    neis_validated_classes: Dict[Tuple[int, int], bool] = {}
    final_to_insert: List[Dict[str, Any]] = []

    if should_validate_neis and valid_records:
        distinct_classes = set((st["grade"], st["class_no"]) for st in valid_records)
        for g, c in distinct_classes:
            is_valid, err_msg = await validate_class_against_neis(target_year, g, c)
            neis_validated_classes[(g, c)] = is_valid
            if not is_valid:
                for st in valid_records:
                    if st["grade"] == g and st["class_no"] == c:
                        parsing_errors.append({
                            "row": st.get("row_number", 0),
                            "name": st["name"],
                            "reason": err_msg or f"NEIS 미인가 학급 ({g}학년 {c}반)",
                        })

        for st in valid_records:
            if neis_validated_classes.get((st["grade"], st["class_no"]), True):
                final_to_insert.append(st)
    else:
        final_to_insert = valid_records

    # 5. DB 트랜잭션 원자적 일괄 등록
    created_count = 0
    updated_count = 0
    skipped_count = 0

    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            # [2.17 성능 최적화] 사전 벌크 조회: 기존 교사 이메일, 기존 학생 정보, 기존 학적 정보 1회 일괄 조회
            all_emails = list({st["email"] for st in final_to_insert if st.get("email")})
            existing_teacher_emails: Set[str] = set()
            existing_students_map: Dict[str, Dict[str, Any]] = {}
            existing_records_map: Dict[int, Dict[str, Any]] = {}

            if all_emails:
                # 1) 교사 명단 이메일 교차 중복 일괄 사전 검증 (Role Confusion 방지)
                t_placeholders = ", ".join(["%s"] * len(all_emails))
                await cur.execute(
                    f"SELECT email FROM teachers WHERE email IN ({t_placeholders}) AND is_deleted = FALSE",
                    tuple(all_emails),
                )
                t_rows = await cur.fetchall() or []
                for tr in t_rows:
                    em = tr["email"] if isinstance(tr, dict) else tr[0]
                    if em:
                        existing_teacher_emails.add(em)

                # 2) 학생 테이블(students) 일괄 사전 조회
                s_placeholders = ", ".join(["%s"] * len(all_emails))
                await cur.execute(
                    f"SELECT student_id, name, email, is_deleted FROM students WHERE email IN ({s_placeholders})",
                    tuple(all_emails),
                )
                s_rows = await cur.fetchall() or []
                for sr in s_rows:
                    em = sr["email"] if isinstance(sr, dict) else sr[2]
                    if em:
                        existing_students_map[em] = sr

                # 3) 기존 학생들의 학년도별 학적(student_academic_records) 일괄 사전 조회
                ex_sids = [
                    (sr["student_id"] if isinstance(sr, dict) else sr[0])
                    for sr in existing_students_map.values()
                ]
                if ex_sids:
                    sid_placeholders = ", ".join(["%s"] * len(ex_sids))
                    await cur.execute(
                        f"""
                        SELECT record_id, student_id, grade, class, number
                        FROM student_academic_records
                        WHERE year_id = %s AND student_id IN ({sid_placeholders})
                        """,
                        (target_year_id, *ex_sids),
                    )
                    r_rows = await cur.fetchall() or []
                    for rr in r_rows:
                        sid = rr["student_id"] if isinstance(rr, dict) else rr[1]
                        existing_records_map[sid] = rr

            for st in final_to_insert:
                st_email = st["email"]
                # [보안 1.24] 교사 명단(teachers)에 이미 등록된 이메일인지 검증
                if st_email in existing_teacher_emails:
                    parsing_errors.append({
                        "row": st.get("row_number", 0),
                        "name": st["name"],
                        "reason": f"교사 명단에 이미 등록된 이메일({st_email})이므로 학생으로 등록할 수 없습니다.",
                    })
                    skipped_count += 1
                    continue

                # 1) students 테이블 조회/생성
                ex_student = existing_students_map.get(st_email)
                if ex_student:
                    student_id = ex_student["student_id"] if isinstance(ex_student, dict) else ex_student[0]
                    ex_name = ex_student["name"] if isinstance(ex_student, dict) else ex_student[1]
                    is_del = ex_student.get("is_deleted") if isinstance(ex_student, dict) else (ex_student[3] if len(ex_student) > 3 else False)
                    if is_overwrite and (ex_name != st["name"] or is_del):
                        await cur.execute(
                            "UPDATE students SET name = %s, is_deleted = FALSE WHERE student_id = %s",
                            (st["name"], student_id),
                        )
                else:
                    await cur.execute(
                        "INSERT INTO students (name, email, status, is_deleted) VALUES (%s, %s, 0, FALSE)",
                        (st["name"], st_email),
                    )
                    student_id = cur.lastrowid
                    if not student_id:
                        await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
                        last_id_row = await cur.fetchone()
                        student_id = last_id_row["last_id"] if isinstance(last_id_row, dict) else last_id_row[0]
                    created_count += 1
                    existing_students_map[st_email] = {
                        "student_id": student_id,
                        "name": st["name"],
                        "email": st_email,
                        "is_deleted": False,
                    }

                # 2) student_academic_records 테이블 조회/생성/갱신
                ex_record = existing_records_map.get(student_id)
                if ex_record:
                    rec_id = ex_record["record_id"] if isinstance(ex_record, dict) else ex_record[0]
                    if is_overwrite:
                        await cur.execute(
                            """
                            UPDATE student_academic_records
                            SET grade = %s, class = %s, number = %s
                            WHERE record_id = %s
                            """,
                            (st["grade"], st["class_no"], st["student_no"], rec_id),
                        )
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    await cur.execute(
                        """
                        INSERT INTO student_academic_records (student_id, year_id, grade, class, number)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (student_id, target_year_id, st["grade"], st["class_no"], st["student_no"]),
                    )
                    rec_id = cur.lastrowid
                    if not rec_id:
                        await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
                        last_id_row = await cur.fetchone()
                        rec_id = last_id_row["last_id"] if isinstance(last_id_row, dict) else last_id_row[0]
                    existing_records_map[student_id] = {
                        "record_id": rec_id,
                        "student_id": student_id,
                        "grade": st["grade"],
                        "class": st["class_no"],
                        "number": st["student_no"],
                    }
                    if ex_student:
                        updated_count += 1

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
            "year": target_year,
            "yearId": target_year_id,
            "totalProcessed": len(final_to_insert) + len(parsing_errors),
            "total_count": len(final_to_insert) + len(parsing_errors),
            "successCount": len(final_to_insert),
            "success_count": len(final_to_insert),
            "failedCount": len(parsing_errors),
            "failed_count": len(parsing_errors),
            "imported_count": created_count + updated_count,
            "createdStudentsCount": created_count,
            "updatedRecordsCount": updated_count,
            "skippedRecordsCount": skipped_count,
            "errors": parsing_errors,
            "neisValidation": {
                "performed": should_validate_neis,
                "schoolName": NEIS_SCHOOL_NAME,
                "officeName": NEIS_OFFICE_NAME,
                "schoolCode": NEIS_SCHOOL_CODE,
                "officeCode": NEIS_OFFICE_CODE,
            },
        },
    ).model_dump()


# ==============================================================================
# 5. 교사 명단 엑셀/CSV 일괄 등록 & NEIS 연동 API (4.7)
# ==============================================================================

@admin_router.get("/neis/teacher-info", summary="NEIS 교원 명단 API 미제공 법적 근거 안내 (4.7)", response_model=SrFormat)
async def get_neis_teacher_integration_info(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    NEIS Open API의 교원 명단 직접 조회 API 미제공에 대한 구체적 법적/기술적 근거 및 학급 배정 연동 가이드를 반환합니다.
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    return SrFormat(
        status_code=200,
        success=True,
        data=explain_neis_teacher_api_limitation(),
    ).model_dump()


@admin_router.post("/teachers/batch", summary="교사 명단 엑셀/CSV 일괄 등록 및 학급 배정 (4.7)", response_model=SrFormat)
async def batch_register_teachers(
    request: Request,
    file: Optional[UploadFile] = File(None),
    year: Optional[int] = Form(None),
    overwrite: Optional[bool] = Form(True),
    validate_with_neis: Optional[bool] = Form(None),
    default_email_domain: Optional[str] = Form("bssm.hs.kr"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    교사 명단 엑셀(XLSX) 또는 CSV 파일 일괄 등록 및 담당 학급(담임) 배정 엔드포인트 (TODO.md 4.7)
    - 관리자 전용
    - XLSX / CSV 파일 자동 감지 및 멀티 인코딩(UTF-8, CP949, EUC-KR) 파싱
    - 수식 인젝션(Formula Injection) 방어 및 XSS 방어
    - 담임 배정 시 부산소프트웨어마이스터고 NEIS 개설 학급 실시간 교차 검증 (validate_with_neis)
    - 교사 정보(teachers) 단일 트랜잭션 원자적 등록 및 갱신
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    content_type = request.headers.get("content-type", "").lower()
    query_params = request.query_params

    # 1. 쿼리/폼/JSON 파라미터 우선순위 정규화
    target_year = year if year is not None else (int(query_params.get("year")) if query_params.get("year") else None)
    is_overwrite = bool(overwrite if overwrite is not None else query_params.get("overwrite", True))
    should_validate_neis = validate_with_neis if validate_with_neis is not None else True
    email_domain = default_email_domain or query_params.get("default_email_domain", "bssm.hs.kr")

    valid_records: List[Dict[str, Any]] = []
    parsing_errors: List[Dict[str, Any]] = []

    # 2. 파일 업로드 또는 JSON 바디 분기 처리
    if file is not None:
        file_bytes = await file.read()
        if len(file_bytes) > 10 * 1024 * 1024:
            return error_response(400, "FILE_TOO_LARGE", "파일 크기는 최대 10MB까지 허용됩니다.")
        try:
            valid_records, parsing_errors = parse_teacher_batch_file(
                file_bytes=file_bytes,
                filename=file.filename or "teachers.xlsx",
                default_email_domain=email_domain,
            )
        except Exception as e:
            return error_response(400, "FILE_PARSE_ERROR", f"파일 파싱 실패: {str(e)}")
    elif "application/json" in content_type:
        try:
            body = await request.json()
            if "year" in body and target_year is None:
                target_year = int(body["year"])
            if "overwrite" in body:
                is_overwrite = bool(body["overwrite"])
            if "validateWithNeis" in body:
                should_validate_neis = bool(body["validateWithNeis"])
            elif "validate_with_neis" in body:
                should_validate_neis = bool(body["validate_with_neis"])
            if "default_email_domain" in body:
                email_domain = body["default_email_domain"]

            raw_teachers = body.get("teachers", [])

            seen_emails = set()
            seen_homeroom_positions = set()

            for idx, item in enumerate(raw_teachers, start=1):
                clean_name = sanitize_text(item.get("name"))
                if not clean_name:
                    parsing_errors.append({"row": idx, "name": "", "reason": "교사 성명이 누락되었습니다."})
                    continue
                if len(clean_name) > 20:
                    parsing_errors.append({"row": idx, "name": clean_name, "reason": "교사 성명은 20자를 초과할 수 없습니다."})
                    continue
                name = clean_name

                # 교과
                subject = None
                raw_subj = item.get("subject")
                if raw_subj:
                    clean_subj = sanitize_text(raw_subj)
                    if len(clean_subj) > 20:
                        parsing_errors.append({"row": idx, "name": clean_name, "reason": "담당교과명은 20자를 초과할 수 없습니다."})
                        continue
                    subject = clean_subj

                # 직책 및 부서
                position = None
                raw_pos = item.get("position")
                if raw_pos:
                    clean_pos = sanitize_text(raw_pos)
                    if clean_pos:
                        if len(clean_pos) > 30:
                            parsing_errors.append({"row": idx, "name": clean_name, "reason": "직책명은 30자를 초과할 수 없습니다."})
                            continue
                        position = clean_pos

                department = None
                raw_dept = item.get("department")
                if raw_dept:
                    clean_dept = sanitize_text(raw_dept)
                    if clean_dept:
                        if len(clean_dept) > 40:
                            parsing_errors.append({"row": idx, "name": clean_name, "reason": "부서명은 40자를 초과할 수 없습니다."})
                            continue
                        department = clean_dept

                # 학년/반 (담임 및 학년부장)
                grade = item.get("grade")
                class_no = item.get("class_no") if item.get("class_no") is not None else item.get("classNo")

                if item.get("homeroom"):
                    g, c = parse_homeroom_field(item.get("homeroom"))
                    if g and c:
                        grade, class_no = g, c

                is_grade_head_pos = position == "학년부장"
                if not is_grade_head_pos and ((grade is not None and class_no is None) or (grade is None and class_no is not None)):
                    parsing_errors.append({
                        "row": idx,
                        "name": clean_name,
                        "reason": "담임 학년과 반은 함께 지정되어야 합니다. (비담임인 경우 둘 다 비워두세요.)",
                    })
                    continue
                if is_grade_head_pos and grade is None:
                    parsing_errors.append({
                        "row": idx,
                        "name": clean_name,
                        "reason": "학년부장은 담당 학년이 필수입니다.",
                    })
                    continue

                if grade is not None:
                    if not (1 <= grade <= 3):
                        parsing_errors.append({"row": idx, "name": clean_name, "reason": f"유효하지 않은 담당 학년입니다 ({grade})."})
                        continue
                    if class_no is not None and not (1 <= class_no <= 20):
                        parsing_errors.append({"row": idx, "name": clean_name, "reason": f"유효하지 않은 담당 반입니다 ({class_no})."})
                        continue

                    if not position:
                        position = "정담임"

                    homeroom_pos_key = (grade, class_no, position)
                    if homeroom_pos_key in seen_homeroom_positions:
                        parsing_errors.append({
                            "row": idx,
                            "name": clean_name,
                            "reason": f"중복된 담임 학급 배정 ({grade}학년 {class_no or '전체'}반 {position})",
                        })
                        continue
                    seen_homeroom_positions.add(homeroom_pos_key)
                elif not position:
                    position = "교과전담"

                # 이메일
                email = (item.get("email") or "").strip().lower()
                if email:
                    if not EMAIL_REGEX.match(email):
                        parsing_errors.append({"row": idx, "name": clean_name, "reason": f"올바르지 않은 이메일 형식입니다: {email}"})
                        continue
                else:
                    en_name = re.sub(r"[^a-zA-Z0-9]", "", clean_name).lower()
                    if en_name:
                        email = f"{en_name}@{email_domain}"
                    else:
                        email = f"teacher_{idx}@{email_domain}"

                if email in seen_emails:
                    parsing_errors.append({"row": idx, "name": clean_name, "reason": f"중복된 이메일 ({email})"})
                    continue
                seen_emails.add(email)

                valid_records.append({
                    "row_number": idx,
                    "name": name,
                    "email": email,
                    "subject": subject,
                    "position": position,
                    "department": department,
                    "grade": grade,
                    "class_no": class_no,
                    "is_homeroom": bool(grade and class_no),
                })
        except Exception as e:
            return error_response(400, "VALIDATION_ERROR", f"JSON 바디 파싱 실패: {str(e)}")
    else:
        return error_response(400, "VALIDATION_ERROR", "업로드할 엑셀/CSV 파일(file) 또는 교사 명단 JSON(teachers)이 필요합니다.")

    # 3. 대상 학년도 확인 (NEIS 검증용)
    async with conn.cursor(cursor=DictCursor) as cur:
        if target_year is None:
            await cur.execute("SELECT year FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            y_row = await cur.fetchone()
            target_year = y_row["year"] if y_row else get_current_academic_year()

    # 4. NEIS 학급 실시간 교차 검증 (담임 배정 건 대상)
    neis_validated_classes: Dict[Tuple[int, int], bool] = {}
    final_to_insert: List[Dict[str, Any]] = []

    if should_validate_neis and valid_records:
        distinct_classes = set(
            (t["grade"], t["class_no"]) for t in valid_records if t["grade"] is not None and t["class_no"] is not None
        )
        for g, c in distinct_classes:
            is_valid, err_msg = await validate_class_against_neis(target_year, g, c)
            neis_validated_classes[(g, c)] = is_valid
            if not is_valid:
                for t in valid_records:
                    if t["grade"] == g and t["class_no"] == c:
                        parsing_errors.append({
                            "row": t.get("row_number", 0),
                            "name": t["name"],
                            "reason": err_msg or f"NEIS 미인가 학급 ({g}학년 {c}반 담임 배정 불가)",
                        })

        for t in valid_records:
            if t["grade"] is None or neis_validated_classes.get((t["grade"], t["class_no"]), True):
                final_to_insert.append(t)
    else:
        final_to_insert = valid_records

    # 5. DB 트랜잭션 원자적 일괄 등록 및 갱신
    created_count = 0
    updated_count = 0
    skipped_count = 0

    try:
        await conn.autocommit(False)
        async with conn.cursor(cursor=DictCursor) as cur:
            # [2.17 성능 최적화] 사전 벌크 조회: 기존 학생 이메일, 기존 교사 정보 1회 일괄 조회
            all_emails = list({t["email"] for t in final_to_insert if t.get("email")})
            existing_student_emails: Set[str] = set()
            existing_teachers_map: Dict[str, Dict[str, Any]] = {}

            if all_emails:
                # 1) 학생 명단(students) 이메일 교차 중복 일괄 사전 검증 (Role Confusion 방지)
                s_placeholders = ", ".join(["%s"] * len(all_emails))
                await cur.execute(
                    f"SELECT email FROM students WHERE email IN ({s_placeholders}) AND is_deleted = FALSE",
                    tuple(all_emails),
                )
                s_rows = await cur.fetchall() or []
                for sr in s_rows:
                    em = sr["email"] if isinstance(sr, dict) else sr[0]
                    if em:
                        existing_student_emails.add(em)

                # 2) 교사 테이블(teachers) 일괄 사전 조회
                t_placeholders = ", ".join(["%s"] * len(all_emails))
                await cur.execute(
                    f"SELECT teachers_id, name, subject, position, department, grade, class, email, is_deleted FROM teachers WHERE email IN ({t_placeholders})",
                    tuple(all_emails),
                )
                t_rows = await cur.fetchall() or []
                for tr in t_rows:
                    em = tr["email"] if isinstance(tr, dict) else tr[7]
                    if em:
                        existing_teachers_map[em] = tr

            for t in final_to_insert:
                t_email = t["email"]
                # [보안 1.24] 학생 명단(students)에 이미 등록된 이메일인지 검증
                if t_email in existing_student_emails:
                    parsing_errors.append({
                        "row": t.get("row_number", 0),
                        "name": t["name"],
                        "reason": f"학생 명단에 이미 등록된 이메일({t_email})이므로 교사로 등록할 수 없습니다.",
                    })
                    skipped_count += 1
                    continue

                ex_teacher = existing_teachers_map.get(t_email)
                if ex_teacher:
                    teachers_id = ex_teacher["teachers_id"] if isinstance(ex_teacher, dict) else ex_teacher[0]
                    if is_overwrite:
                        await cur.execute(
                            """
                            UPDATE teachers
                            SET name = %s,
                                subject = %s,
                                position = %s,
                                department = %s,
                                grade = %s,
                                class = %s,
                                is_deleted = FALSE
                            WHERE teachers_id = %s
                            """,
                            (t["name"], t["subject"], t.get("position", "교과전담"), t.get("department"), t["grade"], t["class_no"], teachers_id),
                        )
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    await cur.execute(
                        """
                        INSERT INTO teachers (name, subject, position, department, grade, class, email, is_deleted)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                        """,
                        (t["name"], t["subject"], t.get("position", "교과전담"), t.get("department"), t["grade"], t["class_no"], t_email),
                    )
                    new_tid = cur.lastrowid
                    if not new_tid:
                        await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
                        last_id_row = await cur.fetchone()
                        new_tid = last_id_row["last_id"] if isinstance(last_id_row, dict) else last_id_row[0]
                    created_count += 1
                    existing_teachers_map[t_email] = {
                        "teachers_id": new_tid,
                        "email": t_email,
                        "name": t["name"],
                        "subject": t["subject"],
                        "position": t.get("position", "교과전담"),
                        "department": t.get("department"),
                        "grade": t["grade"],
                        "class": t["class_no"],
                        "is_deleted": False,
                    }

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
            "year": target_year,
            "totalProcessed": len(final_to_insert) + len(parsing_errors),
            "total_count": len(final_to_insert) + len(parsing_errors),
            "successCount": len(final_to_insert),
            "success_count": len(final_to_insert),
            "failedCount": len(parsing_errors),
            "failed_count": len(parsing_errors),
            "total_teachers": created_count + updated_count,
            "createdTeachersCount": created_count,
            "updatedTeachersCount": updated_count,
            "skippedTeachersCount": skipped_count,
            "errors": parsing_errors,
            "neisValidation": {
                "performed": should_validate_neis,
                "schoolName": NEIS_SCHOOL_NAME,
                "officeName": NEIS_OFFICE_NAME,
                "schoolCode": NEIS_SCHOOL_CODE,
                "officeCode": NEIS_OFFICE_CODE,
            },
        },
    ).model_dump()


# ==============================================================================
# 5. 비동기 대량 등록 및 SSE 진행률 스트리밍 API (Async Batch & SSE)
# ==============================================================================

async def _run_student_batch_job(
    task_id: str,
    valid_records: List[Dict[str, Any]],
    parsing_errors: List[Dict[str, Any]],
    target_year: int,
    target_year_id: int,
    is_overwrite: bool,
    should_validate_neis: bool,
    existing_conn: Any = None,
) -> None:
    """백그라운드에서 실행되는 학생 대량 등록 작업 워커"""
    import database
    from routers.auth import hash_password

    conn_context = None
    if existing_conn is not None:
        conn = existing_conn
    elif database.pool is not None:
        conn_context = database.pool.acquire()
        conn = await conn_context.__aenter__()
    else:
        batch_task_manager.fail_task(task_id, "데이터베이스 커넥션 풀을 찾을 수 없습니다.")
        return

    try:
        total = len(valid_records)
        created_count = 0
        updated_count = 0
        skipped_count = 0

        # 1. NEIS 학급 실시간 검증
        final_to_insert = []
        neis_validated_classes: Dict[Tuple[int, int], bool] = {}

        if should_validate_neis and valid_records:
            distinct_classes = set((st["grade"], st["class_no"]) for st in valid_records)
            for g, c in distinct_classes:
                is_valid, err_msg = await validate_class_against_neis(target_year, g, c)
                neis_validated_classes[(g, c)] = is_valid
                if not is_valid:
                    for st in valid_records:
                        if st["grade"] == g and st["class_no"] == c:
                            parsing_errors.append({
                                "row": st.get("row_number", 0),
                                "name": st["name"],
                                "reason": err_msg or f"NEIS 미인가 학급 ({g}학년 {c}반)",
                            })

            for st in valid_records:
                if neis_validated_classes.get((st["grade"], st["class_no"]), True):
                    final_to_insert.append(st)
        else:
            final_to_insert = valid_records

        # 2. 50건 청크 단위 트랜잭션 적재 및 진행률 통지
        chunk_size = 50
        for i in range(0, len(final_to_insert), chunk_size):
            chunk = final_to_insert[i:i + chunk_size]
            await conn.autocommit(False)
            async with conn.cursor(cursor=DictCursor) as cur:
                for st in chunk:
                    email = st.get("email")
                    name = st["name"]
                    grade = st["grade"]
                    class_no = st["class_no"]
                    student_no = st["student_no"]

                    await cur.execute("SELECT student_id, uuid FROM students WHERE email = %s LIMIT 1", (email,))
                    ex_st = await cur.fetchone()

                    if ex_st:
                        s_id = ex_st["student_id"] if isinstance(ex_st, dict) else ex_st[0]
                        if is_overwrite:
                            await cur.execute("UPDATE students SET name = %s WHERE student_id = %s", (name, s_id))
                            updated_count += 1
                        else:
                            skipped_count += 1
                    else:
                        new_uuid = str(uuid.uuid4())
                        init_pw = f"bssm{target_year}!"
                        pw_hash = hash_password(init_pw)
                        await cur.execute(
                            "INSERT INTO users (uuid, email, password_hash, role) VALUES (%s, %s, %s, 'student')",
                            (new_uuid, email, pw_hash),
                        )
                        await cur.execute(
                            "INSERT INTO students (uuid, name, email) VALUES (%s, %s, %s)",
                            (new_uuid, name, email),
                        )
                        await cur.execute("SELECT LAST_INSERT_ID() AS last_id")
                        last_row = await cur.fetchone()
                        s_id = last_row[0] if isinstance(last_row, (list, tuple)) else last_row["last_id"]
                        created_count += 1

                    # 학적(student_academic_records) 동기화
                    await cur.execute(
                        """
                        INSERT INTO student_academic_records (student_id, year_id, grade, class_no, student_no)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE grade = VALUES(grade), class_no = VALUES(class_no), student_no = VALUES(student_no)
                        """,
                        (s_id, target_year_id, grade, class_no, student_no),
                    )

            await conn.commit()
            processed_so_far = min(total, i + len(chunk))
            batch_task_manager.update_progress(
                task_id=task_id,
                processed_count=processed_so_far,
                created_count=created_count,
                updated_count=updated_count,
                skipped_count=skipped_count,
            )

        batch_task_manager.complete_task(
            task_id=task_id,
            created_count=created_count,
            updated_count=updated_count,
            skipped_count=skipped_count,
            parsing_errors=parsing_errors,
        )
    except Exception as e:
        if conn:
            try:
                await conn.rollback()
            except Exception:
                pass
        batch_task_manager.fail_task(task_id, f"배치 처리 중 오류 발생: {str(e)}")
    finally:
        if conn:
            try:
                await conn.autocommit(True)
            except Exception:
                pass
        if conn_context is not None:
            await conn_context.__aexit__(None, None, None)


@admin_router.post("/students/batch/async", summary="학생 명단 비동기 일괄 등록 (SSE 진행률 연동)", response_model=SrFormat)
async def batch_register_students_async(
    background_tasks: BackgroundTasks,
    request: Request,
    file: Optional[UploadFile] = File(None),
    year: Optional[int] = Form(None),
    overwrite: Optional[bool] = Form(False),
    validate_with_neis: Optional[bool] = Form(None),
    default_email_domain: Optional[str] = Form("bssm.hs.kr"),
    conn: Any = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    학생 명단 비동기 일괄 등록 엔드포인트
    - 대량 등록 시 HTTP 504 타임아웃 방어 (202 Accepted 반환)
    - BackgroundTasks 워커로 적재 실행
    - 반환된 taskId로 /api/admin/batch/tasks/{taskId}/progress SSE 구독 가능
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    query_params = request.query_params
    target_year = year if year is not None else (int(query_params.get("year")) if query_params.get("year") else None)
    is_overwrite = bool(overwrite if overwrite is not None else query_params.get("overwrite", False))
    should_validate_neis = validate_with_neis if validate_with_neis is not None else True
    email_domain = default_email_domain or query_params.get("default_email_domain", "bssm.hs.kr")

    if file is None:
        return error_response(400, "VALIDATION_ERROR", "업로드할 엑셀/CSV 파일(file)이 필요합니다.")

    file_bytes = await file.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        return error_response(400, "FILE_TOO_LARGE", "파일 크기는 최대 10MB까지 허용됩니다.")

    try:
        valid_records, parsing_errors = parse_student_batch_file(
            file_bytes=file_bytes,
            filename=file.filename or "students.xlsx",
            default_email_domain=email_domain,
        )
    except Exception as e:
        return error_response(400, "FILE_PARSE_ERROR", f"파일 파싱 실패: {str(e)}")

    async with conn.cursor(cursor=DictCursor) as cur:
        if target_year is None:
            await cur.execute("SELECT year_id, year FROM academic_years ORDER BY is_activated DESC, year DESC LIMIT 1")
            y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "ITEM_NOT_FOUND", "등록된 학년도가 없습니다.")
            target_year_id = y_row["year_id"]
            target_year = y_row["year"]
        else:
            await cur.execute("SELECT year_id FROM academic_years WHERE year = %s LIMIT 1", (target_year,))
            y_row = await cur.fetchone()
            if not y_row:
                return error_response(404, "ITEM_NOT_FOUND", f"{target_year} 학년도가 시스템에 존재하지 않습니다.")
            target_year_id = y_row["year_id"]

    total_count = len(valid_records) + len(parsing_errors)
    task_id = batch_task_manager.create_task(
        mode="students",
        total_count=total_count,
        filename=file.filename,
    )

    # 백그라운드 태스크 등록
    background_tasks.add_task(
        _run_student_batch_job,
        task_id=task_id,
        valid_records=valid_records,
        parsing_errors=parsing_errors,
        target_year=target_year,
        target_year_id=target_year_id,
        is_overwrite=is_overwrite,
        should_validate_neis=should_validate_neis,
        existing_conn=conn,
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=SrFormat(
            status_code=202,
            success=True,
            data={
                "taskId": task_id,
                "status": "PROCESSING",
                "totalCount": total_count,
                "message": "학생 명단 일괄 등록 작업이 시작되었습니다. SSE 진행률 스트림을 통해 실시간 진행 상태를 확인할 수 있습니다.",
                "progressUrl": f"/api/admin/batch/tasks/{task_id}/progress",
                "reportUrl": f"/api/admin/batch/tasks/{task_id}/report/download",
            },
        ).model_dump(),
    )


@admin_router.get("/batch/tasks/{task_id}/progress", summary="대량 등록 작업 SSE 실시간 진행률 스트림")
async def stream_batch_progress(
    task_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    대량 등록 작업 실시간 SSE 스트림
    - media_type: text/event-stream
    - 3초마다 Keep-Alive 핑 유지
    - 상태 변경 시 progress, processedCount, status 즉시 이벤트 푸시
    """
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    return StreamingResponse(
        batch_task_manager.subscribe_progress(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@admin_router.get("/batch/tasks/{task_id}", summary="대량 등록 작업 상태 단건 조회", response_model=SrFormat)
async def get_batch_task_status(
    task_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """대량 등록 작업 상태 JSON 단건 조회"""
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    task = batch_task_manager.get_task(task_id)
    if not task:
        return error_response(404, "TASK_NOT_FOUND", "해당 배치 작업을 찾을 수 없습니다.")

    return SrFormat(status_code=200, success=True, data=task).model_dump()


@admin_router.get("/batch/tasks/{task_id}/report/download", summary="대량 등록 실패 행 상세 리포트 CSV 다운로드")
async def download_batch_error_report(
    task_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """배치 작업 실패 행 리포트 CSV 다운로드"""
    admin_err = check_admin_access(current_user)
    if admin_err:
        return admin_err

    csv_content = batch_task_manager.generate_csv_report(task_id)
    filename = f"batch_error_report_{task_id}.csv"
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


