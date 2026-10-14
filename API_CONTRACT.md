# 부산소프트웨어마이스터고 역량인증제 API 계약서 (API Contract Specification)

> **기준 백엔드 리비전**: `7742411` (`main` 브랜치 최신)
> **프레임워크 / 프로토콜**: FastAPI (Python 3.12), REST JSON & Multipart Form-Data
> **인코딩 / 시간대**: `UTF-8`, `Asia/Seoul (KST, UTC+9)`

---

## 1. 공통 통신 규격 (Base Protocol & Envelope)

### 1.1 기본 URL 및 경로 호환 정책
- 모든 비즈니스 API는 `/api/*` 하위 경로를 기본으로 사용합니다.
- **프론트엔드 호환 별칭(Alias) 지원**:
  - 인증 라우터: `/auth/*` 와 `/api/auth/*` 모두 동일하게 동작합니다.
  - 교사 라우터: `/api/teacher/*` (단수)와 `/api/teachers/*` (복수) 모두 동일하게 동작합니다.
  - 평가기준 라우터: `/api/criteria/*` 와 `/api/admin/criteria/*` 모두 동일하게 동작합니다.
  - 학생 AI 챗봇: `/api/ai/student/chat` 과 `/api/ai/chat` 모두 동일하게 동작합니다.

### 1.2 공통 응답 봉투 (`SrFormat`)
파일 다운로드(`GET /uploads/*`, `GET /api/admin/export/stats`)를 제외한 **모든 API 응답**은 성공·실패 여부와 관계없이 아래 `SrFormat` JSON 구조를 반환합니다.

#### 성공 응답 예시 (`200 OK` / `201 Created`)
```json
{
  "status_code": 200,
  "success": true,
  "data": { ... },
  "error": null
}
```

#### 실패 응답 예시 (`4xx` / `5xx`)
```json
{
  "status_code": 403,
  "success": false,
  "data": null,
  "error": {
    "code": "FORBIDDEN",
    "message": "해당 제출물을 심사할 권한이 없습니다."
  }
}
```

### 1.3 인증(Authentication) 및 세션 유지
1. **Authorization 헤더 (권장)**:
   ```http
   Authorization: Bearer <accessToken>
   ```
2. **HttpOnly 쿠키 (자동 병행)**:
   - `POST /auth/login` 성공 시 서버가 `Set-Cookie: access_token=<jwt>; HttpOnly; SameSite=Lax; Path=/` 헤더를 함께 내려줍니다.
   - 브라우저 환경에서 `credentials: 'include'` 설정 시 쿠키만으로도 인증이 유지됩니다.
3. **토큰 무효화 (Blacklist)**:
   - `POST /auth/logout` 호출 시 해당 JWT의 `jti`가 Redis 블랙리스트에 등록되어 만료 전이라도 즉시 폐기됩니다.

### 1.4 사용자 역할(`role`) 코드 규격
| 구분 | JWT Payload / 내부 숫자 코드 | API 응답 문자열 (`user.role`) | 설명 |
| :--- | :---: | :---: | :--- |
| **학생** | `0` (`"0"`) | `"student"` | 본인 데이터 조회, 증빙 제출/수정/미승인 삭제, 학생 AI 이용 |
| **교사** | `1` (`"1"`) | `"teacher"` | 교무분장 범위 내 제출물 열람/심사, 전교생 상벌점 관리, 교사 AI 이용 |
| **관리자** | `2` (`"2"`) | `"admin"` | 전교생 데이터/심사/상벌점, 학년도·기준 설정, 일괄 등록, 관리자 AI 이용 |

### 1.5 공통 에러 코드(`error.code`) 사전
| HTTP 상태 | `error.code` | 발생 원인 및 FE 처리 지침 |
| :---: | :--- | :--- |
| `400` | `VALIDATION_ERROR` | 필수 필드 누락, 데이터 타입/범위 오류 (`error.message`에 `[필드명] 사유` 표시) |
| `400` | `INVALID_SCORE` / `SCORE_OUT_OF_RANGE` | 배점 범위 초과(`0 <= score <= maxScore`) 또는 상벌점 범위 위반(`0 < abs <= 100`) |
| `400` | `INVALID_POINT_SIGN` | 상점에 음수(`score < 0`), 벌점에 양수(`score > 0`) 등 유형과 부호가 모순됨 |
| `400` | `INVALID_UPDATE_FIELDS` | `PATCH` 요청에서 수정 가능한 유효 필드가 하나도 없거나 미지원 필드만 전송됨 |
| `400` | `INACTIVE_YEAR` | 비활성화(마감)된 학년도(`is_activated != 1`)에 신규 증빙 제출 시도 |
| `401` | `UNAUTHORIZED` | 토큰 누락, 만료, 블랙리스트(로그아웃) 토큰, 또는 일회용 다운로드 티켓 만료/재사용 |
| `403` | `FORBIDDEN` | 역할 권한 부족, 타 학생 데이터 접근(IDOR), 담당 교과/학급 외 제출물 접근, 타인 AI 대화 ID 접근 |
| `404` | `NOT_FOUND` | 존재하지 않거나 이미 삭제된 학생/제출물/상벌점/평가항목 조회 |
| `409` | `CONFLICT` | 동시성 충돌 (예: 이미 승인된 제출물 삭제 시도, 삭제된 제출물 승인 시도, 중복 이메일/학년도) — **FE는 최신 상태를 재조회해야 함** |
| `429` | `RATE_LIMIT_EXCEEDED` | 로그인/OTP/AI 호출 빈도 제한 초과 (잠시 후 재시도 안내) |
| `500` | `AI_CONVERSATION_VERIFY_ERROR` | AI 대화방 소유권 검증/생성 중 DB 오류 발생 (후속 LLM 호출 및 메시지 저장 중단됨) |
| `503` | `SERVICE_UNAVAILABLE` | DB/Redis 일시 장애 또는 로그아웃 검증 불가 상태 |

