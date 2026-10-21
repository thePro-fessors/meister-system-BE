"""
tests/test_redis_circuit_breaker.py - Redis 서킷 브레이커 및 스토리지 어댑터 검증 스위트
"""

import asyncio
import os
import time
import unittest

from core.circuit_breaker import CircuitBreaker, SafeRedisWrapper
from core.security import create_download_ticket, verify_and_consume_download_ticket
from core.storage import BaseStorageBackend, LocalStorageBackend, S3StorageBackend, get_storage_backend


class BrokenRedisClient:
    """항상 연결 오류 또는 타임아웃을 발생시키는 가상 Redis 클라이언트"""

    async def get(self, key):
        raise ConnectionError("Redis server connection refused")

    async def set(self, key, value, nx=False, ex=None):
        raise ConnectionError("Redis server connection refused")

    async def setex(self, key, time_sec, value):
        raise ConnectionError("Redis server connection refused")

    async def delete(self, *keys):
        raise ConnectionError("Redis server connection refused")


class TestRedisCircuitBreaker(unittest.IsolatedAsyncioTestCase):
    def test_01_circuit_breaker_state_transitions(self):
        """1. 서킷 브레이커 상태 전이 (CLOSED -> OPEN -> HALF_OPEN -> CLOSED) 검증"""
        cb = CircuitBreaker("test_breaker", failure_threshold=3, recovery_timeout_sec=0.1)
        self.assertEqual(cb.state, "CLOSED")
        self.assertTrue(cb.is_available())

        # 1~2회 실패 시 CLOSED 유지
        cb.record_failure(Exception("err1"))
        cb.record_failure(Exception("err2"))
        self.assertEqual(cb.state, "CLOSED")

        # 3회 연속 실패 시 OPEN 전이
        cb.record_failure(Exception("err3"))
        self.assertEqual(cb.state, "OPEN")
        self.assertFalse(cb.is_available())

        # 복구 타임아웃(0.1초) 대기
        time.sleep(0.12)
        # 타임아웃 경과 후 HALF_OPEN으로 시험 허용
        self.assertTrue(cb.is_available())
        self.assertEqual(cb.state, "HALF_OPEN")

        # 시험 호출 성공 시 CLOSED 복구
        cb.record_success()
        self.assertEqual(cb.state, "CLOSED")
        self.assertEqual(cb.failure_count, 0)

    async def test_02_safe_redis_wrapper_suppresses_crashes(self):
        """2. SafeRedisWrapper: Redis 장애 시 크래시 없이 None/False 안전 반환 및 서킷 OPEN 검증"""
        broken_redis = BrokenRedisClient()
        cb = CircuitBreaker("broken_test", failure_threshold=2)
        safe_client = SafeRedisWrapper(broken_redis, breaker=cb)

        # 1회차 실패
        res = await safe_client.get("test_key")
        self.assertIsNone(res)
        self.assertEqual(cb.failure_count, 1)

        # 2회차 실패 -> OPEN 전이
        res = await safe_client.set("test_key", "val")
        self.assertFalse(res)
        self.assertEqual(cb.state, "OPEN")

        # OPEN 상태에서는 실제 호출 없이 즉시 Fallback(None/False) 반환
        res = await safe_client.get("another_key")
        self.assertIsNone(res)

    async def test_03_download_ticket_fallback_when_redis_fails(self):
        """3. Redis 장애 시 다운로드 티켓 인메모리 Fallback 발급 및 단 1회 소비 검증"""
        broken_redis = BrokenRedisClient()
        safe_client = SafeRedisWrapper(broken_redis)

        # Redis 다운 상태에서 티켓 발급
        ticket_id = await create_download_ticket(
            user_uuid="test-uuid",
            role="student",
            file_path="submissions/sample.pdf",
            redis=safe_client,
            ttl_seconds=60,
        )
        self.assertTrue(bool(ticket_id))

        # 발급된 티켓으로 경로 일치 검증 (성공)
        result = await verify_and_consume_download_ticket(
            ticket_id=ticket_id,
            requested_file_path="submissions/sample.pdf",
            redis=safe_client,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["uuid"], "test-uuid")

        # 1회용 티켓이므로 재사용 시도 시 None 차단
        replay = await verify_and_consume_download_ticket(
            ticket_id=ticket_id,
            requested_file_path="submissions/sample.pdf",
            redis=safe_client,
        )
        self.assertIsNone(replay)

    def test_04_storage_backend_factory(self):
        """4. 스토리지 백엔드 팩토리 및 인터페이스 검증"""
        backend = get_storage_backend()
        self.assertIsInstance(backend, BaseStorageBackend)

        # S3 스토리지 백엔드 인스턴스화 검증
        s3_backend = S3StorageBackend(bucket_name="test-bucket")
        self.assertIsInstance(s3_backend, BaseStorageBackend)
        self.assertEqual(s3_backend.bucket, "test-bucket")
