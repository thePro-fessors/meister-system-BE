"""
core/circuit_breaker.py - Redis 및 외부 종속성 고가용성 서킷 브레이커 (Circuit Breaker)
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("meister.circuit_breaker")


class CircuitBreakerOpenException(Exception):
    """서킷 브레이커가 열려있어(OPEN) 요청이 차단되었음을 나타내는 예외"""
    pass


class CircuitBreaker:
    """
    서킷 브레이커 상태 머신
    - CLOSED: 정상 상태. 모든 호출 허용. 연속 실패 임계치 도달 시 OPEN 전이.
    - OPEN: 장애 상태. 모든 호출 즉시 차단(Fallback 실행). 복구 대기 시간 경과 시 HALF_OPEN 전이.
    - HALF_OPEN: 시험 상태. 제한된 시험 호출 허용. 성공 시 CLOSED 복구, 실패 시 즉시 OPEN 재전이.
    """

    def __init__(
        self,
        name: str = "redis_breaker",
        failure_threshold: int = 3,
        recovery_timeout_sec: float = 30.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_sec = recovery_timeout_sec

        self.state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.failure_count: int = 0
        self.last_state_change: float = time.time()
        self.last_failure_time: float = 0.0

    def record_success(self) -> None:
        if self.state in ("HALF_OPEN", "OPEN"):
            logger.info(f"[{self.name}] 서킷 브레이커가 복구되었습니다: {self.state} -> CLOSED")
        self.state = "CLOSED"
        self.failure_count = 0
        self.last_state_change = time.time()

    def record_failure(self, error: Optional[Exception] = None) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        logger.warning(
            f"[{self.name}] 호출 실패 ({self.failure_count}/{self.failure_threshold}): {error}"
        )

        if self.failure_count >= self.failure_threshold:
            if self.state != "OPEN":
                logger.error(
                    f"[{self.name}] 연속 실패 임계치 도달. 서킷 브레이커 OPEN 전이 (Fallback 활성화)"
                )
            self.state = "OPEN"
            self.last_state_change = time.time()

    def is_available(self) -> bool:
        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":
            now = time.time()
            if now - self.last_state_change >= self.recovery_timeout_sec:
                logger.info(f"[{self.name}] 복구 타임아웃 경과. 서킷 브레이커 HALF_OPEN 시험 상태 전이")
                self.state = "HALF_OPEN"
                self.last_state_change = now
                return True
            return False

        # HALF_OPEN 상태
        return True


class SafeRedisWrapper:
    """
    서킷 브레이커가 적용된 안전한 Redis 클라이언트 래퍼
    - Redis 장애 또는 타임아웃 시 예외로 인한 전체 서비스 중단을 방어하고
      호출자에게 None 또는 False를 안전하게 반환하여 인메모리/DB Fallback으로 유도합니다.
    """

    def __init__(self, raw_redis: Any, breaker: Optional[CircuitBreaker] = None):
        self._raw_redis = raw_redis
        self.breaker = breaker or CircuitBreaker("redis_breaker")

    async def get(self, key: str) -> Optional[str]:
        if not self.breaker.is_available():
            return None
        try:
            val = await asyncio.wait_for(self._raw_redis.get(key), timeout=2.0)
            self.breaker.record_success()
            return val
        except Exception as e:
            self.breaker.record_failure(e)
            return None

    async def set(self, key: str, value: Any, nx: bool = False, ex: Optional[int] = None) -> bool:
        if not self.breaker.is_available():
            return False
        try:
            res = await asyncio.wait_for(self._raw_redis.set(key, value, nx=nx, ex=ex), timeout=2.0)
            self.breaker.record_success()
            return bool(res)
        except Exception as e:
            self.breaker.record_failure(e)
            return False

    async def setex(self, key: str, time_sec: int, value: Any) -> bool:
        if not self.breaker.is_available():
            return False
        try:
            await asyncio.wait_for(self._raw_redis.setex(key, time_sec, value), timeout=2.0)
            self.breaker.record_success()
            return True
        except Exception as e:
            self.breaker.record_failure(e)
            return False

    async def delete(self, *keys: str) -> int:
        if not self.breaker.is_available():
            return 0
        try:
            cnt = await asyncio.wait_for(self._raw_redis.delete(*keys), timeout=2.0)
            self.breaker.record_success()
            return cnt or 0
        except Exception as e:
            self.breaker.record_failure(e)
            return 0

    async def ttl(self, key: str) -> int:
        if not self.breaker.is_available():
            return -1
        try:
            val = await asyncio.wait_for(self._raw_redis.ttl(key), timeout=2.0)
            self.breaker.record_success()
            return val
        except Exception as e:
            self.breaker.record_failure(e)
            return -1

    async def incr(self, key: str) -> Optional[int]:
        if not self.breaker.is_available():
            return None
        try:
            val = await asyncio.wait_for(self._raw_redis.incr(key), timeout=2.0)
            self.breaker.record_success()
            return val
        except Exception as e:
            self.breaker.record_failure(e)
            return None

    async def expire(self, key: str, time_sec: int) -> bool:
        if not self.breaker.is_available():
            return False
        try:
            res = await asyncio.wait_for(self._raw_redis.expire(key, time_sec), timeout=2.0)
            self.breaker.record_success()
            return bool(res)
        except Exception as e:
            self.breaker.record_failure(e)
            return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw_redis, name)


redis_circuit_breaker = CircuitBreaker("redis_service")