---

## 2. 인증 및 계정 API (`/auth/*` & `/api/auth/*`)

### 2.1 로그인
- **Endpoint**: `POST /auth/login` (별칭: `POST /api/auth/login`)
- **Auth**: 불필요 (Public, IP/계정 Rate Limit 적용)
- **Request Body (`application/json`)**:
  ```json
  {
    "username": "student01@bssm.hs.kr",
    "password": "Password123!"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "access_token": "eyJhbGciOiJIUzI1NiIs...",
      "accessToken": "eyJhbGciOiJIUzI1NiIs...",
      "token_type": "bearer",
      "user": {
        "id": 101,
        "uuid": "550e8400-e29b-41d4-a716-446655440000",
        "email": "student01@bssm.hs.kr",
        "name": "김마이스터",
        "role": "student",
        "role_code": 0,
        "grade": 2,
        "class_no": 1,
        "classNo": 1,
        "number": 5,
        "student_number": 2105
      }
    },
    "error": null
  }
  ```
  > 교사 로그인 시 `user` 객체에 `subject`(담당교과), `position`(직책: `정담임`/`부담임`/`학년부장`/`부서부장`/`교과전담`), `department`(소속부서) 필드가 추가로 포함됩니다.

### 2.2 로그아웃
- **Endpoint**: `POST /auth/logout` (별칭: `POST /api/auth/logout`)
- **Auth**: `Bearer Token` 또는 `Cookie`
- **Response (`200 OK`)**: 쿠키 삭제 및 Redis `blacklist:{jti}` 등록 완료.

### 2.3 내 정보 조회 (세션 검증)
- **Endpoint**: `GET /auth/me` (별칭: `GET /api/auth/me`)
- **Auth**: 필수 (학생 / 교사 / 관리자)
- **Response (`200 OK`)**: 로그인 응답의 `data.user`와 동일한 최신 프로필 객체 반환.

### 2.4 이메일 OTP 발송
- **Endpoint**: `POST /auth/send-otp` (별칭: `POST /api/auth/send-otp`)
- **Auth**:
  - `purpose == "register"`: 불필요 (Public)
  - `purpose == "email_change"`: 로그인 필수 (`Bearer Token`)
- **Request Body**:
  ```json
  {
    "email": "student01@bssm.hs.kr",
    "purpose": "register",
    "student_grade": 1,
    "student_class": 2,
    "student_number": 3,
    "is_it_student": true
  }
  ```
  > `purpose`는 `"register"`(최초 회원가입) 또는 `"email_change"`(이메일 변경) 중 하나입니다.

### 2.5 이메일 OTP 검증
- **Endpoint**: `POST /auth/verify-otp` (별칭: `POST /api/auth/verify-otp`)
- **Request Body**:
  ```json
  {
    "email": "student01@bssm.hs.kr",
    "verify_code": "123456",
    "purpose": "register"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "verified": true,
      "register_token": "eyJhbGciOi...",
      "registerToken": "eyJhbGciOi...",
      "email_change_token": null,
      "emailChangeToken": null
    },
    "error": null
  }
  ```

### 2.6 최초 회원가입 (비밀번호 등록)
- **Endpoint**: `POST /auth/register` (별칭: `POST /api/auth/register`)
- **Request Body**:
  ```json
  {
    "email": "student01@bssm.hs.kr",
    "password": "NewPassword123!",
    "register_token": "eyJhbGciOi...",
    "role": "student"
  }
  ```

### 2.7 이메일 주소 변경
- **Endpoint**: `PATCH /auth/email` (별칭: `PATCH /api/auth/email`)
- **Auth**: 필수
- **Request Body**:
  ```json
  {
    "email": "new_email@bssm.hs.kr",
    "password": "CurrentPassword123!",
    "email_change_token": "eyJhbGciOi..."
  }
  ```

---

## 3. 학생 역량인증 현황 및 내역 API (`/api/students/*`)

> `{student_id}` 파라미터는 학생의 정수 PK(`student_id`, 예: `101`), 학번(`2105`), 또는 `me` 키워드를 모두 지원합니다.

