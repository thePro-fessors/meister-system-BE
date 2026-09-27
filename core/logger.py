import logging
import json
import re
import socket
from logging.handlers import DatagramHandler
from datetime import datetime
from typing import Any

# UDP 설정 (추후 WebSocket 브릿지 / 터미널 모니터 연동용)
UDP_HOST = "127.0.0.1"
UDP_PORT = 5140

# 마스킹 대상 민감 필드 키워드 (대소문자 무관)
SENSITIVE_KEYS = {
    "password", "passwd", "secret", "token", "access_token",
    "register_token", "registertoken", "authorization", "bearer"
}

# 로그 메시지 본문(message) 내에 비밀번호 JSON/정규식이 포함될 경우를 대비한 마스킹 패턴
SENSITIVE_PATTERN = re.compile(
    r'(?i)(password|secret|token|register_token|registerToken)["\']?\s*[:=]\s*["\']?([^"\'\s,}{]+)["\']?'
)


def mask_sensitive_data(data: Any) -> Any:
    """딕셔너리, 리스트 등 구조화된 데이터 내의 비밀번호, 토큰 등 민감 정보를 재귀적으로 마스킹합니다."""
    if isinstance(data, dict):
        masked_dict = {}
        for key, value in data.items():
            if str(key).lower() in SENSITIVE_KEYS:
                masked_dict[key] = "******"
            else:
                masked_dict[key] = mask_sensitive_data(value)
        return masked_dict
    elif isinstance(data, list):
        return [mask_sensitive_data(item) for item in data]
    return data


def mask_sensitive_message(message: str) -> str:
    """텍스트 메시지 본문에 노출된 민감 정보 패턴(password=..., "password": "...")을 마스킹합니다."""
    return SENSITIVE_PATTERN.sub(r'\1: "******"', message)


class SafeJsonDatagramHandler(DatagramHandler):
    """로그 레코드를 JSON으로 직렬화하여 로컬 UDP 소켓으로 전송하는 핸들러.
    
    민감 정보(비밀번호, 토큰) 마스킹 필터를 내장하여 네트워크 패킷 스니핑 위험을 원천 차단합니다.
    모니터링 프로세스가 꺼져 있거나 수신을 못해도 메인 서버에 전혀 영향을 주지 않습니다.
    """

    def makePickle(self, record: logging.LogRecord) -> bytes:
        raw_message = record.getMessage()
        safe_message = mask_sensitive_message(raw_message)

        # extra_data 내 민감정보 재귀적 마스킹 (단, OTP 테스트 로그의 otp 코드는 모니터링 편의를 위해 보존)
        extra = getattr(record, "extra_data", None)
        if extra:
            extra = mask_sensitive_data(extra)

        log_payload = {
            "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "message": safe_message,
            "module": record.module,
            "pathname": record.pathname,
            "lineno": record.lineno,
            "extra": extra,
        }
        return (json.dumps(log_payload, ensure_ascii=False) + "\n").encode("utf-8")


def setup_logger(name: str = "meister") -> logging.Logger:
    """기본 콘솔 출력과 UDP 브로드캐스팅이 결합된 로거 생성"""
    app_logger = logging.getLogger(name)
    app_logger.setLevel(logging.INFO)

    # 중복 핸들러 등록 방지
    if not app_logger.handlers:
        # 1. 표준 콘솔 출력 (평문 로그 포맷)
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%H:%M:%S"
        )
        console_handler.setFormatter(console_formatter)
        app_logger.addHandler(console_handler)

        # 2. UDP 브로드캐스팅 핸들러 (Fire-and-Forget & 마스킹 보안 적용)
        try:
            udp_handler = SafeJsonDatagramHandler(UDP_HOST, UDP_PORT)
            app_logger.addHandler(udp_handler)
        except Exception:
            pass  # 소켓 바인딩 실패 시 조용히 무시

    return app_logger


logger = setup_logger("meister")


def log_otp(to_email: str, otp_code: str) -> None:
    """개발 모드 OTP 발송 로그를 규격화하여 전송"""
    logger.info(
        f"[OTP] {to_email} -> {otp_code} (유효시간: 5분)",
        extra={"extra_data": {"type": "OTP", "email": to_email, "otp": otp_code}}
    )
