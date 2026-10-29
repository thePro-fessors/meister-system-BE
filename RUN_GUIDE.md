# 🚀 마이스터 역량인증제 시스템 실행 및 시연 환경 구축 가이드 (RUN GUIDE)

> **대상 시스템**: 부산소프트웨어마이스터고등학교 마이스터 역량인증제 통합 시스템 (`meister-system`)  
> **적용 버전**: v1.4 (FastAPI + Next.js + MySQL 8.0 + Redis 7.0 + Nginx)  
> **문서 목적**: 시연 영상 촬영 및 개발/검증을 위한 원클릭 실행, 로컬 개발 환경 구동, 시드 데이터 주입, 계정 정보 및 트러블슈팅 완벽 가이드  

---

## 1. 시스템 아키텍처 및 포트 구성

본 시스템은 프론트엔드와 백엔드가 Nginx 리버스 프록시를 통해 통합 서빙되는 단일 서버 아키텍처(Single Server Ingress)로 구성되어 있습니다.

```
                      [ 사용자 브라우저 (Chrome / Safari) ]
                                      │
                   ┌──────────────────┴──────────────────┐
                   ▼                                     ▼
             HTTP (Port 80)                       HTTPS (Port 443)
                   │                                     │
                   └─────────▶ [ Nginx Reverse Proxy ] ──┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
  [ Next.js Frontend ]        [ FastAPI Backend ]         [ 증빙자료 볼륨 ]
   Container: :3000            Container: :8000            /var/www/uploads
          │                           │
          │                   ┌───────┴───────┐
          │                   ▼               ▼
          │             [ MySQL 8.0 ]    [ Redis 7.0 ]
          │             Container: :3306 Container: :6379
          │                   │               │
          └───────────────────┴───────────────┘
                     (Docker Internal Network)
```

### 접속 엔드포인트 목록
- **웹 서비스 (프론트엔드)**: `https://localhost` (또는 `http://localhost:3000` 로컬 구동 시)
- **백엔드 REST API 헬스체크**: `https://localhost/health`
- **백엔드 Swagger API 문서**: `https://localhost/docs` (대화형 API 테스트 지원)
- **백엔드 ReDoc 명세서**: `https://localhost/redoc`
- **증빙자료 파일 서빙**: `https://localhost/uploads/{file_name}` (인가된 티켓 필요)

---

## 2. 사전 요구사항 (Prerequisites)

시스템을 실행하기 전 호스트 머신에 다음 도구가 준비되어 있어야 합니다:

| 소프트웨어                  |         권장 버전         |     필수 여부     | 비고                        |
| :-------------------------- | :-----------------------: | :---------------: | :-------------------------- |
| **Docker & Docker Compose** | Docker 24.0+ / Compose v2 | **필수 (방법 A)** | 원클릭 컨테이너 기동        |
| **Python**                  |         3.12 이상         | **필수 (방법 B)** | 백엔드 비동기 런타임        |
| **Node.js**                 |     20.x or 22.x LTS      | **필수 (방법 B)** | Next.js 16 프론트엔드       |
| **uv** (또는 pip)           |         최신 버전         |       권장        | 초고속 Python 패키지 동기화 |
| **MySQL Client**            |           8.0+            |       선택        | 직접 DB 접속 시             |

---

## 3. 방법 A: Docker Compose 원클릭 실행 (시연 영상 촬영 권장)

시연 영상을 촬영할 때 모든 인프라(DB, Redis, Backend, Frontend, Nginx)를 한 번에 기동하고 운영 환경과 동일한 TLS 환경을 보장하는 가장 추천하는 방법입니다.

### Step 1. 프로젝트 디렉터리 이동
터미널을 열고 `BE` 디렉터리로 이동합니다.
```bash
cd /Users/satellite/개발/meister-backend/BE
```

### Step 2. 환경변수(.env) 파일 확인
`BE/.env` 파일이 이미 유효한 설정값으로 준비되어 있습니다. 필요 시 설정을 확인합니다.
```bash
# .env 존재 여부 및 주요 보안 키 확인
ls -la .env
```
*(기본 설정: MySQL 포트 3306, Redis 포트 6379, Gemini AI API 키, 암호화 시크릿 키 등이 사전 완비되어 있습니다.)*

### Step 3. 컨테이너 빌드 및 백그라운드 실행
다음 명령어로 5개 컨테이너(`mysql`, `redis`, `backend`, `frontend`, `nginx`)를 빌드하고 기동합니다:
```bash
docker compose up -d --build
```
> **참고**: 이미 이미지가 빌드되어 있다면 `docker compose up -d`로 수 초 만에 즉시 기동됩니다.

### Step 4. 컨테이너 정상 가동 확인
모든 서비스가 `healthy` 및 `Up` 상태인지 확인합니다:
```bash
docker compose ps
```
출력 예시:
```text
NAME               IMAGE                     STATUS                  PORTS
meister_backend    meister-backend:latest    Up (healthy)            8000/tcp
meister_frontend   meister-frontend:latest   Up                      3000/tcp
meister_mysql      mysql:8.0                 Up (healthy)            3306/tcp, 33060/tcp
meister_nginx      nginx:alpine              Up                      0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
meister_redis      redis:7-alpine            Up (healthy)            6379/tcp
```

