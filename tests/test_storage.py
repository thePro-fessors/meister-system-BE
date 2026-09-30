"""
tests/test_storage.py - core/storage.py 단위 테스트 모듈
"""

import io
import os
import shutil
import tempfile
import unittest

from fastapi import HTTPException, UploadFile

from core.storage import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    delete_uploaded_file,
    get_file_extension,
    sanitize_filename,
    save_upload_file,
    validate_file_metadata,
)


class TestStorage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_upload_dir = tempfile.mkdtemp(prefix="meister_test_uploads_")

    def tearDown(self):
        if os.path.exists(self.test_upload_dir):
            shutil.rmtree(self.test_upload_dir)

    def test_extension_extraction(self):
        self.assertEqual(get_file_extension("test.PDF"), ".pdf")
        self.assertEqual(get_file_extension("document.tar.gz"), ".gz")
        self.assertEqual(get_file_extension("no_ext"), "")
        self.assertEqual(get_file_extension(None), "")

    def test_filename_sanitization(self):
        self.assertEqual(sanitize_filename("../../etc/passwd.pdf"), "passwd.pdf")
        self.assertEqual(sanitize_filename(r"C:\Users\Student\Desktop\cert.pdf"), "cert.pdf")
        self.assertEqual(sanitize_filename("test\x00_null.png"), "test_null.png")
        self.assertEqual(sanitize_filename(""), "unnamed_file")
        self.assertEqual(sanitize_filename(None), "unnamed_file")


    def test_validate_file_metadata_allowed_types(self):
        for ext in ALLOWED_EXTENSIONS:
            file = UploadFile(filename=f"file{ext}", file=io.BytesIO(b"dummy"))
            is_valid, err = validate_file_metadata(file)
            self.assertTrue(is_valid, f"{ext} should be allowed")
            self.assertIsNone(err)

    def test_validate_file_metadata_disallowed_types(self):
        disallowed = [".exe", ".sh", ".php", ".js", ".py", ".bat", ""]
        for ext in disallowed:
            file = UploadFile(filename=f"malicious{ext}", file=io.BytesIO(b"echo 1"))
            is_valid, err = validate_file_metadata(file)
            self.assertFalse(is_valid, f"{ext} should NOT be allowed")
            self.assertIsNotNone(err)

    async def test_save_upload_file_success(self):
        content = b"%PDF-1.4 dummy content for certificate"
        file = UploadFile(filename="자격증_사본.pdf", file=io.BytesIO(content))
        result = await save_upload_file(
            file=file,
            subfolder="test_sub",
            base_upload_dir=self.test_upload_dir,
        )

        self.assertTrue(result["file_path"].startswith("/uploads/test_sub/"))
        self.assertTrue(result["file_path"].endswith(".pdf"))
        self.assertEqual(result["original_filename"], "자격증_사본.pdf")
        self.assertEqual(result["file_size"], len(content))
        self.assertTrue(os.path.exists(result["disk_path"]))

        with open(result["disk_path"], "rb") as f:
            disk_content = f.read()
        self.assertEqual(disk_content, content)

    async def test_save_upload_file_oversized_exceeded(self):
        # 100 바이트 용량 제한 테스트 (유효한 ZIP 헤더 포함)
        file = UploadFile(filename="huge.zip", file=io.BytesIO(b"PK\x03\x04" + b"A" * 200))
        with self.assertRaises(HTTPException) as ctx:
            await save_upload_file(
                file=file,
                subfolder="test_sub",
                base_upload_dir=self.test_upload_dir,
                max_size_bytes=100,
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail["error"]["code"], "FILE_SIZE_EXCEEDED")

        # 용량 초과 시 디스크에 부분 저장된 파일이 즉시 삭제되었는지 확인
        all_files = []
        for root, _, files in os.walk(self.test_upload_dir):
            all_files.extend(files)
        self.assertEqual(len(all_files), 0, "Partial file must be unlinked on size exceeded")

    async def test_save_upload_file_empty_file_rejected(self):
        # 0 바이트 빈 파일 업로드 시 INVALID_FILE_TYPE 거부 및 디스크 삭제 검증
        file = UploadFile(filename="empty.pdf", file=io.BytesIO(b""))
        with self.assertRaises(HTTPException) as ctx:
            await save_upload_file(
                file=file,
                subfolder="test_sub",
                base_upload_dir=self.test_upload_dir,
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail["error"]["code"], "INVALID_FILE_TYPE")
        self.assertIn("0 byte", ctx.exception.detail["error"]["message"])

        # 빈 파일이 디스크에 남아있지 않고 삭제되었는지 확인
        all_files = []
        for root, _, files in os.walk(self.test_upload_dir):
            all_files.extend(files)
        self.assertEqual(len(all_files), 0, "Empty file must not remain on disk")


    def test_delete_uploaded_file(self):
        test_file = os.path.join(self.test_upload_dir, "to_delete.txt")
        with open(test_file, "w") as f:
            f.write("hello")
        self.assertTrue(os.path.exists(test_file))

        res = delete_uploaded_file(test_file)
        self.assertTrue(res)
        self.assertFalse(os.path.exists(test_file))

        # 이미 없는 파일 삭제 시 False 반환 (예외 발생 안 함)
        res_none = delete_uploaded_file(test_file)
        self.assertFalse(res_none)

    def test_file_signature_validation(self):
        from core.storage import validate_file_signature
        self.assertTrue(validate_file_signature(b"%PDF-1.4...", ".pdf"))
        self.assertTrue(validate_file_signature(b"\xff\xd8\xff...", ".jpg"))
        self.assertTrue(validate_file_signature(b"\x89PNG\r\n\x1a\n...", ".png"))
        self.assertTrue(validate_file_signature(b"PK\x03\x04...", ".zip"))
        # 확장자 위조 방어: PHP 스크립트가 .pdf로 위장한 경우 거부
        self.assertFalse(validate_file_signature(b"<?php echo 'shell'; ?>", ".pdf"))
        # 확장자 위조 방어: 실행 파일(EXE)이 .png로 위장한 경우 거부
        self.assertFalse(validate_file_signature(b"MZ\x90\x00\x03\x00\x00\x00", ".png"))

    def test_sanitize_filename_header_injection(self):
        # 줄바꿈 및 따옴표를 통한 Content-Disposition 헤더 인젝션 방어
        malicious = 'malicious\r\nSet-Cookie: pwned=1\r\n"; filename="owned.pdf'
        cleaned = sanitize_filename(malicious)
        self.assertNotIn("\r", cleaned)
        self.assertNotIn("\n", cleaned)
        self.assertNotIn('"', cleaned)
        self.assertNotIn(';', cleaned)


if __name__ == "__main__":
    unittest.main()
