"""
core/security.py - JWT 인증, 토큰 수명 주기 관리 및 역할 기반 접근 제어(RBAC) 보안 모듈

주요 기능:
1. JWT Access Token 발급 및 무차별 대입 방지용 JTI(UUID) 삽입
2. HTTP Bearer 인증 헤더 파싱 및 서명/만료시간 검증
3. Redis O(1) 블랙리스트 조회를 통한 즉시 로그아웃 세션 무효화
4. 선언적 RBAC(Role-Based Access Control) 의존성 팩토리 (`require_roles`, `require_student` 등)
5. 리버스 프록시(Nginx/Docker/Cloudflare) 환경 대응 클라이언트 IP 추출 유틸리티 (`get_client_ip`)
"""

import ipaddress
import json
import os
import secrets
import sys
import uuid

# 로컬 개발 환경에서 가상환경 미활성화 상태로 python3 직접 호출 시 로컬 .venv 자동 탐색 (Docker/운영 배포 시에는 미동작)
if "VIRTUAL_ENV" not in os.environ:
    _base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _venv_root = os.path.join(_base_dir, ".venv")
    if os.path.isdir(_venv_root):
        import glob
        for _sp in glob.glob(os.path.join(_venv_root, "lib", "python*", "site-packages")):
            if _sp not in sys.path:
                sys.path.append(_sp)

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Dict, Optional, Sequence

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import redis.asyncio as aioredis
from dotenv import load_dotenv

from database import get_redis
from sr_format import Error, SrFormat

load_dotenv()

# ==============================================================================
# 환경 변수 및 보안 키 설정 (Fail-Fast)
# ==============================================================================
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not JWT_SECRET_KEY:
    raise RuntimeError("ALERT: JWT_SECRET_KEY 환경 변수가 설정되지 않았습니다.")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "240"))

# auto_error=False로 설정하여 누락 시에도 FastAPI 기본 텍스트 대신 표준 SrFormat 401 JSON을 반환하도록 제어
security = HTTPBearer(auto_error=False)


class UserRole(StrEnum):
    """시스템 내 사용자 권한 역할(Role) 열거형"""
    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"


# ==============================================================================
# 클라이언트 IP 추출 유틸리티 (Reverse Proxy & Cloudflare / Nginx 호환)
# ==============================================================================
_DEFAULT_TRUSTED_PROXIES = {
    "127.0.0.1",
    "::1",
    "localhost",
    "testclient",
}

_PRIVATE_PROXIES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def is_trusted_proxy(ip_str: Optional[str]) -> bool:
    """
    주어진 IP 문자열이 신뢰할 수 있는 리버스 프록시인지 판별합니다.
    (SECURITY_AND_AUDIT.md 1.1 및 TODO.md 6.1 P0: X-Forwarded-For 스푸핑 방어)
    """
    if not ip_str:
        return False
    ip_clean = ip_str.strip()
    if ip_clean in _DEFAULT_TRUSTED_PROXIES:
        return True

    # 환경변수 TRUSTED_PROXIES ("127.0.0.1,10.0.0.0/8,...") 검사
    env_proxies = os.getenv("TRUSTED_PROXIES")
    if env_proxies:
        for net_str in env_proxies.split(","):
            net_str = net_str.strip()
            if not net_str:
                continue
            if ip_clean == net_str:
                return True
            try:
                if "/" in net_str and ipaddress.ip_address(ip_clean) in ipaddress.ip_network(net_str, strict=False):
                    return True
            except ValueError:
                pass

    # 기본 사설망(Private/Loopback IP) 검사 (Docker 내부 브릿지, 사설 서브넷)
    try:
        ip_obj = ipaddress.ip_address(ip_clean)
        return ip_obj.is_loopback or any(ip_obj in net for net in _PRIVATE_PROXIES)
    except ValueError:
        return False


