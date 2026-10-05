"""
core/storage.py - 마이스터 시스템 파일 업로드 및 정적 저장소 유틸리티 모듈

명세 및 보안 요구사항:
- TODO.md 5.1 (파일 업로드 유틸리티 모듈: core/storage.py)
- SECURITY_AND_AUDIT.md (확장자 화이트리스트 검사, 50MB 용량 제한, UUID 난수화 경로 저장, 디렉터리 탐색 방어)
"""

import asyncio
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException, UploadFile, status

from sr_format import Error, SrFormat

# 허용된 업로드 파일 확장자 화이트리스트 (소문자 기준)
ALLOWED_EXTENSIONS: Set[str] = {
    # 이미지
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    # 영상
    ".mp4",
    ".mov",
    ".avi",
    ".webm",
    # 문서 및 압축
    ".pdf",
    ".zip",
}

# 최대 허용 파일 용량: 50MB (52,428,800 Bytes)
MAX_FILE_SIZE_BYTES: int = 50 * 1024 * 1024

# 스트리밍 청크 버퍼 크기: 64KB
CHUNK_SIZE_BYTES: int = 64 * 1024


def get_upload_base_dir() -> str:
    """기본 업로드 디렉터리의 절대 경로를 안전하게 반환합니다."""
    env_dir = os.getenv("UPLOAD_DIR")
    if env_dir:
        return os.path.abspath(env_dir)
    # 기본값: 프로젝트 루트 내 uploads/
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "uploads")


def get_file_extension(filename: Optional[str]) -> str:
    """파일명에서 정규화된 소문자 확장자를 추출합니다."""
    if not filename or "." not in filename:
        return ""
    _, ext = os.path.splitext(filename)
    return ext.lower().strip()


def sanitize_filename(filename: Optional[str]) -> str:
    """
    업로드 원본 파일명에서 경로 조작 문자열(..), 디렉터리 구분자 및 위험 문자를 안전하게 정제합니다.
    (TODO.md 5.1: 원본 파일명 보존 및 경로/헤더 인젝션 공격 차단)
    """
    if not filename:
        return "unnamed_file"

    # 1. 파일명만 추출 (Windows 백슬래시 및 POSIX 슬래시 분리)
    clean_name = filename.replace("\\", "/").rstrip("/").split("/")[-1]

    # 2. 널 바이트 및 개행 문자, 제어 문자 제거
    clean_name = clean_name.replace("\x00", "").replace("\r", "").replace("\n", "")

    # 3. 디렉터리 트래버설 문자열(..) 무력화
    clean_name = re.sub(r"\.\.+", ".", clean_name)

    # 4. 공백 및 특수문자 정제 (헤더 인젝션용 따옴표, 세미콜론 및 위험 특수문자 치환)
    clean_name = re.sub(r'[\/\\:\*\?"<>|;]', "_", clean_name)
    clean_name = clean_name.strip()

    # 빈 문자열 폴백
    if not clean_name or clean_name == ".":
        return "unnamed_file"

    # 길이 제한 (최대 200자)
    if len(clean_name) > 200:
        name_part, ext_part = os.path.splitext(clean_name)
        clean_name = name_part[: 200 - len(ext_part)] + ext_part

    return clean_name


def validate_file_extension(filename: Optional[str]) -> str:
    """
    파일 확장자가 화이트리스트에 부합하는지 검증합니다.
    검증 통과 시 소문자 정규화된 확장자를 반환합니다.
    (SECURITY_AND_AUDIT.md: 확장자 위변조 방어)
    """
    ext = get_file_extension(filename)
    if not ext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=SrFormat(
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                data=None,
                error=Error(
                    code="INVALID_FILE_TYPE",
                    message="파일 확장자가 누락되었거나 유효하지 않습니다.",
                ),
            ).model_dump(),
        )

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=SrFormat(
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                data=None,
                error=Error(
                    code="INVALID_FILE_TYPE",
                    message=f"허용되지 않은 파일 형식({ext})입니다. 허용 목록: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
                ),
            ).model_dump(),
        )

    return ext


def validate_file_metadata(file: UploadFile) -> Tuple[bool, Optional[str]]:
    """
    업로드 파일의 확장자 등 메타데이터 유효성을 검증합니다.
    (tests/test_storage.py 및 core/__init__.py 호환)
    """
    if not file or not file.filename:
        return False, "파일명이 누락되었습니다."
    ext = get_file_extension(file.filename)
    if not ext:
        return False, "확장자가 누락되었거나 유효하지 않습니다."
    if ext not in ALLOWED_EXTENSIONS:
        return False, f"허용되지 않은 파일 형식({ext})입니다."
    return True, None


