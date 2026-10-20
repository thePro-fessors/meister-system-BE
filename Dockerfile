# ==============================================================================
# Meister Backend Production Dockerfile (Python 3.12-slim)
# ==============================================================================
# 향후 Python 버전 업그레이드 시 ARG PYTHON_VERSION=3.15-slim 등으로 1줄 변경 가능
ARG PYTHON_VERSION=3.12-slim

# ------------------------------------------------------------------------------
# Stage 1: Build & Dependency Stage
# ------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

WORKDIR /build

# 빌드 및 컴파일에 필요한 최소 시스템 패키지 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt 복사 및 가상환경 빌드
COPY requirements.txt .

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------------------------
# Stage 2: Minimal Runtime Stage (Security Hardened & Non-root)
# ------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS runtime

LABEL maintainer="BSSM Meister Certification Team"
LABEL description="BSSM Meister Competency Certification System Backend"

WORKDIR /app

# 런타임 의존성 (PDF 폰트 렌더링, 헬스체크용 curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 빌더 스테이지에서 가상환경 복사
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

# 보안을 위한 비특권(Non-root) 전용 실행 계정 생성 (UID: 10001)
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /sbin/nologin -d /app appuser && \
    mkdir -p /app/uploads /app/assets/fonts && \
    chown -R appuser:appgroup /app

# 애플리케이션 소스코드 및 정적 폰트 복사
COPY --chown=appuser:appgroup . /app

# 비특권 사용자로 전환
USER appuser

# 컨테이너 헬스체크 설정 (FastAPI /health 엔드포인트)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/health || exit 1

EXPOSE 8000

# Gunicorn / UvicornWorker 실행 (프로덕션 4 워커, 신뢰된 사설망/프록시 대역만 X-Forwarded-For 허용)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"]