### Step 5. 헬스체크 검증
터미널에서 백엔드 및 웹 프록시 연결을 검증합니다:
```bash
curl -k https://localhost/health
# 응답: {"status":"ok","service":"meister-backend"}
```

---

## 4. 방법 B: 로컬 개발 환경 직접 실행 (개발 및 디버깅용)

컨테이너를 사용하지 않고 소스코드를 실시간 핫리로드(Hot-reload)하며 수정/디버깅할 때 사용하는 방식입니다.

### 4.1 데이터베이스 & Redis 기동
로컬에 MySQL/Redis가 설치되어 있지 않다면, Docker를 사용해 DB와 Redis만 먼저 띄웁니다:
```bash
cd /Users/satellite/개발/meister-backend/BE
docker compose up -d mysql redis
```
*(docker-compose.yml에 `127.0.0.1:3306` 및 `127.0.0.1:6379` 포트 포워딩이 설정되어 있어 로컬 호스트의 FastAPI 서버 및 Python 스크립트가 로컬 DB에 즉시 연결됩니다.)*

### 4.2 백엔드 (FastAPI) 로컬 실행
1. Python 3.12 가상환경 활성화:
```bash
cd /Users/satellite/개발/meister-backend/BE
source .venv/bin/activate
```
2. Uvicorn 비동기 서버 실행 (8000 포트):
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
3. 브라우저에서 `http://localhost:8000/docs` 접속하여 Swagger UI 정상 로딩 확인.

### 4.3 프론트엔드 (Next.js) 로컬 실행
새 터미널을 열고 프론트엔드를 실행합니다:
```bash
cd /Users/satellite/개발/meister-backend/FE

# 의존성 설치 (최초 1회)
npm install

# 백엔드 API 대상 로컬 개발 서버 실행 (3000 포트)
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npm run dev
```
브라우저에서 `http://localhost:3000` 접속.

---

## 5. 데이터베이스 초기화 및 시연용 시드(Sample) 데이터 주입

시연 영상 촬영을 위해 **2026 활성 학년도, 5대 마이스터 역량 영역(전공/외국어/자격/인성/창체), 8대 배점 산정 규칙, 교사 계정 4명(교감/부장/담임), 학생 계정 16명**을 자동 생성합니다.

### 5.1 Docker 환경에서 시드 데이터 주입 (원클릭)
컨테이너 내부에서 시드 스크립트를 즉시 실행합니다:
```bash
docker compose exec backend python scripts/seed_initial_data.py --with-sample-data
```

### 5.2 로컬 개발 환경에서 시드 데이터 주입
```bash
cd /Users/satellite/개발/meister-backend/BE
source .venv/bin/activate
python scripts/seed_initial_data.py --with-sample-data
```

### 주입 완료 화면 예시:
```text
[*] MySQL 연결 중... (root@mysql:3306/swMeister)
[1/4] 학년도(academic_years) 확인 및 생성...
  [+] 2026 학년도 신규 생성 및 활성화 완료
[2/4] 관리자(admin) 계정 확인 및 생성...
  [+] 관리자 계정 생성 완료: ID='admin', Role=2
[3/4] 5대 마이스터 역량 인증 영역 및 기본 평가 기준 주입...
  [+] 1~3학년 역량 영역 15개, 평가 항목 30개 시드 등록 완료
[4/4] 테스트용 교사 3명 및 학생 16명 주입...
  [+] 교사 생성: ID='vp' (교감), ID='head1' (학년부장), ID='teacher1' (1학년 1반 담임), ID='teacher2' (1학년 2반 담임)
  [+] 학생 생성: ID='student1' ~ ID='student16' (1반 8명, 2반 8명)
🎉 모든 초기 시드 데이터가 성공적으로 커밋되었습니다!
```

---

## 6. 시연 및 평가용 테스트 계정 명세표

시연 영상 촬영 시 각 역할(학생, 교사, 관리자)별로 사용할 수 있는 공식 계정 목록입니다.

### 6.1 관리자 계정 (Role 2)
| 아이디   |  비밀번호   |     성명      | 직책/권한                   | 시연 추천 기능                                                |
| :------- | :---------: | :-----------: | :-------------------------- | :------------------------------------------------------------ |
| `admin`  | `admin123!` | 시스템관리자  | 마이스터 시스템 최고 관리자 | 전교생 역량 통계, 학년도 개설/마감, 평가 기준 관리, 감사 로그 |
| `admin1` |   `1234`    | 관리자 (데모) | 프론트엔드 모의 관리자      | 클라이언트 데모 로그인용                                      |