# 파일 확장자별 매직 바이트(바이너리 시그니처) 매핑 테이블
# (SECURITY_AND_AUDIT.md 3.2: Content-Type 위조 및 바이너리 매직 넘버 검증)
MAGIC_BYTES_SIGNATURES: Dict[str, List[bytes]] = {
    ".jpg": [b"\xFF\xD8\xFF"],
    ".jpeg": [b"\xFF\xD8\xFF"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".gif": [b"GIF87a", b"GIF89a"],
    ".webp": [],  # RIFF 컨테이너 내 WEBP 청크 12바이트 필수 검증 (OPS-03)
    ".pdf": [b"%PDF-"],
    ".zip": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    # MP4 / MOV (ISO Base Media file: ....ftyp)
    ".mp4": [b"ftyp"],
    ".mov": [b"ftyp", b"moov", b"free", b"mdat", b"wide"],
    ".avi": [],  # RIFF 컨테이너 내 AVI 청크 12바이트 필수 검증 (OPS-03)
    ".webm": [b"\x1A\x45\xDF\xA3"],  # Matroska / WebM EBML
    ".heic": [b"ftypheic", b"ftypmif1", b"ftypmsf1", b"ftypheix", b"ftyphevc"],
    ".heif": [b"ftypheif", b"ftypmif1", b"ftypmsf1"],
}


def validate_magic_bytes(header_bytes: bytes, ext: str) -> bool:
    """
    파일의 첫 32바이트 바이너리 시그니처를 검사하여 위조된 확장자 여부를 판별합니다.
    (OPS-03: 컨테이너 포맷 세부 청크 우선 검증 적용)
    """
    if not header_bytes:
        return False

    # 1. RIFF 포맷 세부 검사 (WebP, AVI: 단순 RIFF 접두어 우회 원천 차단)
    if ext == ".webp":
        return len(header_bytes) >= 12 and header_bytes.startswith(b"RIFF") and header_bytes[8:12] == b"WEBP"
    if ext == ".avi":
        return len(header_bytes) >= 12 and header_bytes.startswith(b"RIFF") and header_bytes[8:12] == b"AVI "

    # 2. ISO Base Media (MP4/MOV/HEIC/HEIF) ftyp 박스 검사 (오프셋 4~12에 위치)
    if ext in (".mp4", ".mov", ".heic", ".heif") and len(header_bytes) >= 12:
        box_type = header_bytes[4:8]
        signatures = MAGIC_BYTES_SIGNATURES.get(ext, [])
        if box_type == b"ftyp":
            for sig in signatures:
                if sig in header_bytes[:32]:
                    return True
            return True

    # 3. 일반 접두사 매직 넘버 검사
    signatures = MAGIC_BYTES_SIGNATURES.get(ext)
    if not signatures:
        return True

    for sig in signatures:
        if header_bytes.startswith(sig):
            return True

    return False


def validate_file_signature(content: bytes, ext: str) -> bool:
    """
    validate_magic_bytes의 별칭 호환 함수.
    (tests/test_storage.py 및 core/__init__.py 호환)
    """
    return validate_magic_bytes(content, ext)


async def save_upload_file(
    file: UploadFile,
    subfolder: str = "submissions",
    max_size: Optional[int] = None,
    max_size_bytes: Optional[int] = None,
    base_upload_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    FastAPI `UploadFile`을 스트리밍 방식으로 읽어 50MB 용량 제한 및 바이너리 매직바이트를 검증하고,
    날짜 기반 서브디렉터리 및 UUID 파일명으로 디스크에 안전하게 저장합니다.

    보안 통제:
    1. 확장자 화이트리스트 검사 (`validate_file_extension`)
    2. 스트리밍 청크 누적 용량 검증 (50MB 초과 즉시 중단 및 임시파일 삭제)
    3. 첫 청크 바이너리 매직 넘버(File Signature) 위조 검증
    4. UUID4 난수화 파일명 저장으로 파일명 충돌 및 직접 경로 추측 방어
    5. 경로 디렉터리 트래버설 방어 및 정제된 원본 파일명 보존
    """
    if not file or not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=SrFormat(
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                data=None,
                error=Error(code="MISSING_FILE", message="업로드할 파일이 제공되지 않았습니다."),
            ).model_dump(),
        )

    # 1. 파일 확장자 검증
    ext = validate_file_extension(file.filename)

    # 2. 저장 경로 및 UUID 파일명 생성 (예: uploads/submissions/2026/10/a1b2c3d4... .pdf)
    upload_root = base_upload_dir or get_upload_base_dir()
    now = datetime.now()
    year_str = now.strftime("%Y")
    month_str = now.strftime("%m")

    # 서브폴더 인젝션 방어 (영숫자 및 밑줄만 허용)
    clean_subfolder = re.sub(r"[^a-zA-Z0-9_\-]", "", subfolder) or "submissions"
    target_dir = os.path.join(upload_root, clean_subfolder, year_str, month_str)

    os.makedirs(target_dir, exist_ok=True)

    stored_filename = f"{uuid.uuid4().hex}{ext}"
    disk_path = os.path.join(target_dir, stored_filename)

    # 웹 접근용 상대 URL 경로 (예: /uploads/submissions/2026/10/stored.pdf)
    rel_path = os.path.relpath(disk_path, upload_root).replace("\\", "/")
    web_path = f"/uploads/{rel_path}"

    # 3. 청크 단위 스트리밍 저장 및 용량/매직바이트 실시간 검증
    total_bytes = 0
    first_chunk = True
    effective_limit = max_size_bytes or max_size or MAX_FILE_SIZE_BYTES

    try:
        with open(disk_path, "wb") as buffer:
            while True:
                chunk = await file.read(CHUNK_SIZE_BYTES)
                if not chunk:
                    break

                # 첫 번째 청크에서 바이너리 매직 넘버 검증 (파일 변조 탐지)
                if first_chunk:
                    first_chunk = False
                    if not validate_magic_bytes(chunk[:32], ext):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=SrFormat(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                success=False,
                                data=None,
                                error=Error(
                                    code="INVALID_FILE_TYPE",
                                    message=f"파일 확장자({ext})와 실제 파일 시그니처(Magic Bytes)가 일치하지 않습니다.",
                                ),
                            ).model_dump(),
                        )

                total_bytes += len(chunk)
                if total_bytes > effective_limit:
                    limit_label = f"{effective_limit // (1024 * 1024)}MB" if effective_limit >= 1024 * 1024 else f"{effective_limit}B"
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=SrFormat(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            success=False,
                            data=None,
                            error=Error(
                                code="FILE_SIZE_EXCEEDED",
                                message=f"파일 용량이 제한({limit_label})을 초과했습니다.",
                            ),
                        ).model_dump(),
                    )
                # [성능 3.4] 동기 파일 쓰기로 인한 이벤트 루프 블로킹 방지
                await asyncio.to_thread(buffer.write, chunk)

        if total_bytes == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=SrFormat(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    success=False,
                    data=None,
                    error=Error(
                        code="INVALID_FILE_TYPE",
                        message="업로드된 파일의 내용이 비어 있습니다 (0 byte). 유효한 증빙 파일을 첨부해주세요.",
                    ),
                ).model_dump(),
            )
    except Exception:
        # 비정상 중단, 용량 초과 또는 빈 파일 감지 시 불완전 파일 즉시 제거
        if os.path.exists(disk_path):
            try:
                os.remove(disk_path)
            except OSError:
                pass
        raise
    finally:
        # 파일 포인터 원위치 복원
        await file.seek(0)

    original_filename = sanitize_filename(file.filename)

    return {
        "file_path": web_path,
        "disk_path": disk_path,
        "original_filename": original_filename,
        "file_size": total_bytes,
        "content_type": file.content_type,
    }


def delete_uploaded_file(disk_path: Optional[str]) -> bool:
    """디스크에 저장된 파일을 안전하게 삭제합니다."""
    if not disk_path:
        return False
    try:
        if os.path.exists(disk_path):
            os.remove(disk_path)
            return True
    except OSError:
        pass
    return False


def cleanup_old_file_on_resubmit(old_web_path: Optional[str], base_upload_dir: Optional[str] = None) -> bool:
    """
    증빙자료 재제출(Resubmit) 또는 대체 시, 기존 파일이 디스크에 고아 파일(Orphan File)로
    방치되지 않도록 안전하게 삭제합니다.
    (SECURITY_AND_AUDIT.md 4.1 권고 및 TODO.md 2.6)
    """
    if not old_web_path or not isinstance(old_web_path, str):
        return False

    upload_root = base_upload_dir or get_upload_base_dir()
    clean_rel = old_web_path.strip().lstrip("/\\")
    if clean_rel.startswith("uploads/"):
        clean_rel = clean_rel[8:]

    abs_path = os.path.abspath(os.path.join(upload_root, clean_rel))

    # 상위 경로 탐색 방어: upload_root 외부 파일 삭제 차단
    try:
        if os.path.commonpath([upload_root, abs_path]) != upload_root:
            return False
    except ValueError:
        return False

    if os.path.isfile(abs_path):
        return delete_uploaded_file(abs_path)
    return False


def _scan_and_clean_disk_orphans(
    upload_root: str,
    active_rel_paths: Set[str],
    max_age_seconds: int = 3600,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    디스크 상의 업로드 디렉터리를 순회하며 고아 파일을 찾아 정리하는 동기 작업 함수.
    (asyncio.to_thread 위임용)
    """
    now_ts = datetime.now().timestamp()
    scanned_count = 0
    orphan_count = 0
    freed_bytes = 0
    cleaned_files = []

    for root, _, files in os.walk(upload_root):
        for f in files:
            abs_disk_path = os.path.join(root, f)
            scanned_count += 1

            try:
                stat = os.stat(abs_disk_path)
            except OSError:
                continue

            # Grace period 검사: 최근 생성된 파일은 업로드 진행 중일 수 있으므로 보존
            if now_ts - stat.st_mtime < max_age_seconds:
                continue

            rel_from_root = os.path.relpath(abs_disk_path, upload_root).replace("\\", "/").strip().lstrip("/\\")
            if rel_from_root.startswith("uploads/"):
                rel_from_root = rel_from_root[8:]
            rel_norm = os.path.normpath(rel_from_root).replace("\\", "/")
            web_path = f"/uploads/{rel_norm}"

            # DB에 등록되어 있지 않은 파일인 경우 고아 파일로 판정
            if rel_norm not in active_rel_paths:
                orphan_count += 1
                freed_bytes += stat.st_size
                cleaned_files.append(web_path)

                if not dry_run:
                    delete_uploaded_file(abs_disk_path)

    # 3. 비어 있는 디렉터리 후속 정리 (역방향 순회)
    if not dry_run:
        for root, dirs, files in os.walk(upload_root, topdown=False):
            if root != upload_root and not os.listdir(root):
                try:
                    os.rmdir(root)
                except OSError:
                    pass

    return {
        "scanned": scanned_count,
        "orphans_cleaned": orphan_count,
        "bytes_freed": freed_bytes,
        "cleaned_files": cleaned_files,
        "dry_run": dry_run,
    }


async def find_and_clean_orphan_files(
    conn: Any,
    base_upload_dir: Optional[str] = None,
    max_age_seconds: int = 3600,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    데이터베이스(submissions)와 디스크 저장소를 전수 대조하여
    참조가 끊긴 고아 파일(Orphan Files)을 백그라운드에서 안전하게 정리(GC)합니다.

    안전 제어:
    - max_age_seconds (기본 1시간): 현재 업로드 진행 중인 파일의 오삭제를 방지하는 Grace Period
    - dry_run: 실제 파일 삭제 없이 고아 파일 목록과 예상 회수 용량만 사전 점검
    - 디렉터리 탐색 및 DB 트랜잭션과 격리된 비동기 스레드 I/O (asyncio.to_thread)
    """
    upload_root = base_upload_dir or get_upload_base_dir()
    if not os.path.exists(upload_root):
        return {"scanned": 0, "orphans_cleaned": 0, "bytes_freed": 0, "dry_run": dry_run}

    # 1. 활성 증빙자료의 파일 경로 수집
    active_rel_paths: Set[str] = set()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT file_path
                FROM submissions
                WHERE file_path IS NOT NULL AND file_path != '' AND is_deleted = FALSE
                """
            )
            rows = await cur.fetchall()
            for r in rows:
                p = r[0] if isinstance(r, (list, tuple)) else r.get("file_path")
                if p:
                    clean_p = p.strip().lstrip("/\\").replace("\\", "/")
                    if clean_p.startswith("uploads/"):
                        clean_p = clean_p[8:]
                    clean_norm = os.path.normpath(clean_p).replace("\\", "/")
                    active_rel_paths.add(clean_norm)
    except Exception as e:
        return {"error": f"DB 조회 실패: {e}", "orphans_cleaned": 0}

    # 2. 디스크 상의 업로드 디렉터리 순회 (asyncio.to_thread로 이벤트 루프 블로킹 방지)
    result = await asyncio.to_thread(
        _scan_and_clean_disk_orphans,
        upload_root=upload_root,
        active_rel_paths=active_rel_paths,
        max_age_seconds=max_age_seconds,
        dry_run=dry_run,
    )
    return result
