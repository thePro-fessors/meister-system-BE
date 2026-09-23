import logging
import json
import socket
from logging.handlers import DatagramHandler
from datetime import datetime

# UDP 설정 (추후 WebSocket 브릿지 / 터미널 모니터 연동용)
UDP_HOST = "127.0.0.1"
UDP_PORT = 5140


class SafeJsonDatagramHandler(DatagramHandler):
    """로그 레코드를 JSON으로 직렬화하여 로컬 UDP 소켓으로 전송하는 핸들러.
    
    모니터링 프로세스가 꺼져 있거나 수신을 못해도 메인 서버에 전혀 영향을 주지 않습니다.
    """

    def makePickle(self, record: logging.LogRecord) -> bytes:
        log_payload = {
            "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "pathname": record.pathname,
            "lineno": record.lineno,
            "extra": getattr(record, "extra_data", None),
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

        # 2. UDP 브로드캐스팅 핸들러 (Fire-and-Forget)
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
