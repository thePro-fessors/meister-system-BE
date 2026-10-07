"""
core/excel_parser.py - 학생 명단 일괄 등록용 XLSX / CSV 파서

주요 기능:
1. XLSX 및 CSV 파일 자동 감지 및 파싱
2. CSV 다중 인코딩 지원: UTF-8(BOM 포함), CP949, EUC-KR
3. 유연한 컬럼 헤더 매핑:
   - NEIS 표준 추출 서식, 학교 자체 서식, 영문 헤더 등 폭넓은 별칭 지원
   - 학번(예: 1101, 2315) 자동 분해(학년, 반, 번호 자동 추출)
4. 보안 방어:
   - CSV / Excel Formula Injection 방어: `=, +, -, @` 수식 접두어 살균
   - HTML XSS 방어: `html.escape`
   - 파일 용량 및 최대 행 수(최대 1,000행) DOS 공격 방어
"""

import csv
import html
import io
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import openpyxl

# 이메일 기본 정규식
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

# 수식 인젝션 위험 접두어 패턴
FORMULA_PREFIX_REGEX = re.compile(r"^[\s]*[=+\-@\t\r]")


def sanitize_text(value: Any) -> str:
    """수식 인젝션(Formula Injection) 방어 및 문자열 정제"""
    if value is None:
        return ""
    text = str(value).strip()
    # 수식 기호(=, +, -, @)로 시작하는 경우 수식 실행을 방지하기 위해 앞선 특수문자 제거
    while text and FORMULA_PREFIX_REGEX.match(text):
        text = text.lstrip("=+-@ \t\r\n")
    return text.strip()


def normalize_column_name(col: str) -> str:
    """컬럼명 정규화 (공백 및 특수문자 소거 후 소문자 변환)"""
    return re.sub(r"[\s_\-()\[\]]", "", str(col or "")).lower()


# 컬럼 헤더 별칭 매핑 테이블
COLUMN_ALIASES = {
    "name": {"성명", "이름", "학생명", "학생성명", "name", "studentname", "student_name"},
    "grade": {"학년", "grade", "targetgrade"},
    "class_no": {"반", "학급", "class", "classno", "class_no", "classroom"},
    "student_no": {"번호", "출석번호", "number", "studentno", "student_no", "no"},
    "student_number": {"학번", "studentnumber", "student_number", "studentid", "student_id"},
    "email": {"이메일", "전자우편", "email", "mail", "studentemail", "student_email"},
}


def find_column_mapping(headers: List[str]) -> Dict[str, int]:
    """헤더 행에서 필드 인덱스를 매핑합니다."""
    mapping: Dict[str, int] = {}
    for idx, header in enumerate(headers):
        if not header:
            continue
        norm = normalize_column_name(header)
        for field, aliases in COLUMN_ALIASES.items():
            if norm in aliases and field not in mapping:
                mapping[field] = idx
                break
    return mapping


def parse_student_number_field(val: Any) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    4자리(또는 5자리) 학번에서 학년, 반, 번호 자동 분리
    예: 1101 -> (1, 1, 1)
        2315 -> (2, 3, 15)
        30412 -> (3, 4, 12)
    """
    s_val = re.sub(r"[^\d]", "", str(val or "").strip())
    if len(s_val) == 4:
        try:
            grade = int(s_val[0])
            class_no = int(s_val[1])
            student_no = int(s_val[2:])
            return grade, class_no, student_no
        except ValueError:
            pass
    elif len(s_val) == 5:
        try:
            grade = int(s_val[0])
            class_no = int(s_val[1:3])
            student_no = int(s_val[3:])
            return grade, class_no, student_no
        except ValueError:
            pass
    return None, None, None


def parse_csv_content(file_bytes: bytes) -> List[List[Any]]:
    """CSV 바이트 데이터를 다중 인코딩 시도를 통해 파싱합니다."""
    encodings = ["utf-8-sig", "utf-8", "cp949", "euc-kr"]
    text_content = None

    for enc in encodings:
        try:
            text_content = file_bytes.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue

    if text_content is None:
        raise ValueError("CSV 인코딩을 인식할 수 없습니다. (지원: UTF-8, CP949, EUC-KR)")

    # CSV reader로 파싱
    stream = io.StringIO(text_content)
    reader = csv.reader(stream)
    rows: List[List[Any]] = []
    for r in reader:
        # 빈 행 스킵
        if any(cell.strip() for cell in r if isinstance(cell, str)):
            rows.append(r)
    return rows


def parse_xlsx_content(file_bytes: bytes) -> List[List[Any]]:
    """XLSX 바이트 데이터를 openpyxl로 파싱합니다."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as e:
        raise ValueError(f"XLSX 엑셀 파일을 읽을 수 없습니다: {e}")

    ws = wb.active
    if not ws:
        raise ValueError("엑셀 시트가 비어있습니다.")

    rows: List[List[Any]] = []
    for row in ws.iter_rows(values_only=True):
        if any(cell is not None and str(cell).strip() != "" for cell in row):
            rows.append(list(row))
    return rows