def get_client_ip(request: Request) -> str:
    """
    리버스 프록시(Nginx, Docker, Cloudflare 등) 환경에서 IP 스푸핑 공격을 원천 방어하며
    클라이언트의 실제 IP를 안전하게 추출합니다.

    보안 메커니즘 (SECURITY_AND_AUDIT.md 1.1 방어):
    1. 직접 연결 소켓 IP(request.client.host)가 신뢰된 프록시(is_trusted_proxy)인 경우에만
       CF-Connecting-IP, X-Real-IP, X-Forwarded-For 헤더를 역추적 신뢰합니다.
    2. 직전 홉이 신뢰되지 않은 외부 클라이언트 직접 접속인 경우,
       클라이언트가 위조하여 보낸 프록시 헤더를 완전히 무시하고 socket IP를 반환하여
       로그인 및 OTP 발송 Rate Limiter 우회를 차단합니다.
    3. X-Forwarded-For 체인은 우측(최근 홉)부터 역추적하여 첫 번째 비신뢰 IP를 실제 발신자로 확정합니다.
    """
    direct_ip = request.client.host if request.client else "unknown"
    if direct_ip == "unknown":
        return "unknown"

    # 직접 연결 IP가 신뢰된 프록시가 아니라면, 헤더 위조 가능성이 있으므로 직접 IP 반환
    if not is_trusted_proxy(direct_ip):
        return direct_ip

    # 1. Cloudflare 경유 시 CF-Connecting-IP 헤더 우선
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip and cf_ip.strip():
        return cf_ip.strip()

    # 2. Nginx proxy_set_header X-Real-IP
    real_ip = request.headers.get("X-Real-IP")
    if real_ip and real_ip.strip():
        return real_ip.strip()

    # 3. X-Forwarded-For 체인 역추적 (우측->좌측 순으로 신뢰 프록시 제거)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        for ip in reversed(parts):
            if not is_trusted_proxy(ip):
                return ip
        # 체인의 모든 노드가 신뢰 프록시인 경우 가장 최초 발신 IP 반환
        if parts:
            return parts[0]

    return direct_ip


