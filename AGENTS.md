# Meister System Backend - Agent Instructions & Architecture Guide (Jules)

이 문서는 Meister System Backend (`meister-backend`) 프로젝트에서 작업하는 AI 에이전트(Jules 등)를 위한 아키텍처 명세, 개발 규칙, 보안 정책 및 최적화 지침서입니다.

---

## 1. 프로젝트 개요 & 기술 스택 (Tech Stack)

- **언어 및 런타임**: Python 3.12 (가상환경: `.venv`)
- **웹 프레임워크**: FastAPI
- **데이터베이스**: MySQL 8.0 (비동기 커넥션 풀: `aiomysql` / `pymysql.cursors.DictCursor`)
- **인메모리 캐시 & 세션**: Redis (비동기 `redis.asyncio`)
- **인증 & 암호화**: JWT (`python-jose` / `PyJWT`), `bcrypt` (72바이트 상한 방어 및 스레드풀 오프로딩)
- **테스트 프레임워크**: `unittest` (`.venv/bin/python -m unittest discover tests -v`, 106개 테스트)

---

## 2. 디렉터리 구조 및 핵심 모듈 역할

```
meister-backend/
├── main.py                     # 앱 진입점, CORS/보안 헤더, 정적 업로드 파일 서빙(/uploads), 티켓 발급
├── database.py                 # MySQL DB 커넥션 풀(get_db) & Redis 풀(get_redis), 롤백 보장
├── databases.sql               # MySQL DDL 스키마 및 초기 시드 데이터 (submissions_status 1~5 등)
├── core/                       # 핵심 도메인 엔진 및 보안 인프라
│   ├── models.py               # 표준 공통 응답 규격(SrFormat, Error) DTO
│   ├── security.py             # JWT 발급/검증, purpose 격리('access' vs 'download'), Redis 블랙리스트
│   ├── authorization.py        # [SEC-01] 중앙 RBAC & 담임교사 학급 스코프 격리 인가 엔진
│   ├── calculator.py           # [SCORE-01] 마이스터 인증제 통일 점수/등급/합격 상태 산출 엔진
│   └── storage.py              # 파일 업로드/저장 경로 정규화, 매직 바이트 검증, 고아 파일 GC
├── routers/                    # 비즈니스 API 엔드포인트
│   ├── auth.py                 # 로그인, 목적 분리 OTP 회원가입/이메일 변경, 로그아웃, /me
│   ├── students.py             # 학생 인증 현황, 제출 내역 목록, 상벌점 목록
│   ├── teachers.py             # 교사 대시보드, 담당 학생 검색(8종 필터링), 학생 상세 조회
│   ├── submissions.py          # 증빙 신규제출, 재제출(상태 2 초기화), 삭제, 단건조회, 승인/반려/점수수정
│   ├── points.py               # 상벌점 단건/목록 조회, 신규 부여(POST 201), 수정(PATCH) 및 merits_log 감사
│   └── admin.py                # 학년도 CRUD(/api/years), 평가기준 관리(/api/criteria), 관리자 대시보드
└── tests/                      # 106종 단위/통합 테스트 스위트
```

---

## 3. 핵심 규칙 & 개발 가이드라인 (Mandatory Rules)

### 3.1. 표준 응답 규격 (`SrFormat`) 준수
모든 API 엔드포인트는 반드시 `core.models.SrFormat` Envelope 형식을 반환해야 합니다. 임의의 raw dict나 Pydantic 모델을 직접 반환하지 마십시오.
```python
# 표준 성공 응답
return SrFormat(status_code=200, success=True, data=result_data).model_dump()

# 표준 에러 응답 (FastAPI JSONResponse)
return JSONResponse(
    status_code=403,
    content=SrFormat(
        status_code=403,
        success=False,
        data=None,
        error=Error(code="FORBIDDEN", message="담당 학급 학생의 리소스만 접근할 수 있습니다."),
    ).model_dump(),
)
```