### 3.1 학생 역량인증 종합 현황 조회
- **Endpoint**: `GET /api/students/{student_id}/certification-status`
- **Query Params**: `year` (선택, 기본값: 현재 활성 학년도)
- **Auth**: 학생 본인, 담당 학급 담임교사, 담당 학년부장, 총괄부서장, 관리자
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "year": 2026,
      "grade": 2,
      "classNo": 1,
      "number": 5,
      "totalScore": 74.5,
      "pointTotal": 5,
      "certStatus": "인증 가능",
      "areas": [
        {
          "areaId": 1,
          "area": "전공 영역",
          "score": 30.0,
          "maxScore": 40.0,
          "grade": "우수",
          "status": "달성"
        }
      ]
    },
    "error": null
  }
  ```

### 3.2 학생별 증빙 제출 목록 조회
- **Endpoint**: `GET /api/students/{student_id}/submissions`
- **Query Params**: `year` (선택), `area` (선택), `status` (선택: `심사대기`/`인정완료`/`반려`/`재제출요청`), `page` (기본 `1`), `size` (기본 `50`)
- **Auth**:
  - 학생 본인, 담당 담임교사, 담당 학년부장, 총괄부서장, 관리자: 전체 제출 목록 반환
  - 비담임 교과/부서 교사 및 타 학급 담임교사: **본인이 열람/심사 권한을 가진 평가 항목의 제출물만 필터링**하여 반환 (권한 건수가 0건이면 `403 FORBIDDEN`)
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "items": [
        {
          "id": 501,
          "submissionId": 501,
          "studentId": 101,
          "year": 2026,
          "areaId": 1,
          "area": "전공 영역",
          "itemId": 12,
          "itemName": "전공 자격증 취득 (정보처리기능사)",
          "detail": "정보처리기능사 최종 합격",
          "activityDate": "2026-05-10",
          "description": "한국산업인력공단 정기 기능사 2회 합격증 제출",
          "filePath": "/uploads/2026/101/cert_501.pdf",
          "fileUrl": "/uploads/2026/101/cert_501.pdf",
          "originalFilename": "정보처리기능사_합격증.pdf",
          "linkUrl": "https://q-net.or.kr",
          "status": "인정완료",
          "statusCode": 2,
          "score": 20.0,
          "grantedScore": 20.0,
          "maxScore": 20.0,
          "teacherComment": "자격증 원본 확인 완료",
          "submittedAt": "2026-05-11T14:20:00",
          "reviewedAt": "2026-05-12T09:15:00",
          "reviewerId": 11
        }
      ],
      "total": 1
    },
    "error": null
  }
  ```

### 3.3 학생별 상벌점 내역 조회
- **Endpoint**: `GET /api/students/{student_id}/points`
- **Query Params**: `year` (선택), `type` (선택: `상점`/`벌점`)
- **Auth**: 학생 본인 또는 모든 교사/관리자

---

## 4. 증빙자료 제출·심사·파일 다운로드 API (`/api/submissions/*`, `/uploads/*`)

### 4.1 신규 증빙자료 제출
- **Endpoint**: `POST /api/submissions` (또는 `POST /api/students/{student_id}/submissions`)
- **Auth**: 학생 본인 전용 (`role == "student"`)
- **Content-Type**: `multipart/form-data`
- **Form Fields**:
  | 필드명 | 타입 | 필수 | 설명 |
  | :--- | :--- | :---: | :--- |
  | `item_id` (또는 `itemId`) | `int` | Y | 평가 항목 ID (`evaluation_items.item_id`) |
  | `detail` | `string` | Y | 활동명 / 취득 내역 요약 (최대 200자) |
  | `activity_date` (또는 `activityDate`) | `string` | Y | 취득/활동 일자 (`YYYY-MM-DD`) |
  | `description` | `string` | N | 상세 부연 설명 |
  | `link_url` (또는 `linkUrl`) | `string` | N | 외부 증빙 URL (`http://` 또는 `https://`만 허용) |
  | `file` | `File` | 조건부 | 증빙 첨부파일 (`requires_evidence=true` 항목이면서 `link_url`이 없으면 필수. 허용 확장자: `pdf`, `png`, `jpg`, `jpeg`, `webp`, 최대 10MB) |
- **활성 학년도 검증 (P1 반영)**:
  - 해당 평가 항목이 속한 학년도의 `is_activated`가 명시적 활성(`1` 또는 `True`)이 아니면 `400 INACTIVE_YEAR`로 즉시 차단되며, 디스크에 잔여 파일을 남기지 않습니다.

### 4.2 반려/재제출요청 건 재제출
- **Endpoint**: `PATCH /api/submissions/{submission_id}/resubmit`
- **Auth**: 제출한 학생 본인 전용
- **Content-Type**: `multipart/form-data` (`detail`, `activity_date`, `description`, `link_url`, `file` 선택 수정)
- **제약**: 이미 `인정완료(2)` 상태인 제출물은 재제출 불가 (`400` 반환). 재제출 시 상태가 `심사대기(0)`로 전환됩니다.

### 4.3 제출물 삭제 (동시성 보호 적용)
- **Endpoint**: `DELETE /api/submissions/{submission_id}`
- **Auth**: 제출한 학생 본인(미승인 상태에 한함) 또는 관리자(`admin`)
- **동시성 제어 (P1 반영)**:
  - 트랜잭션 내부에서 `SELECT ... FOR UPDATE` 행 잠금을 수행합니다.
  - 학생 삭제 요청 시점에 이미 교사가 승인(`status == 2`)했거나 삭제된 상태라면 **`409 CONFLICT`**를 반환하며, 승인된 데이터와 증빙 파일은 안전하게 보존됩니다.

### 4.4 제출물 단건 상세 조회
- **Endpoint**: `GET /api/submissions/{submission_id}`
- **Auth**: 학생 본인 또는 해당 제출물에 대한 열람 권한(`action="read"`)이 있는 교사/관리자

