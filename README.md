# 🏫 부산소프트웨어마이스터고 마이스터 역량인증제 백엔드 (MEISTER BACKEND)

> **부산소프트웨어마이스터고등학교(BSSM) 마이스터 역량인증제 전산 관리 시스템 백엔드 API 서비스**  
> 고성능 비동기 Python FastAPI, MySQL 8.0, Redis 7.0, Multi-Provider AI (Gemini / Claude / OpenAI) 기반 엔터프라이즈 아키텍처

---

## 📚 빠른 문서 바로가기

| 문서명 | 목적 및 내용 | 바로가기 |
|:---|:---|:---:|
| **실행 가이드** | Docker 원클릭 실행, 로컬 개발 환경, DB 시드 주입, 테스트 계정 목록 | [RUN_GUIDE.md](file:///Users/satellite/개발/meister-backend/BE/RUN_GUIDE.md) |
| **사용 설명서 & 시연 큐시트** | 학생·교사·관리자 상세 기능 매뉴얼, **시연 영상 촬영용 Step-by-Step 큐시트** | [USER_MANUAL.md](file:///Users/satellite/개발/meister-backend/BE/USER_MANUAL.md) |
| **서버 운영 가이드** | 프로덕션 배포, 환경변수 상세 명세, Nginx 리버스 프록시 연동, 장애 대응 | [OPERATIONS.md](file:///Users/satellite/개발/meister-backend/BE/OPERATIONS.md) |
| **API 계약 명세서** | RESTful API 엔드포인트 명세, 요청/응답 스키마, 에러 코드 | [API_CONTRACT.md](file:///Users/satellite/개발/meister-backend/BE/API_CONTRACT.md) |
| **FE 연동 가이드** | 프론트엔드-백엔드 연동 명세, 토큰 저장소 및 권한 규격 | [FE_INTEGRATION_GUIDE.md](file:///Users/satellite/개발/meister-backend/BE/FE_INTEGRATION_GUIDE.md) |
| **보안 및 감사 규격** | RBAC 인가 계층, 불변 감사 로그(Audit Log), 취약점 방어 전략 | [SECURITY_AND_AUDIT.md](file:///Users/satellite/개발/meister-backend/BE/SECURITY_AND_AUDIT.md) |
| **기술 명세서** | 데이터 모델링(ERD), 아키텍처 다이어그램, 8대 배점 산정 로직 | [Tech_spec.md](file:///Users/satellite/개발/meister-backend/BE/Tech_spec.md) |

---

## ⚡ 빠른 시작 (Quick Start)

### 1. Docker Compose 원클릭 실행 (권장)
```bash
# 1. BE 디렉터리 진입
cd /Users/satellite/개발/meister-backend/BE

# 2. 전체 서비스 빌드 및 실행 (MySQL, Redis, Backend, Frontend, Nginx)
docker compose up -d --build

# 3. 시연용 초기 시드 데이터 주입 (관리자, 교사 4명, 학생 16명, 5대 영역 평가 기준)
docker compose exec backend python scripts/seed_initial_data.py --with-sample-data
```

### 2. 접속 주소
- **웹 서비스 (프론트엔드)**: [https://localhost](https://localhost)
- **백엔드 헬스체크**: [https://localhost/health](https://localhost/health)
- **대화형 API 문서 (Swagger UI)**: [https://localhost/docs](https://localhost/docs)
- **API 명세서 (ReDoc)**: [https://localhost/redoc](https://localhost/redoc)

> 💡 **브라우저 SSL 보안 경고 통과 팁**: 자체 서명 인증서로 인해 경고 화면이 나타날 경우, 키보드로 `thisisunsafe`를 입력하면 즉시 접속됩니다.

---

## 🔑 대표 시연용 계정 정보

- **시스템 관리자**: `admin` / `admin123!` (또는 데모: `admin1` / `1234`)
- **1학년 1반 담임교사**: `teacher1` / `password123!` (또는 데모: `teacher1` / `1234`)
- **1학년 1반 학생 (홍길동)**: `student1` / `password123!` (또는 데모: `student1` / `1234`)

자세한 계정 명세와 시연 영상 녹화 시나리오는 [USER_MANUAL.md](file:///Users/satellite/개발/meister-backend/BE/USER_MANUAL.md)를 참고하시기 바랍니다.
