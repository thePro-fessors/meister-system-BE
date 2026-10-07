# 🚀 마이스터 역량인증제 백엔드 서버 운영 및 초기 기동 가이드 (OPERATIONS GUIDE)

> **문서 버전**: v1.0  
> **최종 갱신 일자**: 2026-10-07  
> **대상 시스템**: 부산소프트웨어마이스터고등학교 마이스터 역량인증제 시스템 (`meister-backend`)  
> **핵심 스택**: Python 3.12+, FastAPI, asyncmy (MySQL 8.0+), Redis 7.0+, Uvicorn/Gunicorn  

---

## 📌 목차
1. [시스템 아키텍처 개요](#1-시스템-아키텍처-개요)
2. [초기 환경 구축 및 의존성 설치 (Cold Start)](#2-초기-환경-구축-및-의존성-설치-cold-start)
3. [환경변수(.env) 설정 명세표 및 보안 가이드](#3-환경변수env-설정-명세표-및-보안-가이드)
4. [데이터베이스(MySQL) 구축 및 DDL 마이그레이션 절차](#4-데이터베이스mysql-구축-및-ddl-마이그레이션-절차)
5. [Redis 스토어 설정 및 캐시 관리](#5-redis-스토어-설정-및-캐시-관리)
6. [서버 기동 및 프로덕션 프로세스 관리](#6-서버-기동-및-프로덕션-프로세스-관리)
7. [리버스 프록시(Nginx / Cloudflare) 연동 설정](#7-리버스-프록시nginx--cloudflare-연동-설정)
8. [정기 유지보수 및 연례 학사 운영 태스크](#8-정기-유지보수-및-연례-학사-운영-태스크)
9. [모니터링 및 트러블슈팅 가이드](#9-모니터링-및-트러블슈팅-가이드)

---

## 1. 시스템 아키텍처 개요

본 시스템은 학생의 마이스터 역량인증제 활동 증빙 제출/심사, 교사의 승인/반려/상벌점 부여, 관리자의 학년도 및 평가 기준 관리와 통계를 지원하는 비동기 RESTful API 백엔드입니다.

- **웹 프레임워크**: FastAPI (ASGI 기반 고성능 비동기 처리)
- **주 데이터베이스**: MySQL 8.0+ (`asyncmy` 비동기 커넥션 풀)
- **캐시 및 분산락**: Redis 7.0+ (`redis.asyncio`, OTP 검증, 브루트포스 차단, 업로드 티켓, 동시성 분산 락)
- **보안/인가 계층**: JWT (HMAC-SHA256), 단일 소비 일회용 티켓, 경로 바인딩 단기 서명, RBAC 중앙 인가(`core/authorization.py`)
- **파일 스토리지**: 로컬 디스크 격리 저장 (`UUIDv4.hex`, 14종 바이너리 매직바이트 검증, 비동기 디렉터리 순회 GC)

---

## 2. 초기 환경 구축 및 의존성 설치 (Cold Start)

### 2.1 Python 3.12+ 가상환경 구성
본 시스템은 Python 3.12 이상의 문법 및 기능을 사용합니다.

```bash
# 1. 저장소 클론 및 프로젝트 디렉터리 진입
cd meister-backend

# 2. Python 3.12 가상환경 생성
python3.12 -m venv .venv

# 3. 가상환경 활성화 (macOS / Linux)
source .venv/bin/activate

# 4. uv 패키지 매니저 활용 시 (권장 - 10배 이상 고속 설치)
pip install uv
uv sync

# 또는 pip을 통한 pyproject.toml 의존성 직접 설치
pip install -e .
```

---

## 3. 환경변수(.env) 설정 명세표 및 보안 가이드

서버 루트 디렉터리의 `.env` 파일에 다음 환경변수를 반드시 정의해야 합니다. `DATABASE_PASSWORD` 등 핵심 보안 변수 누락 시 서버 부팅이 즉시 중단(Fail-Fast)됩니다.

| 환경변수명 | 필수 여부 | 기본값 | 설명 및 권장 설정 |
|:---|:---:|:---:|:---|
| `DATABASE_HOST` | 선택 | `127.0.0.1` | MySQL 데이터베이스 호스트 주소 |
| `DATABASE_PORT` | 선택 | `3306` | MySQL 포트 번호 |
| `DATABASE_USER` | 선택 | `meister` | MySQL 접속 계정명 |
| `DATABASE_PASSWORD` | **필수** | (없음) | **MySQL 패스워드 (미설정 시 Fail-Fast 부팅 중단)** |
| `DATABASE_NAME` | 선택 | `meister` | 데이터베이스 스키마명 |
| `DATABASE_POOL_MIN` | 선택 | `5` | `asyncmy` 비동기 커넥션 풀 최소 연결 수 |
| `DATABASE_POOL_MAX` | 선택 | `30` | `asyncmy` 비동기 커넥션 풀 최대 연결 수 (동시 트래픽 대응) |
| `REDIS_URL` | 권장 | (없음) | Redis 연결 URI (`redis://:password@host:port/0`) |
| `REDIS_HOST` | 선택 | `localhost` | `REDIS_URL` 미지정 시 사용될 호스트 |
| `REDIS_PORT` | 선택 | `6379` | `REDIS_URL` 미지정 시 사용될 포트 |
| `REDIS_PASSWORD` | 선택 | (없음) | Redis 인증 비밀번호 |
| `REDIS_POOL_MAX` | 선택 | `50` | Redis 커넥션 풀 최대 크기 |
| `JWT_SECRET_KEY` | **필수** | (없음) | **JWT 서명용 256비트 이상 비밀키 (`openssl rand -hex 32` 생성)** |
| `JWT_ALGORITHM` | 선택 | `HS256` | 토큰 서명 알고리즘 |
| `ACCESS_TOKEN_EXPIRE_MINUTES`| 선택 | `240` | 액세스 토큰 유효시간 (분 단위, 기본 4시간) |
| `REFRESH_TOKEN_EXPIRE_DAYS` | 선택 | `7` | 리프레시 토큰 유효기간 (일 단위, 기본 7일) |
| `SINGLE_USE_TICKET_SECRET` | 선택 | `JWT_SECRET_KEY` | 일회용 다운로드 티켓 HMAC 서명 시크릿 |
| `TRUSTED_PROXIES` | **운영필수** | `127.0.0.1` | 리버스 프록시 IP 목록 (쉼표 구분, 예: `10.0.0.1,172.16.0.0/12`) |
| `CORS_ORIGINS` | 선택 | `http://localhost:3000,...` | 허용 오리진 (운영 배포 시 특정 도메인으로 엄격 제한) |
| `UPLOAD_DIR` | 선택 | `uploads` | 증빙자료 업로드 저장 디렉터리 경로 |
| `MAX_FILE_SIZE_MB` | 선택 | `20` | 단일 파일 최대 업로드 용량 제한 (MB 단위) |
| `EMAIL_PROVIDER` | 선택 | `smtp` | 이메일 발송 방식 (`smtp` 또는 `mock`) |
| `SMTP_HOST` | 선택 | `smtp.gmail.com` | SMTP 메일 서버 호스트 |
| `SMTP_PORT` | 선택 | `587` | SMTP 포트 (TLS: 587, SSL: 465) |
| `SMTP_USER` | 선택 | (없음) | SMTP 인증 이메일 계정 |
| `SMTP_PASSWORD` | 선택 | (없음) | SMTP 계정 앱 비밀번호 (Google 2단계 앱 패스워드) |
| `SMTP_FROM_NAME` | 선택 | `부산소프트웨어마이스터고등학교` | 발신자 표시 이름 |

> [!CAUTION]
> 운영 환경에서는 `CORS_ORIGINS`에 `*` 와일드카드를 사용하지 마십시오. 와일드카드 지정 시 `allow_credentials`가 보안 정책상 자동으로 `False`로 강제 다운그레이드됩니다.

---

## 4. 데이터베이스(MySQL) 구축 및 DDL 마이그레이션 절차

### 4.1 신규 데이터베이스 설치 시 (Clean Install)
신규 서버에 처음 데이터베이스를 설치하는 경우, 루트의 `databases.sql`을 실행하여 모든 테이블, 외래키 제약, 성능 복합 인덱스를 일괄 생성합니다.

```bash
# MySQL 접속 후 스키마 생성 및 DDL 실행
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS meister DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -u root -p meister < databases.sql
```

### 4.2 기존 운영 데이터베이스 업그레이드 시 (v1.3 마이그레이션)
기존에 운영 중이던 데이터베이스 인스턴스에는 `SCHEMA_CHANGELOG.md`의 v1.3 패치를 순서대로 실행해야 런타임 컬럼 초과(Error 1406/1264) 및 제약 충돌을 방지할 수 있습니다.

```sql
-- [v1.3 마이그레이션 스크립트]
-- 1. 관리자 상벌점 직접 부여 시 teachers_id NULL 허용 및 100점 확장 (DECIMAL(5,2))
ALTER TABLE merits
  MODIFY COLUMN teachers_id INT NULL COMMENT '교사 고유 ID (관리자 부여 시 NULL 허용)',
  MODIFY COLUMN points DECIMAL(5,2) NULL DEFAULT 1.00 COMMENT '상벌점 점수';

ALTER TABLE merits_log
  MODIFY COLUMN old_points DECIMAL(5,2) NULL COMMENT '변경 전 점수',
  MODIFY COLUMN new_points DECIMAL(5,2) NULL COMMENT '변경 후 점수';

-- 2. users 테이블(VARCHAR(320))과의 이메일 컬럼 길이 불일치 해소
ALTER TABLE students
  MODIFY COLUMN email VARCHAR(255) NOT NULL COMMENT '학생 이메일';

ALTER TABLE teachers
  MODIFY COLUMN email VARCHAR(255) NOT NULL COMMENT '교사 이메일';

-- 3. XSS 이스케이프 문자열 팽창 보존 및 중복 기준 방지 제약
ALTER TABLE evaluation_items
  ADD CONSTRAINT UQ_area_item_name UNIQUE (area_id, name),
  MODIFY COLUMN name VARCHAR(100) NULL COMMENT '평가 항목 이름 (XSS 이스케이프 보존)';

ALTER TABLE certification_areas
  MODIFY COLUMN name VARCHAR(60) NOT NULL COMMENT '인증 영역 (XSS 이스케이프 보존)';

-- 4. 성능 최적화 복합 인덱스 (기존 인덱스 미적용 시)
CREATE INDEX idx_submissions_history
  ON submissions (student_id, is_deleted, created_at DESC, submission_id DESC);

CREATE INDEX idx_merits_history
  ON merits (student_id, is_deleted, occurred_at DESC, merits_point_id DESC);

CREATE INDEX idx_submissions_teacher_filter
  ON submissions (is_deleted, status_code, created_at DESC, submission_id DESC);
```

### 4.3 초기 학년도 및 관리자 계정 셋업
1. 시스템 기동 전 관리자 계정이 `users` 테이블에 최소 1개 이상 존재해야 합니다 (`role = 2`).
2. 학사력(3월 1일 기준)에 맞는 당해 연도가 `academic_years` 테이블에 등록되고 `is_activated = TRUE` 상태여야 합니다.
   - 예: 2026년 3월 기준 $\rightarrow$ `INSERT INTO academic_years (year, is_activated) VALUES (2026, TRUE);`

---

## 5. Redis 스토어 설정 및 캐시 관리

본 백엔드는 단일 장애점(SPOF) 방지 및 보안 강화를 위해 Redis를 필수 의존성으로 사용합니다.

- **OTP 발송 및 인증**: `otp:{email}` (TTL 5분), `otp:attempts:{email}` (최대 5회 초과 시 차단)
- **로그인 브루트포스 차단**: `login:fail:{ip}` (분당 20회 초과 시 429), `login:lock:{user_id}` (5회 연속 실패 시 10분 잠금)
- **일회용 파일 다운로드 티켓**: `ticket:{ticket_uuid}` (TTL 60초, 단 1회 읽기 후 원자적 삭제)
- **동시 제출 방어 분산 락**: `lock:submission:{student_id}:{item_id}` (TTL 10초)

```bash
# Redis 서버 상태 확인
redis-cli ping
# 응답: PONG

# Redis 메모리 및 키 확인
redis-cli info memory
```

---

## 6. 서버 기동 및 프로덕션 프로세스 관리

### 6.1 개발 및 로컬 테스트 기동
```bash
# 자동 리로드 활성화
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 6.2 프로덕션 배포 기동 (Gunicorn + UvicornWorker)
프로덕션 환경에서는 멀티 코어 CPU를 효율적으로 활용하기 위해 Gunicorn 마스터 프로세스 아래 Uvicorn 비동기 워커를 구동합니다. 권장 워커 수: `(2 x CPU_CORES) + 1`

```bash
# Gunicorn 멀티 워커 구동 (워커 4개 기준 예시)
gunicorn main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --access-logfile - \
  --error-logfile - \
  --timeout 120 \
  --graceful-timeout 30 \
  --keep-alive 5
```

### 6.3 Systemd 서비스 등록 예시 (`/etc/systemd/system/meister.service`)
```ini
[Unit]
Description=Meister Backend FastAPI Service
After=network.target mysql.service redis-server.service

[Service]
Type=simple
User=meister
Group=meister
WorkingDirectory=/var/www/meister-backend
EnvironmentFile=/var/www/meister-backend/.env
ExecStart=/var/www/meister-backend/.venv/bin/gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8000 --access-logfile - --error-logfile -
Restart=always
RestartSec=3s
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

```bash
# 서비스 활성화 및 시작
sudo systemctl daemon-reload
sudo systemctl enable --now meister
sudo systemctl status meister
```

---

## 7. 리버스 프록시(Nginx / Cloudflare) 연동 설정

백엔드가 클라이언트의 실제 IP를 판별하고 Rate Limit / 감사 로그를 정상 수집하려면 Nginx에서 프록시 헤더를 정확히 전달해야 합니다.

### Nginx 가상 호스트 설정 예시
```nginx
server {
    listen 80;
    server_name api.meister.school.kr;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name api.meister.school.kr;

    ssl_certificate /etc/letsencrypt/live/api.meister.school.kr/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.meister.school.kr/privkey.pem;

    # 파일 업로드 용량 제한 (백엔드 MAX_FILE_SIZE_MB 이상으로 설정)
    client_max_body_size 30M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;

        # WebSocket 및 비동기 스트림 호환
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # 실제 클라이언트 IP 전달 (core/security.py get_client_ip가 파싱)
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 60s;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

> [!IMPORTANT]
> Nginx 서버의 IP(예: `127.0.0.1` 또는 프라이빗 사설망 IP)를 백엔드 `.env`의 `TRUSTED_PROXIES` 목록에 반드시 포함해야 IP 스푸핑 공격을 방어하면서 실제 클라이언트 IP를 정상 추출할 수 있습니다.

---

## 8. 정기 유지보수 및 연례 학사 운영 태스크

### 8.1 고아 파일(Orphan Files) 자동 정리 (스토리지 GC)
증빙자료 수정/재제출 시 남겨진 고아 파일이나 트랜잭션 도중 단절된 파일은 관리자 GC 엔드포인트를 통해 청소할 수 있습니다.

- **엔드포인트**: `POST /api/admin/storage/gc` (관리자 권한 필요, Role 2)
- **주기적 Cron 등록 (예: 매주 일요일 새벽 03:00)**:
```bash
# /etc/cron.d/meister-gc
0 3 * * 0 meister curl -X POST -H "Authorization: Bearer <ADMIN_TOKEN>" http://127.0.0.1:8000/api/admin/storage/gc > /dev/null 2>&1
```

### 8.2 연례 학년도 전환 절차 (매년 3월 1일 전후)
1. **차년도 등록**: `POST /api/years` `{"year": 2027, "isActivated": false}`
2. **기준 데이터 복제**: `POST /api/years/2027/copy-from/2026`
   - 직전 학년도의 인증 영역 및 평가 항목 일괄 벌크 복제
   - 복제 후 당해 연도 변경 기준을 `PATCH /api/criteria/areas/{id}` 및 `PATCH /api/criteria/items/{id}`로 조정
3. **신규 학년도 활성화**: `PATCH /api/years/2027` `{"isActivated": true}`
   - 활성화 즉시 이전 2026 학년도는 자동으로 비활성화되며, 전체 프로세스 캐시(`_YEAR_ID_CACHE`)가 자동 무효화됩니다.

---

## 9. 모니터링 및 트러블슈팅 가이드

### 9.1 DB 커넥션 풀 고갈 (`TimeoutError: QueuePool limit reached`)
- **원인**: 동시 요청 수가 `DATABASE_POOL_MAX`를 초과하거나, 트랜잭션이 커밋/롤백되지 않고 점유된 상태.
- **조치**:
  1. `.env`에서 `DATABASE_POOL_MAX`를 상향 조정 (예: `30` $\rightarrow$ `50`).
  2. `SHOW PROCESSLIST;`로 슬로우 쿼리 점검 및 락 경합 해소.

### 9.2 Redis 연결 실패 및 장애 시 영향 범위
- **영향**: 로그인 브루트포스 차단 가드, OTP 인증번호 저장, 다운로드 일회용 티켓 발행 실패.
- **조치**:
  1. `systemctl restart redis-server`
  2. Redis 메모리 초과 시 `maxmemory-policy allkeys-lru` 설정 점검.

### 9.3 파일 업로드 거부 (`400 MAGIC_BYTE_MISMATCH`)
- **원인**: 클라이언트가 확장자만 변조하고 실제 내용물이 다른 웹쉘/실행 파일을 업로드했을 때 발생.
- **정상 지원 확장자 (14종)**: `pdf`, `png`, `jpg`, `jpeg`, `gif`, `webp`, `zip`, `hwp`, `hwpx`, `doc`, `docx`, `xls`, `xlsx`, `csv`
- 바이너리 첫 2KB의 매직바이트 시그니처가 정확히 일치해야 승인됩니다.

### 9.4 단위/회귀 테스트 전체 검증
운영 패치 배포 전 항상 전체 157종 회귀 테스트를 통과하는지 확인하십시오.

```bash
./.venv/bin/python -m unittest discover tests
# 결과: Ran 157 tests in ~0.4s - OK
```
