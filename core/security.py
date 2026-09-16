import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import redis.asyncio as aioredis
from dotenv import load_dotenv

from database import get_redis
from sr_format import Error, SrFormat

load_dotenv()

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "default-secret-key-change-it-now!")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "240"))

security = HTTPBearer(auto_error=False)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """JWT Access Token 발급

    요구 필드:
      - sub: user["uuid"]
      - id: user["id"]
      - role: 문자열 ("student", "teacher", "admin")
      - jti: 고유 UUID
      - exp: 만료 시각 (기본 240분)
      - iat: 발급 시각
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
    """JWT 토큰 검증 및 Redis 블랙리스트 확인 의존성"""
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

    # Redis 블랙리스트 키 확인: O(1)
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