def parse_student_batch_file(
    file_bytes: bytes,
    filename: str,
    default_email_domain: str = "bssm.hs.kr",
    max_rows: int = 1000,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    XLSX 또는 CSV 파일을 분석하여 학생 목록과 행별 검증 에러 목록을 반환합니다.

    반환값:
    - valid_records: 정상 파싱된 학생 정보 리스트
    - errors: 유효성 검증 실패 행 리스트 [{"row": int, "name": str, "reason": str}]
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "csv":
        raw_rows = parse_csv_content(file_bytes)
    elif ext in ("xlsx", "xls"):
        raw_rows = parse_xlsx_content(file_bytes)
    else:
        raise ValueError(f"지원하지 않는 파일 형식입니다 (.{ext}). XLSX 또는 CSV 파일만 업로드 가능합니다.")

    if not raw_rows:
        raise ValueError("파일에 유효한 데이터 행이 존재하지 않습니다.")

    if len(raw_rows) > max_rows:
        raise ValueError(f"한 번에 최대 {max_rows}행까지만 일괄 등록 가능합니다 (현재: {len(raw_rows)}행).")

    # 1. 헤더 행 탐색 (첫 5개 행 내에서 헤더 검색)
    header_idx = -1
    col_map: Dict[str, int] = {}

    for i in range(min(5, len(raw_rows))):
        current_headers = [str(c or "") for c in raw_rows[i]]
        mapping = find_column_mapping(current_headers)
        # 필수 요소: 'name'이 존재하고, 'grade'/'class_no'/'student_no' 또는 'student_number'가 존재해야 함
        if "name" in mapping and (("grade" in mapping and "class_no" in mapping) or "student_number" in mapping):
            header_idx = i
            col_map = mapping
            break

    # 헤더를 못 찾았을 경우, 기본 열 순서 가정 [학년, 반, 번호, 성명, 이메일]
    if header_idx == -1:
        if len(raw_rows[0]) >= 4:
            header_idx = -1  # 0번째 행부터 바로 데이터로 처리
            col_map = {"grade": 0, "class_no": 1, "student_no": 2, "name": 3}
            if len(raw_rows[0]) >= 5:
                col_map["email"] = 4
        else:
            raise ValueError(
                "엑셀 헤더를 인식할 수 없습니다. '성명', '학년', '반', '번호'(또는 '학번') 컬럼이 필요합니다."
            )

    valid_records: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    seen_students: Set[Tuple[int, int, int]] = set()
    seen_emails: Set[str] = set()

    data_rows = raw_rows[header_idx + 1 :] if header_idx >= 0 else raw_rows

    for offset, row in enumerate(data_rows, start=header_idx + 2 if header_idx >= 0 else 1):
        try:
            # 이름 추출 및 검증
            name_idx = col_map.get("name")
            raw_name = row[name_idx] if name_idx is not None and name_idx < len(row) else None
            clean_name = sanitize_text(raw_name)

            if not clean_name:
                errors.append({"row": offset, "name": "", "reason": "학생 성명이 누락되었습니다."})
                continue

            if len(clean_name) > 20:
                errors.append({"row": offset, "name": clean_name, "reason": "학생 성명은 20자를 초과할 수 없습니다."})
                continue

            name = html.escape(clean_name)

            # 학년, 반, 번호 추출
            grade: Optional[int] = None
            class_no: Optional[int] = None
            student_no: Optional[int] = None

            # 1) 학번 필드가 있을 경우 우선 시도
            if "student_number" in col_map:
                snum_idx = col_map["student_number"]
                if snum_idx < len(row) and row[snum_idx] is not None:
                    g, c, n = parse_student_number_field(row[snum_idx])
                    if g and c and n:
                        grade, class_no, student_no = g, c, n

            # 2) 개별 컬럼에서 보완
            if "grade" in col_map and col_map["grade"] < len(row) and row[col_map["grade"]] is not None:
                try:
                    grade = int(re.sub(r"[^\d]", "", str(row[col_map["grade"]])))
                except (ValueError, TypeError):
                    pass

            if "class_no" in col_map and col_map["class_no"] < len(row) and row[col_map["class_no"]] is not None:
                try:
                    class_no = int(re.sub(r"[^\d]", "", str(row[col_map["class_no"]])))
                except (ValueError, TypeError):
                    pass

            if "student_no" in col_map and col_map["student_no"] < len(row) and row[col_map["student_no"]] is not None:
                try:
                    student_no = int(re.sub(r"[^\d]", "", str(row[col_map["student_no"]])))
                except (ValueError, TypeError):
                    pass

            # 학년/반/번호 유효성 검사
            if grade is None or grade < 1 or grade > 3:
                errors.append({"row": offset, "name": name, "reason": f"유효하지 않은 학년입니다 ({grade}). 고등학교는 1~3학년이어야 합니다."})
                continue

            if class_no is None or class_no < 1 or class_no > 20:
                errors.append({"row": offset, "name": name, "reason": f"유효하지 않은 반 번호입니다 ({class_no})."})
                continue

            if student_no is None or student_no < 1 or student_no > 50:
                errors.append({"row": offset, "name": name, "reason": f"유효하지 않은 학생 출석번호입니다 ({student_no})."})
                continue

            # 이메일 추출 및 검증 (누락 시 학교 규격 이메일 자동 생성)
            email: Optional[str] = None
            if "email" in col_map and col_map["email"] < len(row) and row[col_map["email"]] is not None:
                raw_email = str(row[col_map["email"]]).strip().lower()
                if raw_email:
                    if not EMAIL_REGEX.match(raw_email):
                        errors.append({"row": offset, "name": name, "reason": f"올바르지 않은 이메일 형식입니다: {raw_email}"})
                        continue
                    email = raw_email

            if not email:
                # 부산소프트웨어마이스터고 기본 이메일 양식: s{grade}{class_no:02d}{student_no:02d}@bssm.hs.kr
                # 예: 1학년 1반 1번 -> s10101@bssm.hs.kr
                email = f"s{grade}{class_no:02d}{student_no:02d}@{default_email_domain}"

            # 파일 내 중복 검사
            st_key = (grade, class_no, student_no)
            if st_key in seen_students:
                errors.append({
                    "row": offset,
                    "name": name,
                    "reason": f"파일 내에 동일한 학년/반/번호({grade}학년 {class_no}반 {student_no}번)가 중복으로 존재합니다.",
                })
                continue
            seen_students.add(st_key)

            if email in seen_emails:
                errors.append({
                    "row": offset,
                    "name": name,
                    "reason": f"파일 내에 동일한 이메일({email})이 중복으로 존재합니다.",
                })
                continue
            seen_emails.add(email)

            valid_records.append({
                "row_number": offset,
                "name": name,
                "grade": grade,
                "class_no": class_no,
                "student_no": student_no,
                "email": email,
                "student_number": f"{grade}{class_no}{student_no:02d}",
            })

        except Exception as e:
            errors.append({"row": offset, "name": "", "reason": f"행 처리 중 예기치 않은 오류: {str(e)}"})

    return valid_records, errors