### 3.2. [SEC-01] 중앙 집중식 인가 엔진 (`core/authorization.py`) 필수 적용
학생 ID 또는 제출 ID를 다루는 모든 엔드포인트는 DB 쿼리 전 반드시 인가 함수를 호출해야 합니다.
- **`authorize_student_access(current_user, student_id, conn, target_year, action)`**:
  - 관리자(`admin`): 전교생 모든 리소스 허용.
  - 학생(`student`): 본인 ID 리소스만 허용 (`uuid` 일치 여부). 심사 작업 수행 시 403 차단.
  - 교사(`teacher`):
    - 담임 교사 (`grade`, `class` 배정): 본인 학급(`sar.grade`, `sar.class`) 소속 학생 리소스만 허용. 타 학급 접근 시 **403 FORBIDDEN** 반환.
    - 교과 교사 (`grade=None, class=None`): 전교생 조회 허용.
- **`authorize_submission_access(current_user, submission_id, conn, action)`**:
  - `submission_id`로부터 소속 학생 및 해당 학년도를 역조회한 후 `authorize_student_access`를 호출. 클라이언트 전달 파라미터를 맹신하지 마십시오.

### 3.3. [SEC-02] 다운로드 토큰 격리 및 Redis 고가용성 방어
- 일반 API 인증 (`get_current_user`): `verify_jwt_token(token, required_purpose="access")`로 검증하며, `purpose="download"` 토큰으로 일반 API 접근 시 즉시 401 차단.
- 다운로드 토큰: `create_download_token(file_path, user_id, role)`으로 발급하며 `purpose="download"`, 정규화된 `path`에 엄격히 바인딩.
- Redis 블랙리스트 조회 중 장애 발생 시 토큰을 무조건 허용하지 않고 **503 SERVICE_UNAVAILABLE**로 안전하게 차단(Fail-Safe).

### 3.4. [DATA-01/02] 재제출 및 심사 상태 전이 일관성
- 제출 상태 코드 표준 매핑:
  - `1`: 제출완료
  - `2`: 검토중 (재제출 완료 시에도 반드시 2로 갱신)
  - `3`: 인정완료
  - `4`: 반려
  - `5`: 재제출요청
- 재제출 시: 기존 심사 필드(`granted_score`, `reviewer_id`, `reviewed_at`, `teacher_comment`)를 `NULL`로 초기화하고 `submissions_logs`에 `'RESUBMIT'` 감사 로그를 기록.
- 심사(승인/반려/점수수정) 시: 낙관적 락(`WHERE status_code IN (1, 2)`) 및 `submissions_logs`에 `'APPROVE'`, `'REJECT'`, `'MODIFY_SCORE'` 이력 원자적 기록.

### 3.5. [SCORE-01] 점수/등급/합격 상태 산출 일원화
- 학생 인증 현황, 교사 학생 검색, 교사 학생 상세 조회 시 점수/상태 계산을 라우터에 중복 구현하지 마십시오.
- 반드시 `core.calculator.calculate_student_certification(areas, submissions, merits)`를 호출하여 일관된 집계 결과를 도출해야 합니다.
- 상벌점 부호 처리: `MERIT` / `상점` / `+`는 가산, `DEMERIT` / `벌점` / `-`는 감산으로 정규화.

### 3.6. DB 트랜잭션 및 풀 안전성 (`database.py`)
- 트랜잭션 처리 패턴:
  ```python
  try:
      await conn.autocommit(False)
      async with conn.cursor(cursor=DictCursor) as cur:
          # SQL 작업
      await conn.commit()
  except Exception:
      await conn.rollback()
      raise
  finally:
      await conn.autocommit(True)
  ```
- `database.py`의 `get_db`는 비정상 트랜잭션이 풀로 반환되는 것을 방지하기 위해 커넥션 해제 전 안전 `rollback()`을 수행하도록 구성되어 있습니다.

---

## 4. 테스트 실행 및 회귀 검증 지침

버그 수정 및 리팩토링 후에는 반드시 전체 테스트 스위트를 실행하여 106개 테스트가 모두 통과하는지 확인해야 합니다.
```bash
# 가상환경 활성화 상태에서 실행
.venv/bin/python -m unittest discover tests -v

# 또는 개별 검증
.venv/bin/python -m unittest tests/test_p0_p1_features.py -v
```
- **테스트 추가 원칙**: 버그 수정 시 반드시 해당 취약점/결함을 재현하는 단위 테스트를 `tests/` 하위에 추가하고, 100% Pass 상태를 유지해야 합니다.
