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
from typing import Any, Dict, Optional, Set, Tuple

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
    return os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads"))


def get_file_extension(filename: Optional[str]) -> str:
    """파일명에서 소문자 확장자를 안전하게 추출합니다."""
    if not filename:
        return ""
    # Windows 경로 구분자(\)를 표준(/)으로 변환 후 파일명만 추출
    normalized = filename.replace("\\", "/")
    clean_name = os.path.basename(normalized).strip().rstrip(".")
    _, ext = os.path.splitext(clean_name)
    return ext.lower()


def sanitize_filename(filename: Optional[str]) -> str:
    """원본 파일명에서 경로 탐색 문자, 널 바이트, 제어문자, 따옴표를 정제하여 헤더 인젝션을 방어합니다.
    (SECURITY_AND_AUDIT.md 3.6: Content-Disposition 및 HTTP Response Splitting 방어)
    """
    if not filename:
        return "unnamed_file"
    # Windows 경로 구분자(\) 및 POSIX 구분자(/) 모두 대응
    normalized = filename.replace("\\", "/")
    # 디렉터리 경로 분리 후 순수 파일명만 취득
    clean_name = os.path.basename(normalized)
    # 널 바이트, 제어 문자, 줄바꿈, 따옴표/세미콜론 제거
    clean_name = re.sub(r'[\r\n\x00-\x1f\x7f-\x9f"\'\\;]+', '', clean_name).strip()
    return clean_name[:255] if clean_name else "unnamed_file"


def validate_file_signature(header: bytes, ext: str) -> bool:
    """파일의 첫 바이트(Magic Bytes)와 확장자의 일치 여부를 검증합니다.
    (SECURITY_AND_AUDIT.md 3.3: 확장자 위조, WebShell 및 Polyglot 파일 공격 원천 방어)
    """
    if not header or not ext:
        return False

    ext_lower = ext.lower()

    # 1. PDF
    if ext_lower == ".pdf":
        return header.startswith(b"%PDF-")

    # 2. 이미지 (JPEG, PNG, GIF, WebP, HEIC/HEIF)
    if ext_lower in (".jpg", ".jpeg"):
        return header.startswith(b"\xff\xd8\xff")
    if ext_lower == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if ext_lower == ".gif":
        return header.startswith(b"GIF87a") or header.startswith(b"GIF89a")
    if ext_lower == ".webp":
        return len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    if ext_lower in (".heic", ".heif"):
        return len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12].lower() in (b"heic", b"heix", b"hevc", b"mif1", b"msf1")

    # 3. 압축 (ZIP)
    if ext_lower == ".zip":
        return header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06") or header.startswith(b"PK\x07\x08")

    # 4. 비디오 (MP4, MOV, AVI, WebM)
    if ext_lower in (".mp4", ".mov"):
        return len(header) >= 8 and (header[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"skip") or b"ftyp" in header[:16])
    if ext_lower == ".avi":
        return len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"AVI "
    if ext_lower == ".webm":
        return header.startswith(b"\x1a\x45\xdf\xa3")

    return False


def validate_file_metadata(file: UploadFile) -> Tuple[bool, Optional[str]]:
    """
    업로드된 파일의 기본 메타데이터(확장자 유효성 등)를 사전 검증합니다.
    """
    if not file or not file.filename:
        return False, "업로드할 파일이 지정되지 않았습니다."

    ext = get_file_extension(file.filename)
    if not ext or ext not in ALLOWED_EXTENSIONS:
        return False, f"허용되지 않는 파일 형식({ext or '확장자 없음'})입니다. (jpg, jpeg, png, gif, webp, mp4, mov, avi, webm, pdf, zip만 허용)"

    return True, None


async def save_upload_file(
    file: UploadFile,
    subfolder: str = "submissions",
    base_upload_dir: Optional[str] = None,
    max_size_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    UploadFile을 청크 스트리밍 방식으로 디스크에 안전하게 저장합니다.

    보안 및 안정성 보장:
    1. 확장자 화이트리스트 사전 검증 (위반 시 INVALID_FILE_TYPE 400 반환)
    2. 매직 넘버(File Signature) 검증으로 파일 확장자 위조 및 웹쉘 방어
    3. 64KB 청크 단위 스트리밍 저장 중 실시간 용량 누적 체크 (50MB 초과 시 FILE_SIZE_EXCEEDED 400)
    4. 비동기 이벤트 루프 블로킹 방지를 위한 asyncio.to_thread 파일 I/O
    5. 년/월/UUID 기반 파일명 해싱 난수화 경로 생성 (경로 탐색 및 덮어쓰기 공격 원천 차단)
    6. 예외 발생 또는 용량 초과 시 불완전한 임시 파일 자동 클린업 (Unlink)
    """
    effective_limit = max_size_bytes if max_size_bytes is not None else MAX_FILE_SIZE_BYTES

    is_valid, err_msg = validate_file_metadata(file)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=SrFormat(
                status_code=status.HTTP_400_BAD_REQUEST,
                success=False,
                data=None,
                error=Error(
                    code="INVALID_FILE_TYPE",
                    message=err_msg or "허용되지 않는 파일 형식입니다.",
                ),
            ).model_dump(),
        )

    upload_root = base_upload_dir or get_upload_base_dir()
    now = datetime.now()
    year_str = f"{now.year:04d}"
    month_str = f"{now.month:02d}"

    target_dir = os.path.join(upload_root, subfolder, year_str, month_str)
    os.makedirs(target_dir, exist_ok=True)

    ext = get_file_extension(file.filename)
    unique_filename = f"{uuid.uuid4().hex}{ext}"
    disk_path = os.path.join(target_dir, unique_filename)
    web_path = f"/uploads/{subfolder}/{year_str}/{month_str}/{unique_filename}"

    total_bytes = 0
    is_first_chunk = True
    try:
        with open(disk_path, "wb") as buffer:
            while True:
                chunk = await file.read(CHUNK_SIZE_BYTES)
                if not chunk:
                    break

                # [보안 3.3] 첫 청크 매직 넘버(File Signature) 검증
                if is_first_chunk:
                    is_first_chunk = False
                    if not validate_file_signature(chunk[:32], ext):
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
    - 디렉터리 탐색 및 DB 트랜잭션과 격리된 비동기 I/O
    """
    upload_root = base_upload_dir or get_upload_base_dir()
    if not os.path.exists(upload_root):
        return {"scanned": 0, "orphans_cleaned": 0, "bytes_freed": 0, "dry_run": dry_run}

    # 1. 활성 증빙자료의 파일 경로 수집
    active_rel_paths = set()
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

    # 2. 디스크 상의 업로드 디렉터리 순회
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