### 4.5 제출물 심사 — 승인 (`approve`)
- **Endpoint**: `PATCH /api/submissions/{submission_id}/approve`
- **Auth**: 해당 제출물에 대한 심사 권한(`action="approve"`)이 있는 교사 또는 관리자
- **Request Body (`application/json`)**:
  ```json
  {
    "score": 20.0,
    "comment": "증빙 확인 완료, 승인합니다."
  }
  ```
  > 필드 별칭 호환: `score` / `grantedScore` / `granted_score`, `comment` / `teacherComment` / `teacher_comment` 모두 지원. `score` 생략 시 해당 항목의 기본 최대 배점(`max_score`)이 부여됩니다.
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "submissionId": 501,
      "status": "인정완료",
      "statusCode": 2,
      "grantedScore": 20.0,
      "teacherComment": "증빙 확인 완료, 승인합니다.",
      "reviewedAt": "2026-10-13T13:30:00",
      "studentTotals": {
        "totalScore": 74.5,
        "pointTotal": 5,
        "certStatus": "인증 가능",
        "areas": [ ... ]
      }
    },
    "error": null
  }
  ```
  > **FE 연동 주의**:① 삭제가 먼저 완료된 제출물을 승인하려 하면 `409 CONFLICT`가 반환됩니다. ② 만약 재계산(`studentTotals`) 중 부수 오류가 발생하면 `studentTotals`는 `null`일 수 있으므로, `null`을 `0점`으로 덮어쓰지 마십시오.

### 4.6 제출물 심사 — 반려 / 재제출요청 (`reject`)
- **Endpoint**: `PATCH /api/submissions/{submission_id}/reject`
- **Auth**: 해당 제출물 심사 권한 보유 교사 또는 관리자
- **Request Body (`application/json`)**:
  ```json
  {
    "comment": "합격증의 성명과 수험번호가 식별되지 않습니다. 원본 PDF로 다시 제출해주세요.",
    "statusCode": 3
  }
  ```
  > `comment`(또는 `reason`, `teacherComment`)는 **필수**입니다. `statusCode`는 `1`(`"반려"`) 또는 `3`(`"재제출요청"`, 기본값 `1`)을 지정할 수 있습니다. 응답에는 `studentTotals`가 포함됩니다.

### 4.7 제출물 부여 점수 수정 (`score`)
- **Endpoint**: `PATCH /api/submissions/{submission_id}/score`
- **Auth**: 해당 제출물 심사 권한 보유 교사 또는 관리자
- **Request Body (`application/json`)**:
  ```json
  {
    "score": 15.0,
    "reason": "공동 수상 기준 적용으로 배점 조정"
  }
  ```
  > `score`(`0 <= score <= maxScore`)와 수정 사유 `reason`(또는 `comment`)은 **필수**이며, 변경 전·후 점수가 `submissions_logs`에 기록되고 응답에 최신 `studentTotals`가 포함됩니다.

### 4.8 증빙 파일 다운로드 티켓 발급 및 열람
브라우저 `<img>`, `<iframe>`, 파일 다운로드 시 URL 쿼리에 장기 JWT가 노출되는 것을 막기 위해 **60초 유효 일회용 티켓(`ticket`)** 방식을 사용합니다.

1. **티켓 발급**:
   - **방식 A (제출 ID 기준 권장)**: `POST /api/submissions/{submission_id}/download-ticket`
   - **방식 B (파일 경로 기준)**: `POST /api/uploads/ticket?file_path=/uploads/2026/101/cert_501.pdf`
   - **Response (`200 OK`)**:
     ```json
     {
       "status_code": 200,
       "success": true,
       "data": {
         "ticket": "d8f9e0a1b2c3...",
         "signedToken": "eyJhbGciOi...",
         "expiresIn": 60,
         "downloadUrl": "/uploads/2026/101/cert_501.pdf?ticket=d8f9e0a1b2c3...",
         "fileUrl": "/uploads/2026/101/cert_501.pdf?ticket=d8f9e0a1b2c3...",
         "signedUrl": "/uploads/2026/101/cert_501.pdf?token=eyJhbGciOi..."
       },
       "error": null
     }
     ```
2. **파일 다운로드/열람**:
   - **Endpoint**: `GET /uploads/{file_path}?ticket={ticket}`
   - **원자적 소비 (P2 반영)**: Redis `GETDEL`을 통해 티켓은 **최초 1회 요청에만 소비**되며, 동일 티켓으로 동시/중복 요청 시 두 번째 요청은 `401 UNAUTHORIZED`로 거부됩니다.

---

## 5. 상벌점 관리 API (`/api/points/*`)

> **권한 정책**: 모든 교사(`role=1`) 및 관리자(`role=2`)는 전교생을 대상으로 상벌점을 조회·부여·수정·삭제할 수 있습니다. 학생은 본인의 상벌점 내역만 조회할 수 있습니다.

### 5.1 상벌점 목록 조회
- **Endpoint**: `GET /api/points`
- **Query Params**: `student_id` (선택), `year` (선택), `type` (`상점`/`벌점`), `page` (기본 `1`), `size` (기본 `50`)

### 5.2 상벌점 부여 (`POST /api/points`)
- **Auth**: 교사 또는 관리자
- **Request Body (`application/json`) — `points` 및 부호 있는 `score` 모두 호환**:
  ```json
  {
    "studentId": 101,
    "type": "벌점",
    "score": -3,
    "reason": "실습실 안전수칙 미준수",
    "occurredAt": "2026-10-13",
    "relatedArea": "인성 영역"
  }
  ```
- **점수·부호 검증 규칙 (P1 계약 확정)**:
  - `points` 또는 `score` 중 하나를 전달합니다 (절댓값 기준 **`0 < |값| <= 100`** 정수/실수).
  - `type`이 `"상점"`(`"MERIT"`)일 때 음수(`score < 0`)를 전달하거나, `"벌점"`(`"DEMERIT"`)일 때 `score` 필드에 양수를 혼용하여 부호가 모순되는 경우 명시적 `400 INVALID_POINT_SIGN` 에러를 반환합니다 (`points` 필드에 양수 절댓값 `3`과 `type: "벌점"`을 전달하는 방식도 허용되며, DB에는 부호가 정규화되어 저장됩니다).
- **Response (`201 Created`)**: 생성된 상벌점 정보 및 재계산된 `studentTotals` 포함.

### 5.3 상벌점 수정 (`PATCH /api/points/{point_id}`)
- **Auth**: 교사 또는 관리자
- **Request Body (`application/json`)**:
  ```json
  {
    "type": "벌점",
    "score": -2,
    "reason": "초기 계도 사유 참작하여 벌점 2점으로 감경",
    "occurredAt": "2026-10-13"
  }
  ```
- **계약 규격 (P1 반영)**:
  - **정식 점수 필드**: `points` 또는 `score` (양쪽 모두 100% 동일하게 동작하며 조용히 무시되지 않음).
  - **필수 필드**: 수정 사유 `reason` (공백 불가, 감사 로그 `merits_log`에 이전·이후 값과 함께 기록됨).
  - **미지원 필드 방어**: 정의되지 않은 임의의 필드만 보낼 경우 `400 INVALID_UPDATE_FIELDS` 에러를 반환합니다.
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "id": 88,
      "pointId": 88,
      "studentId": 101,
      "type": "벌점",
      "points": -2,
      "score": -2,
      "reason": "초기 계도 사유 참작하여 벌점 2점으로 감경",
      "occurredAt": "2026-10-13",
      "studentTotals": {
        "totalScore": 72.5,
        "pointTotal": 3,
        "certStatus": "인증 가능"
      }
    },
    "error": null
  }
  ```

### 5.4 상벌점 삭제 (`DELETE /api/points/{point_id}`)
- **Auth**: 교사 또는 관리자
- **Response (`200 OK`)**: 삭제 완료 메시지 및 최신 `studentTotals` 반환.

---

## 6. 교사 대시보드 및 학생 지도 API (`/api/teacher/*` & `/api/teachers/*`)

### 6.1 교사 대시보드 조회
- **Endpoint**: `GET /api/teacher/dashboard` (별칭: `GET /api/teachers/dashboard`)
- **Query Params**: `year` (선택), `grade` (선택), `class_no` (선택)
- **Auth**: 교사 또는 관리자
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "year": 2026,
      "scopeLabel": "2학년 1반 정담임 (웹프로그래밍)",
      "teacher": {
        "teacherId": 11,
        "name": "박교사",
        "email": "teacher01@bssm.hs.kr",
        "subject": "웹프로그래밍",
        "position": "정담임",
        "department": "마이스터부",
        "grade": 2,
        "classNo": 1
      },
      "kpi": {
        "totalStudents": 16,
        "certifiableCount": 10,
        "reviewingCount": 4,
        "needsImprovementCount": 2,
        "pendingSubmissionCount": 5,
        "averageScore": 68.4
      },
      "recentPendingSubmissions": [ ... ],
      "atRiskStudents": [ ... ]
    },
    "error": null
  }
  ```

### 6.2 교사용 학생 목록 조회
- **Endpoint**: `GET /api/teacher/students` (별칭: `GET /api/teachers/students`)
- **Query Params**:
  - `year` (선택)
  - `grade`, `class_no` (선택: 미지정 시 담임은 본인 학급, 학년부장은 본인 학년 기본 조회)
  - `scope` (`"my"` | `"all"` — 상벌점 부여 또는 교과 심사를 위해 `"all"` 지정 시 전교생 검색 가능)
  - `cert_status` (`인증 가능` / `검토중` / `보완 필요`), `search` (이름/학번 검색)

### 6.3 교사용 학생 개별 상세 조회
- **Endpoint**: `GET /api/teacher/students/{student_id}` (별칭: `GET /api/teachers/students/{student_id}`)
- **Auth**:
  - 담임교사(본인 반), 학년부장(본인 학년), 총괄부서장, 관리자: 학생 전체 영역 현황 + 전체 제출 목록 반환
  - 교과전담/타 반 교사: 해당 학생이 제출한 과제 중 **본인이 심사 권한을 가진 과제가 존재하는 경우** 열람 허용 (제출 목록도 본인 담당 과제로 필터링)

---

## 7. 학년도·평가기준·관리자 행정 API (`/api/years/*`, `/api/criteria/*`, `/api/admin/*`)

### 7.1 학년도 관리 (`/api/years`)
| Method | Endpoint | 권한 | 설명 |
| :---: | :--- | :---: | :--- |
| `GET` | `/api/years` | 전체 로그인 사용자 | 등록된 전체 학년도 목록 및 `isActivated` 상태 조회 |
| `POST` | `/api/years` | 관리자 (`admin`) | 신규 학년도 생성 (`{"year": 2027, "isActivated": true}`) |
| `PATCH` | `/api/years/{year}` | 관리자 (`admin`) | 학년도 활성/비활성 전환 (`{"isActivated": false}`) |
| `POST` | `/api/years/{target_year}/copy-from/{source_year}` | 관리자 (`admin`) | 이전 학년도 평가 영역·항목·전담교사 배정 일괄 복사 |

### 7.2 평가 영역 및 항목 기준 관리 (`/api/criteria` & `/api/admin/criteria`)
| Method | Endpoint | 권한 | 설명 |
| :---: | :--- | :---: | :--- |
| `GET` | `/api/criteria?year=2026` | 전체 로그인 사용자 | 해당 학년도의 5대 영역 및 하위 평가 항목 트리 조회 (`assignedTeachers`, `department`, `subject` 포함) |
| `PATCH` | `/api/criteria/areas/{area_id}` | 관리자 (`admin`) | 영역 배점 한도(`maxScore`), 담당 부서(`department`), 영역 전담교사(`teacherIds`) 수정 |
| `POST` | `/api/criteria/areas/{area_id}/items` | 관리자 (`admin`) | 신규 평가 항목 추가 (`name`, `maxScore`, `department`, `subject`, `requiresEvidence`, `teacherIds` 등) |
| `PATCH` | `/api/criteria/items/{item_id}` | 관리자 (`admin`) | 평가 항목 수정 및 활성 여부(`isActive`), 1순위 전담교사(`teacherIds`) 수정 |
| `DELETE` | `/api/criteria/items/{item_id}` | 관리자 (`admin`) | 평가 항목 삭제 (연결된 제출물이 있으면 Soft-Delete `is_active=false` 전환) |

### 7.3 학교 총괄 현황 및 일괄 등록·감사·통계 (`/api/admin/*`)
| Method | Endpoint | 권한 | 설명 |
| :---: | :--- | :---: | :--- |
| `GET` | `/api/admin/overview?year=2026` | 관리자 (`admin`) | 전교생 학년별/영역별 인증 통계, 심사 대기 현황, 최근 감사 로그 요약 |
| `POST` | `/api/admin/students/batch` | 관리자 (`admin`) | 학생 명렬표 일괄 등록 (JSON 배열 또는 `.xlsx`/`.csv` 파일 업로드, 교사 이메일 교차 중복 차단) |
| `POST` | `/api/admin/teachers/batch` | 관리자 (`admin`) | 교사 명렬표 일괄 등록 (`position`: 정담임/부담임/임시담임/학년부장/부서부장/교과전담, `department`, 복수 담임 허용) |
| `GET` | `/api/admin/audit-logs` | 관리자 (`admin`) | 제출물 심사 로그(`submissions_logs`) 및 상벌점 변경 로그(`merits_log`) 통합 조회 |
| `GET` | `/api/admin/export/stats?year=2026&format=xlsx` | 관리자 (`admin`) | 전교생 역량인증 및 상벌점 현황 엑셀(`xlsx`)/`csv` 다운로드 (수식 인젝션 방어 적용) |
| `POST` | `/api/admin/recalculate` | 관리자 (`admin`) | 전교생(또는 특정 학년/학생) 역량인증 총점 일괄 재계산 및 정합성 보정 |
| `POST` | `/api/admin/storage/gc?dry_run=true` | 관리자 (`admin`) | DB에 연결되지 않은 디스크 고아 파일(Orphan Files) 탐색 및 정리 |

---

## 8. AI Copilot 및 지능형 분석 API (`/api/ai/*`)

### 8.1 Multi-Provider AI 지원 및 응답 공통 메타데이터 규격
본 시스템은 **Google Gemini**, **OpenAI**, **Anthropic Claude**, 및 **OpenAI 호환 Custom Endpoint (Groq, vLLM, Ollama, DeepSeek 등)** 멀티 프로바이더를 전면 지원합니다.

#### 지원 프로바이더 및 환경 변수 우선순위
1. **Google Gemini**: `GEMINI_API_KEY`, `GEMINI_MODEL` (기본값: `gemini-3.8-flash`)
2. **OpenAI**: `OPENAI_API_KEY`, `OPENAI_MODEL` (기본값: `gpt-4o-mini`)
3. **Anthropic Claude**: `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` (기본값: `claude-3-5-sonnet-20241022`)
4. **OpenAI-Compatible Custom Endpoint**: `AI_BASE_URL` (또는 `OPENAI_BASE_URL`), `AI_API_KEY`, `AI_MODEL` (Groq, vLLM, Ollama, DeepSeek)
- **역할별 키/모델/엔드포인트 분리**: `TEACHER_AI_*`, `ADMIN_AI_*`, `STUDENT_AI_*` 환경 변수를 통해 교사/관리자/학생용 AI 프로바이더 및 모델을 각각 독립적으로 구성할 수 있습니다.

#### 공통 응답 메타데이터
모든 AI 엔드포인트는 응답 `data` 내부에 호출된 프로바이더와 모델, 그리고 폴백 여부를 명확히 식별할 수 있도록 다음 필드를 공통 반환합니다:
```json
{
  "analysisSource": "gemini",
  "fallbackUsed": false,
  "fallbackReason": null,
  "model": "gemini-3.8-flash",
  "capabilityMode": "live_llm"
}
```
- `analysisSource`: `"gemini"` | `"openai"` | `"claude"` | `"openai_compatible"` | `"rule_based_fallback"`
- `capabilityMode`: `"live_llm"` (외부 모델 호출 성공) 또는 `"rule_based_engine"` (내장 규칙 기반 엔진 폴백)
- `fallbackUsed`: boolean (LLM 호출 실패, 타임아웃, 응답 파싱 실패 시 `true` 자동 전환)

---

### 8.2 [학생용] 증빙자료 자동 분류 추천
- **Endpoint**: `POST /api/ai/classify-submission`
- **Auth**: 로그인 사용자 (학생 / 교사 / 관리자)
- **Request Body**:
  ```json
  {
    "detail": "TOPCIT 정기평가 수준 3 달성",
    "description": "소프트웨어 역량지수 평가 성적증명서 제출"
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "area": "전공 영역",
      "areaId": 1,
      "itemId": 15,
      "itemName": "TOPCIT 성적 우수",
      "confidence": 0.92,
      "reason": "TOPCIT 정기평가 성적 증명 활동으로 전공 영역의 TOPCIT 항목에 부합합니다.",
      "analysisSource": "gemini",
      "fallbackUsed": false,
      "model": "gemini-3.8-flash"
    },
    "error": null
  }
  ```

### 8.3 [학생용] 맞춤형 역량 달성 상담 챗봇 (P0 소유권 격리 적용)
- **Endpoint**: `POST /api/ai/student/chat` (별칭: `POST /api/ai/chat`)
- **Auth**: 로그인 사용자
- **Request Body**:
  ```json
  {
    "message": "제가 지금 졸업 인증 받으려면 어떤 영역 점수가 부족한가요?",
    "conversationId": "conv_student_101_01"
  }
  ```
- **대화방 소유권 보안 규칙 (P0 반영)**:
  - `conversationId`를 생략하면 서버가 안전한 신규 ID를 발급합니다.
  - 클라이언트가 `conversationId`를 지정한 경우, 서버는 **이력 조회 및 LLM 호출 전에 반드시 `chat_conversations`의 소유자(`user_uuid`)를 먼저 검증**합니다.
  - 다른 학생 소유의 `conversationId`이거나 동시 생성 충돌 시 후속 조회·기록·프롬프트 생성을 즉시 중단하고 **`403 FORBIDDEN`**을 반환합니다.
  - 소유권 확인 중 DB 오류가 발생하면 무시하고 진행하지 않고 즉시 **`500 AI_CONVERSATION_VERIFY_ERROR`**로 중단합니다.
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "reply": "현재 전공 영역은 30점으로 달성 기준을 충족했으나, 외국어 영역이 10점 부족합니다...",
      "conversationId": "conv_student_101_01",
      "studentSummary": {
        "totalScore": 62.0,
        "certStatus": "보완 필요"
      },
      "analysisSource": "gemini",
      "fallbackUsed": false
    },
    "error": null
  }
  ```

### 8.4 [교사용] 증빙자료 심사 보조 및 검토의견 초안 Copilot
- **Endpoint**: `POST /api/ai/teacher/review-draft`
- **Auth**: 교사(`teacher`) 또는 관리자(`admin`) 전용 (학생 호출 시 `403 FORBIDDEN`)
- **제출물별 인가 검증 (P1 반영)**:
  - `submissionId`가 포함된 경우, 조회 및 분석 전에 `authorize_submission_access`를 통해 해당 교사가 그 제출물에 대한 열람/심사 권한이 있는지 먼저 검증합니다 (권한 없는 타 학급·타 교과 제출물 ID 지정 시 `403 FORBIDDEN`).
  - **초안 생성 전용**: 이 API는 검토 초안만 반환하며, 실제 제출물의 승인·반려 상태나 점수를 자동 변경하지 않습니다.
- **Request Body**:
  ```json
  {
    "submissionId": 501,
    "area": "전공 영역",
    "itemName": "교외 해커톤 대회 수상",
    "detail": "제10회 SW 마이스터고 연합 해커톤 대상",
    "description": "팀 프로젝트 백엔드 개발 담당, 상장 사본 첨부",
    "maxScore": 20.0
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "submissionId": 501,
      "status": "인정완료",
      "recommendedStatus": "인정완료",
      "recommendedScore": 20.0,
      "confidence": 0.88,
      "teacherCommentDraft": "연합 해커톤 대상 수상 실적과 상장 증빙이 평가 기준에 부합합니다. 수상을 축하하며 20점 인정합니다.",
      "guidelineSummary": "교외 공인 대회 수상 여부 및 상장 직인/수상자 성명 일치 여부를 최종 확인하세요.",
      "evidencePresent": true,
      "analysisSource": "gemini",
      "fallbackUsed": false,
      "capabilityMode": "live_llm"
    },
    "error": null
  }
  ```
  > 증빙 파일(`file_path`)과 외부 링크(`link_url`)가 모두 없고 설명이 부실한 제출물에 대해서는 무조건 만점/인정완료를 반환하지 않고 `"재제출요청"` 또는 `"반려"` 권고와 낮은 점수를 반환합니다.

### 8.5 [교사용] 학급 지도 Copilot 챗봇
- **Endpoint**: `POST /api/ai/teacher/chat`
- **Auth**: 교사(`teacher`) 또는 관리자(`admin`) 전용 (학생 호출 시 `403 FORBIDDEN`)
- **학급/학년 RAG 범위 격리**:
  - 담임교사: 본인 담당 학급(`grade`, `class_no`) 데이터만 RAG 주입 (타 학급 요청 시 `403 FORBIDDEN`)
  - 학년부장: 본인 담당 학년 데이터만 RAG 주입
  - 총괄부서장 / 관리자: 요청한 `grade`, `classNo` 또는 전교생 데이터 RAG 주입
- **Request Body**:
  ```json
  {
    "message": "우리 반에서 졸업 인증 보완이 시급한 학생과 지도 방향을 요약해줘.",
    "conversationId": "conv_teacher_11_01",
    "grade": 2,
    "classNo": 1
  }
  ```

### 8.6 [관리자용] 전교생 인증 통계 분석 및 총괄 리포트 생성
- **Endpoint**: `POST /api/ai/admin/summary-report`
- **Auth**: 관리자(`admin`) 전용 (학생/일반교사 호출 시 `403 FORBIDDEN`)
- **Request Body**:
  ```json
  {
    "year": 2026,
    "grade": null
  }
  ```
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "year": 2026,
      "status": "COMPLETED",
      "reportMarkdown": "## 2026학년도 역량인증제 운영 총괄 분석 보고서\n...",
      "summaryReport": "## 2026학년도 역량인증제 운영 총괄 분석 보고서\n...",
      "kpi": {
        "totalStudents": 192,
        "certifiableCount": 134,
        "certifiableRate": 69.79,
        "averageTotalScore": 71.2
      },
      "areaStats": [ ... ],
      "recommendations": [
        "외국어 영역의 평균 달성률이 타 영역 대비 낮으므로 방과후 토익/오픽 프로그램 안내 강화가 필요합니다."
      ],
      "analysisSource": "gemini",
      "fallbackUsed": false,
      "capabilityMode": "live_llm"
    },
    "error": null
  }
  ```
  > 해당 학년도에 학생/제출 데이터가 전혀 없는 경우 `status: "EMPTY_DATA"`를 반환하여 정상 분석 완료와 빈 학년도를 명확히 구분합니다.

### 8.7 [관리자용] 부정 의심 제출 및 이상 징후 감사 (Anomaly Detection)
- **Endpoint**: `POST /api/ai/admin/audit-anomalies`
- **Auth**: 관리자(`admin`) 전용 (학생/일반교사 호출 시 `403 FORBIDDEN`)
- **Request Body**:
  ```json
  {
    "year": 2026,
    "threshold": 0.4,
    "limit": 50
  }
  ```
- **탐지 규칙 (5대 감사 룰 + LLM 교차 분석)**:
  1. `SCORE_EXCEEDS_MAX`: 배점 한도 초과 또는 음수 점수 부여 건 (`riskScore: 0.95`)
  2. `INACTIVE_ITEM_APPROVED`: 비활성 평가 항목에 점수가 승인된 건 (`riskScore: 0.85`)
  3. `DUPLICATE_SUBMISSION`: 동일 학생·동일 항목에 동일한 설명으로 중복 승인된 건 (`riskScore: 0.80`)
  4. `RAPID_APPROVAL`: 제출 후 60초 이내에 초고속 승인된 건 (`riskScore: 0.65`)
  5. `EXCESSIVE_MERITS`: 단기/누적 과다 상벌점(합산 30점 이상 또는 10회 이상) 집중 부여 건 (`riskScore: 0.70`)
- **Response (`200 OK`)**:
  ```json
  {
    "status_code": 200,
    "success": true,
    "data": {
      "year": 2026,
      "status": "COMPLETED",
      "overallStatus": "WARNING",
      "riskScore": 0.8,
      "anomalyCount": 2,
      "anomalies": [
        {
          "issueType": "DUPLICATE_SUBMISSION",
          "targetType": "submission",
          "submissionId": 512,
          "studentId": 104,
          "studentName": "이개발",
          "riskScore": 0.8,
          "reason": "동일 평가항목(봉사활동)에 동일한 상세 설명으로 2건 중복 승인됨"
        }
      ],
      "auditAnalysis": "감사 결과 총 2건의 이상 징후가 탐지되었습니다...",
      "summary": "감사 결과 총 2건의 이상 징후가 탐지되었습니다...",
      "auditedCounts": {
        "submissionsScanned": 420,
        "meritGroupsScanned": 85
      },
      "analysisSource": "gemini",
      "fallbackUsed": false,
      "capabilityMode": "live_llm"
    },
    "error": null
  }
  ```
  > **상태 구분 보장**:
  > - `status`: `"COMPLETED"` (실제 스캔 완료) vs `"EMPTY_DATA"` (해당 학년도에 검사할 제출/상벌점 데이터가 0건임)
  > - `overallStatus`: `"NORMAL"` (이상 징후 0건 또는 위험도 낮음) / `"WARNING"` (`0.5 <= riskScore < 0.8`) / `"CRITICAL"` (`riskScore >= 0.8`)
