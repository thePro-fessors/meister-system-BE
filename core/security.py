"""
core/security.py - JWT 인증, 토큰 수명 주기 관리 및 역할 기반 접근 제어(RBAC) 보안 모듈

주요 기능:
1. JWT Access Token 발급 및 무차별 대입 방지용 JTI(UUID) 삽입
2. HTTP Bearer 인증 헤더 파싱 및 서명/만료시간 검증
3. Redis O(1) 블랙리스트 조회를 통한 즉시 로그아웃 세션 무효화
4. 선언적 RBAC(Role-Based Access Control) 의존성 팩토리 (`require_roles`, `require_student` 등)
5. 리버스 프록시(Nginx/Docker/Cloudflare) 환경 대응 클라이언트 IP 추출 유틸리티 (`get_client_ip`)
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Dict, Sequence

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
def get_client_ip(request: Request) -> str:
    """
    리버스 프록시(Nginx, Docker, Cloudflare, Traefik 등) 환경을 고려하여
    클라이언트의 실제 공인/사설 IP를 안전하게 추출합니다.

    우선순위:
    1. X-Forwarded-For 헤더 (클라이언트 최초 발신 IP)
    2. X-Real-IP 헤더
    3. request.client.host (직접 연결 소켓 IP)
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # X-Forwarded-For: <client>, <proxy1>, <proxy2>
        client = forwarded.split(",")[0].strip()
        if client:
            return client

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        client = real_ip.strip()
        if client:
            return client

    if request.client and request.client.host:
        return request.client.host

    return "unknown"


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

    to_encode["iat"] = int(now.timestamp())
    to_encode["exp"] = int(expire.timestamp())

    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    redis: aioredis.Redis = Depends(get_redis),
) -> Dict[str, Any]:
    """
    FastAPI 의존성 주입용: 요청 헤더의 JWT 토큰을 파싱하고 유효성 및 블랙리스트 등록 여부를 검증합니다.
    
    검증 절차:
    1. Authorization Bearer 헤더 존재 여부 확인 (미존재 시 401 UNAUTHORIZED)
    2. JWT 서명 위조 및 만료 시간(exp) 확인 (만료 시 401 UNAUTHORIZED)
    3. 페이로드 내 jti 추출 및 Redis 블랙리스트 키(`blacklist:{jti}`) 존재 여부 O(1) 검사
    4. 검증 완료 시 사용자 식별 정보 딕셔너리 반환
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
                    message="올바르지 않은 토큰 페이로드 형식입니다.",
                ),
            ).model_dump(),
        )

    # Redis 블랙리스트 키 확인: O(1) 초고속 조회
    is_revoked = await redis.get(f"blacklist:{jti}")
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
    """

    def __init__(self, allowed_roles: Sequence[str | UserRole]):
        self.allowed_roles = {str(r) for r in allowed_roles}

    def __call__(self, current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        user_role = current_user.get("role")
        if user_role not in self.allowed_roles:
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


def require_roles(*roles: str | UserRole) -> Callable[..., Dict[str, Any]]:
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