### 6.2 교원 계정 (Role 1)
| 아이디     |    비밀번호    |  성명  |    직책    |      담당 학급/교과       | 시연 추천 기능                                  |
| :--------- | :------------: | :----: | :--------: | :-----------------------: | :---------------------------------------------- |
| `teacher1` | `password123!` | 이담임 |  담임교사  | 1학년 1반 담임 (전공교과) | **학생 증빙 심사, 점수 승인/반려, 상벌점 부여** |
| `teacher2` | `password123!` | 최담임 |  담임교사  | 1학년 2반 담임 (보통교과) | 1학년 2반 심사 및 상벌점 부여                   |
| `head1`    | `password123!` | 김부장 |  학년부장  |  1학년 총괄 (마이스터부)  | 1학년 전체 학생 현황 조회 및 심사               |
| `vp`       | `password123!` | 박교감 | 교감선생님 | 전교생 총괄 (교무기획부)  | 전교생 마이스터 인증 현황 및 총괄 통계          |

> **FE 로컬 데모 모드 접속 시**: `teacher1` / `1234` 로도 즉시 로그인 가능합니다.

### 6.3 학생 계정 (Role 0)
| 아이디          |    비밀번호    |   성명   |     학적      | 시연 추천 기능                                              |
| :-------------- | :------------: | :------: | :-----------: | :---------------------------------------------------------- |
| `student1`      | `password123!` |  홍길동  | 1학년 1반 1번 | **대시보드 확인, 증빙 제출(+AI 추천), AI 도우미 챗봇 질의** |
| `student2`      | `password123!` |  강민준  | 1학년 1반 2번 | 추가 증빙 제출 및 재제출/이의신청 시연                      |
| `student3`      | `password123!` |  김서연  | 1학년 1반 3번 | 상벌점 현황 및 종합 역량 달성률 확인                        |
| `student4`~`16` | `password123!` | (학생단) |  1학년 1~2반  | 다수 학생 데이터 집계 및 통계 검증용                        |

> **FE 로컬 데모 모드 접속 시**: `student1` / `1234` 로도 즉시 로그인 가능합니다.

---

## 7. 컨테이너 운영 및 상태 확인 명령어

### 7.1 실시간 로그 모니터링
```bash
# 전체 서비스 실시간 통합 로그
docker compose logs -f

# 백엔드 FastAPI 로그만 집중 모니터링 (API 호출, AI 응답, 에러 확인)
docker compose logs -f backend

# 프론트엔드 Next.js 로그 모니터링
docker compose logs -f frontend

# Nginx 액세스 및 프록시 로그 모니터링
docker compose logs -f nginx
```

### 7.2 컨테이너 재시작 및 정지
```bash
# 전체 컨테이너 일시 중지
docker compose stop

# 중지된 컨테이너 다시 시작
docker compose start

# 특정 서비스(예: backend)만 재시작
docker compose restart backend

# 전체 컨테이너 완전 종료 및 네트워크 정리
docker compose down

# (주의) 볼륨(데이터베이스 데이터)까지 완전 초기화 후 종료
docker compose down -v
```

---

## 8. 트러블슈팅 및 브라우저 보안 경고 통과 가이드

### 8.1 Chrome / Edge 브라우저의 "연결이 비공개로 설정되어 있지 않습니다" (NET::ERR_CERT_AUTHORITY_INVALID)
개발 및 사내 시연을 위한 자체 서명(Self-Signed) SSL 인증서를 사용하므로 최초 접속 시 경고창이 나타날 수 있습니다.
- **해결 방법 1 (가장 빠름)**: 경고창 빈 공간을 마우스로 한 번 클릭한 뒤, 키보드로 영문 `thisisunsafe` 를 연속으로 타이핑하면 경고가 풀리고 바로 페이지가 열립니다.
- **해결 방법 2**: 화면 하단의 `고급(Advanced)` 클릭 ➜ `localhost(안전하지 않음)으로 이동(Proceed to localhost)` 링크를 클릭합니다.

### 8.2 80 또는 443 포트가 이미 사용 중인 경우
로컬 머신에 Apache, IIS, 이전 Nginx 또는 다른 웹 서비스가 포트를 점유하고 있을 때 발생합니다.
```bash
# macOS에서 포트 점유 프로세스 확인
lsof -i :80
lsof -i :443

# 점유 중인 프로세스 종료 (PID 확인 후)
kill -9 <PID>
```

### 8.3 데이터베이스 연결 실패 (Connection refused)
MySQL 컨테이너가 초기화되는 동안 약 10~15초 소요될 수 있습니다. `docker compose ps` 상에서 `meister_mysql`의 상태가 `(healthy)`로 바뀔 때까지 기다린 후 다시 접속합니다.
```bash
docker compose logs mysql
```

### 8.4 브라우저 캐시 및 토큰 리셋
이전 테스트로 인해 브라우저의 잘못된 로그인 세션이 남아있는 경우:
1. 브라우저 개발자 도구(F12) ➜ `Application(애플리케이션)` 탭 진입.
2. `Local storage` 및 `Session storage`에서 `meister_access_token`, `meister_current_user` 삭제.
3. 브라우저 새로고침(Cmd + Shift + R / Ctrl + Shift + R).