# ==============================================================================
# 미디어 열람 및 다운로드 일회용 티켓 / 서명 토큰 보안 모듈 (P1 1.2)
# ==============================================================================
async def create_download_ticket(
    user_uuid: str,
    role: str,
    file_path: str,
    redis: aioredis.Redis,
    ttl_seconds: int = 60,
) -> str:
    """
    미디어 및 파일의 안전한 1회용 열람/다운로드를 위한 단기 유효 일회용 티켓(Single-use Ticket)을 발급합니다.
    URL 쿼리 스트링에 장기 유효 Access Token이 노출되는 취약점을 원천 해소합니다.
    (SECURITY_AND_AUDIT.md 1.2 & TODO.md 6.1 P1)
    """
    ticket_id = secrets.token_urlsafe(32)
    ticket_payload = json.dumps({
        "uuid": user_uuid,
        "role": role,
        "file_path": file_path.strip().lstrip("/\\"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    ticket_key = f"download_ticket:{ticket_id}"
    await redis.setex(ticket_key, ttl_seconds, ticket_payload)
    return ticket_id


async def verify_and_consume_download_ticket(
    ticket_id: str,
    requested_file_path: str,
    redis: aioredis.Redis,
) -> Optional[Dict[str, Any]]:
    """
    일회용 다운로드 티켓을 검증하고 파일 경로 일치 시 즉시 소비(Single-use 원자적 삭제)합니다.
    """
    if not ticket_id or not redis:
        return None

    ticket_key = f"download_ticket:{ticket_id}"
    raw_data = await redis.get(ticket_key)
    if not raw_data:
        return None

    try:
        data = json.loads(raw_data)
    except Exception:
        await redis.delete(ticket_key)
        return None

    expected_path = data.get("file_path", "").strip().lstrip("/\\")
    req_path = requested_file_path.strip().lstrip("/\\")
    if expected_path.startswith("uploads/"):
        expected_path = expected_path[8:]
    if req_path.startswith("uploads/"):
        req_path = req_path[8:]

    expected_path = os.path.normpath(expected_path).replace("\\", "/")
    req_path = os.path.normpath(req_path).replace("\\", "/")

    # 발급 대상 파일 경로와 요청 파일 경로 불일치 시 거부 (경로 탈취 방어, 티켓은 보존)
    if expected_path != req_path:
        return None

    # 경로 일치 확인 후 티켓 소비 및 즉시 파기 (Single-use 보장)
    await redis.delete(ticket_key)

    return {
        "uuid": data.get("uuid"),
        "role": data.get("role"),
    }


def create_signed_download_token(
    user_uuid: str,
    role: str,
    file_path: str,
    ttl_seconds: int = 60,
) -> str:
    """
    Redis 비의존 상태에서도 안전하게 미디어를 열람할 수 있도록
    HMAC-SHA256으로 서명되고 특정 파일 경로 및 단기 만료(60초)에 바인딩된 다운로드 서명 토큰을 생성합니다.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(seconds=ttl_seconds)
    raw_p = file_path.strip().lstrip("/\\")
    if raw_p.startswith("uploads/"):
        raw_p = raw_p[8:]
    clean_p = os.path.normpath(raw_p).replace("\\", "/")
    payload = {
        "sub": user_uuid,
        "role": str(role),
        "path": clean_p,
        "purpose": "download",
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def verify_signed_download_token(
    token_str: str,
    requested_file_path: str,
) -> Optional[Dict[str, Any]]:
    """
    다운로드 서명 토큰을 검증하고 파일 경로 일치 시 사용자 식별 정보를 반환합니다.
    """
    if not token_str:
        return None
    try:
        payload = jwt.decode(token_str, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None

    if payload.get("purpose") != "download":
        return None

    allowed_path = payload.get("path", "").strip().lstrip("/\\")
    req_path = requested_file_path.strip().lstrip("/\\")
    if allowed_path.startswith("uploads/"):
        allowed_path = allowed_path[8:]
    if req_path.startswith("uploads/"):
        req_path = req_path[8:]

    allowed_path = os.path.normpath(allowed_path).replace("\\", "/")
    req_path = os.path.normpath(req_path).replace("\\", "/")

    if allowed_path != req_path:
        return None

    return {
        "uuid": payload.get("sub"),
        "role": payload.get("role"),
    }


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    사용자 인증 후 전달할 JWT Access Token을 발급합니다.

    페이로드 구성:
      - sub: user["uuid"] (사용자 고유 UUID)
      - id: user["id"] (로그인 아이디)
      - role: "student" | "teacher" | "admin"
      - jti: 고유 UUID4 (Redis 블랙리스트 등록 및 토큰 고유 식별용)
      - exp: 만료 시각 (기본 240분)
      - iat: 발급 시각 (UTC 타임스탬프)
    """
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    if "jti" not in to_encode:
        to_encode["jti"] = str(uuid.uuid4())

    to_encode["purpose"] = "access"
    to_encode["iat"] = int(now.timestamp())
    to_encode["exp"] = int(expire.timestamp())

    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    redis: aioredis.Redis = Depends(get_redis),
) -> Dict[str, Any]:
    """
    FastAPI 의존성 주입용: 요청 헤더의 JWT 토큰을 파싱하고 유효성 및 블랙리스트 등록 여부를 검증합니다.
    (SEC-02: purpose=access 필수 검증, download 토큰 차단, Redis 장애 시 503 반환)
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="UNAUTHORIZED",
                    message="인증 토큰이 누락되었거나 Bearer 형식이 아닙니다.",
                ),
            ).model_dump(),
        )

    token = credentials.credentials

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="UNAUTHORIZED",
                    message="토큰이 만료되었습니다. 다시 로그인해주세요.",
                ),
            ).model_dump(),
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="UNAUTHORIZED",
                    message="유효하지 않은 인증 토큰입니다.",
                ),
            ).model_dump(),
        )

    # SEC-02: 다운로드 전용 토큰은 일반 API 인증에 사용 불가
    if payload.get("purpose") == "download":
        raise HTTPException(
            status_code=401,
            detail=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="UNAUTHORIZED",
                    message="다운로드 전용 토큰은 일반 API 인증에 사용할 수 없습니다.",
                ),
            ).model_dump(),
        )

    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=401,
            detail=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="UNAUTHORIZED",
                    message="올바르지 않은 토큰 페이로드 형식입니다 (jti 누락).",
                ),
            ).model_dump(),
        )

    # Redis 블랙리스트 키 확인: 장애 시 pass로 우회하지 않고 503 반환 (SEC-02)
    try:
        is_revoked = await redis.get(f"blacklist:{jti}")
    except Exception:
        raise HTTPException(
            status_code=503,
            detail=SrFormat(
                status_code=503,
                success=False,
                data=None,
                error=Error(
                    code="SERVICE_UNAVAILABLE",
                    message="인증 세션 상태 확인 중 오류가 발생했습니다.",
                ),
            ).model_dump(),
        )

    if is_revoked:
        raise HTTPException(
            status_code=401,
            detail=SrFormat(
                status_code=401,
                success=False,
                data=None,
                error=Error(
                    code="UNAUTHORIZED",
                    message="이미 로그아웃되거나 무효화된 토큰입니다.",
                ),
            ).model_dump(),
        )

    user_uuid = payload.get("sub") or payload.get("uuid")

    return {
        "uuid": user_uuid,
        "sub": user_uuid,
        "id": payload.get("id"),
        "role": payload.get("role"),
        "jti": jti,
        "exp": payload.get("exp"),
        "token": token,
    }


class RoleChecker:
    """
    선언적 역할 기반 접근 제어 (RBAC) 검증 클래스
    
    허용된 역할 목록(allowed_roles)을 받아 현재 사용자의 role과 대조하며,
    권한이 없을 경우 403 FORBIDDEN 표준 에러를 발생시킵니다.
    문자열 역할("student", "teacher", "admin")과 정수형 역할(0, 1, 2)을 모두 정상 정규화하여 호환합니다.
    """

    ROLE_NORMALIZATION: Dict[Any, str] = {
        0: "student",
        "0": "student",
        "student": "student",
        1: "teacher",
        "1": "teacher",
        "teacher": "teacher",
        2: "admin",
        "2": "admin",
        "admin": "admin",
    }

    def __init__(self, allowed_roles: Sequence[str | int | UserRole]):
        self.allowed_roles = set()
        for r in allowed_roles:
            norm = self.ROLE_NORMALIZATION.get(r, str(r))
            self.allowed_roles.add(norm)
            self.allowed_roles.add(str(r))
            if isinstance(r, int):
                self.allowed_roles.add(r)

    def __call__(self, current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        user_role = current_user.get("role")
        norm_user_role = self.ROLE_NORMALIZATION.get(user_role, str(user_role))
        if norm_user_role not in self.allowed_roles and user_role not in self.allowed_roles and str(user_role) not in self.allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=SrFormat(
                    status_code=403,
                    success=False,
                    data=None,
                    error=Error(
                        code="FORBIDDEN",
                        message="해당 리소스에 접근할 권한이 없습니다.",
                    ),
                ).model_dump(),
            )
        return current_user


def require_roles(*roles: str | int | UserRole) -> Callable[..., Dict[str, Any]]:
    """
    지정된 역할 목록 중 하나 이상을 가진 사용자만 접근을 허용하는 의존성 팩토리 함수

    사용 예시:
        @router.get("/admin/overview")
        async def admin_overview(user: dict = Depends(require_roles(UserRole.ADMIN))):
            ...

        @router.get("/teacher/students")
        async def get_students(user: dict = Depends(require_roles("teacher", "admin"))):
            ...
    """
    return RoleChecker(roles)


# ==============================================================================
# 사전 구성된 단축 의존성 (Pre-configured Shortcut Dependencies)
# ==============================================================================
require_student = require_roles(UserRole.STUDENT)
require_teacher = require_roles(UserRole.TEACHER)
require_admin = require_roles(UserRole.ADMIN)
require_staff = require_roles(UserRole.TEACHER, UserRole.ADMIN)
