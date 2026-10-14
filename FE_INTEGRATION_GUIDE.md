# 📱 마이스터 역량인증제 프론트엔드 연동 가이드 (FE Integration Guide)

> **문서 버전**: v1.0  
> **최종 갱신 일자**: 2026-10-14  
> **대상 시스템**: 부산소프트웨어마이스터고등학교 마이스터 역량인증제 시스템 (`meister-backend`)  
> **기준 백엔드 커밋/버전**: v1.4.0 (360개 단위/통합 회귀 테스트 100% 검증 완료)  
> **문서 목적**: Next.js / React 프론트엔드 개발팀이 백엔드 REST API 및 인가 정책, DTO 규약, AI 서비스를 안전하고 정확하게 연동하기 위한 단일 기준 명세서.

---

## 📌 목차
1. [교무분장 12대 사용자 결정사항 기반 차등 인가 매트릭스](#1-교무분장-12대-사용자-결정사항-기반-차등-인가-매트릭스)
2. [핵심 3대 식별자(ID) 구분 및 사용 규약](#2-핵심-3대-식별자id-구분-및-사용-규약)
3. [공통 응답 봉투(SrFormat) 및 에러 핸들링 가이드](#3-공통-응답-봉투srformat-및-에러-핸들링-가이드)
4. [보안 파일 처리 및 1회용 다운로드 티켓 플로우](#4-보안-파일-처리-및-1회용-다운로드-티켓-플로우)
5. [상벌점(Points) API DTO 및 부호 규약](#5-상벌점points-api-dto-및-부호-규약)
6. [AI 서비스 연동 엔드포인트 및 메타데이터 응답 규약](#6-ai-서비스-연동-엔드포인트-및-메타데이터-응답-규약)
7. [심사 후 studentTotals 처리 및 Fallback Refetch 패턴](#7-심사-후-studenttotals-처리-및-fallback-refetch-패턴)
8. [연동 체크리스트 요약 (Quick Reference)](#8-연동-체크리스트-요약-quick-reference)

---

## 1. 교무분장 12대 사용자 결정사항 기반 차등 인가 매트릭스

본 시스템은 부산소프트웨어마이스터고등학교의 실제 교무분장 조직(담임, 학년부, 전문교과/보통교과, 총괄부서 등)을 충실히 반영하여 세밀한 RBAC(Role-Based Access Control) 인가를 적용하고 있습니다.

### 1.1 사용자 역할 정의
- **학생 본인 (`student`, role=0)**: 본인의 역량인증 현황 조회, 증빙자료 신규 제출/미승인건 재제출/삭제, 본인 상벌점 조회, 학생 전용 AI 이용.
- **정·부·임시 담임교사 (`teacher`, role=1, grade & class 배정)**: 담당 학급(우리 반) 학생에 대한 종합 관리.
- **학년부장 (`teacher`, role=1, position='학년부장', grade 배정)**: 담당 학년(우리 학년) 전체 학급 학생에 대한 총괄 관리.
- **비담임 교과·부서 교사 (`teacher`, role=1, grade & class 미배정 또는 교과/부서 배정)**: 담당 교과(subject) 또는 소속 부서(department) 소관 영역 평가.
- **1순위 전담 평가자 (`teacher`, role=1, `evaluation_item_teachers` N:M 매핑)**: 특정 자격증/대회 항목에 1순위로 지정된 전문 평가자.
- **총괄부서장 (`teacher`, role=1, position='부서부장' AND department IN ('마이스터부', '교무기획부'))**: 학교 전체 전교생 모든 항목 심사/열람 전면 허용.
- **최고 관리자 (`admin`, role=2)**: 시스템 설정, 학년도/평가기준 CRUD, 감사로그, 스토리지 GC, 전교생 데이터 전면 허용.

---

### 1.2 교무분장 12대 결정사항(의심-01 ~ 의심-12) 반영 인가 매트릭스

| 대상 리소스 및 행위 | HTTP 엔드포인트 | 학생 본인 | 담임교사 (우리 반) | 학년부장 (우리 학년) | 비담임 교과·부서 교사 | 1순위 전담 평가자 | 총괄부서장 (마이스터/교무) | 관리자 (Admin) | 적용 결정사항 및 주의사항 |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **인증 종합 현황 열람** | `GET /api/students/{id}/certification-status` | **O** | **O** | **O** | **O** | **O** | **O** | **O** | **[의심-02 B안]** 모든 교사에게 전교생 인증 현황 Read 허용 |
| **제출 목록 조회** | `GET /api/students/{id}/submissions` | **O** | **O** (전체) | **O** (전체) | **O** (담당과제 필터, 0건시 `[]`) | **O** (지정과제) | **O** (전체) | **O** (전체) | **[의심-03 B안]** 타반/비담임 조회 시 담당 과제 필터링, 0건이어도 200 OK 빈 배열 반환 |
| **제출 단건 상세 조회** | `GET /api/submissions/{id}` | **O** | **O** | **O** | **O** (참고 열람) | **O** | **O** | **O** | **[의심-02 B안]** 모든 교사에게 전교생 증빙 Read 허용 |
| **증빙 일회용 티켓 발급** | `POST /api/submissions/{id}/download-ticket`<br>`POST /api/uploads/ticket?file_path=` | **O** | **O** | **O** | **O** (열람용) | **O** | **O** | **O** | **[의심-02 B안]** 단건 상세 열람 권한자와 동일하게 티켓 발급 허용 |
| **증빙 원본 파일 다운로드** | `GET /uploads/{path}?ticket={ticket}` | **O** | **O** | **O** | **O** | **O** | **O** | **O** | Redis 1회 원자적 소비(GETDEL) 검증 통과 시 스트리밍 서빙 |
| **신규 증빙 제출** | `POST /api/submissions` | **O** | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | 학생 본인 전용 (활성 학년도 필수) |
| **증빙 재제출 (보완)** | `PATCH /api/submissions/{id}/resubmit` | **O** (미승인건) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | 인정완료(승인)건은 재제출 불가 |
| **제출물 삭제** | `DELETE /api/submissions/{id}` | **O** (미승인건) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **O** (승인건 포함) | **[의심-10 A안]** 교사는 직접 삭제 불가(403). 오직 학생 본인(미승인) 또는 관리자만 가능 |
| **1순위 전담과제 심사** | `PATCH /api/submissions/{id}/approve`, `reject` | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **O** (최우선) | **O** | **O** | **[의심-01 B안]** 전담교사 지정 건은 담임/학년부장이어도 심사 불가 (오직 전담교사/총괄/관리자) |
| **2·3순위 교과/부서과제 심사**| `PATCH /api/submissions/{id}/approve`, `reject` | **X** (403) | **X** (403) | **X** (403) | **O** (해당 교과/부서) | - | **O** | **O** | **[의심-01/06 B안]** 소속 부서/담당 교과 일치 교사 및 부서장만 심사 가능 |
| **일반 공통과제 심사** | `PATCH /api/submissions/{id}/approve`, `reject` | **X** (403) | **O** (우리 반) | **O** (우리 학년) | **X** (403) | - | **O** | **O** | **[의심-07 B안]** 전담/교과/부서 미지정 일반 과제는 담임/학년부장/총괄부서장/관리자만 심사 |
| **점수 사후 수정** | `PATCH /api/submissions/{id}/score` | **X** (403) | 심사 권한과 동일 | 심사 권한과 동일 | 심사 권한과 동일 | 심사 권한과 동일 | **O** | **O** | 심사(승인) 권한이 있는 교사/총괄부서장/관리자만 수정 가능 |
| **상벌점 신규 부여** | `POST /api/points` | **X** (403) | **O** | **O** | **O** | **O** | **O** | **O** | **[의심-04 B안]** 모든 교사가 전교생 상벌점 부여 가능 |
| **상벌점 수정 / 삭제** | `PATCH`/`DELETE /api/points/{pointId}` | **X** (403) | **O** (부여자/우리반) | **O** (부여자/우리학년) | **O** (본인 부여자 건만) | **O** (본인 부여자 건만) | **O** (마이스터/생활부) | **O** | **[의심-04 B안]** 본인 부여자 + 담임 + 학년부장 + 학생생활안전부 교사 + 관리자만 허용 |
| **학생 AI (분류/챗봇)** | `POST /api/ai/classify-submission`, `/chat` | **O** | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **O** (테스트 허용) | **[의심-12 B안]** 학생 전용. 교사 계정 호출 시 403 `FORBIDDEN_ROLE` 차단 |
| **교사 AI (심사초안)** | `POST /api/ai/teacher/review-draft` | **X** (403) | **O** (심사가능 과제) | **O** (심사가능 과제) | **O** (담당 과제) | **O** (전담 과제) | **O** (전 과제) | **O** | **[의심-09 B안]** 실제 해당 제출물을 승인/반려할 권한이 있는 교사만 호출 가능 |
| **교사 AI (학급챗봇)** | `POST /api/ai/teacher/chat` | **X** (403) | **O** (우리 반) | **O** (우리 학년) | **O** (지정 학년/반 질의) | **O** | **O** (전교생) | **O** | **[의심-08 B안]** 비담임 교사도 학년/반 지정 또는 담당 영역 기반 질의 허용 |
| **관리자 행정 기능** | `/api/admin/*`, `/api/criteria/*` | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **X** (403) | **O** | **[의심-05 A안]** 학년도/평가기준/통계엑셀/감사로그는 오직 Admin(role=2)만 가능 |

---

## 2. 핵심 3대 식별자(ID) 구분 및 사용 규약

프론트엔드 연동 시 가장 빈번하게 발생하는 오류는 서로 다른 성격의 식별자를 혼동하여 잘못된 파라미터로 전달하는 경우입니다. 반드시 아래 3대 식별자를 구분하여 호출하십시오.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        3대 식별자 체계 및 매핑 관계                     │
├──────────────────────┬──────────────────────┬──────────────────────────┤
│ 1. 로그인 ID          │ 2. 유저 고유키 (UUID) │ 3. 도메인 고유 PK (AutoInc)│
│ (users.id)           │ (users.uuid)         │ (students.student_id /   │
│                      │                      │  teachers.teachers_id)   │
├──────────────────────┼──────────────────────┼──────────────────────────┤
│ - "student01"        │ - "550e8400-e29b-..."│ - 101 (정수형 숫자)       │
│ - 로그인 폼 입력값     │ - JWT 'sub' 클레임    │ - REST API Path 파라미터   │
│ - username 필드      │ - 감사 로그 작성자     │ - DB 외래키(FK) 참조 대상  │
└──────────────────────┴──────────────────────┴──────────────────────────┘
```

### 2.1 식별자별 상세 명세

| 식별자 명칭 | DB 컬럼 | 타입 | 예시값 | 주 사용처 및 FE 바인딩 위치 |
| :--- | :--- | :---: | :--- | :--- |
| **로그인 ID** (`id` / `username`) | `users.id` | `VARCHAR(50)` | `"student01"`, `"teacher_kim"` | `POST /api/auth/login`의 `username` 입력값. 화면 상단 프로필/헤더에 사용자 아이디 표시 시 사용. (정수 아님!) |
| **유저 고유 UUID** (`uuid`) | `users.uuid` | `VARCHAR(36)` | `"8b2a1e04-7a31-4a1e-8e4d-..."` | JWT 토큰의 `sub` 클레임 값. AI 챗봇 세션 소유자 식별자(`user_uuid`). |
| **학적 고유 ID** (`studentId`) | `students.student_id` | `INT` (정수) | `101`, `205` | **모든 학생 관련 REST API URL 경로 변수 (`/api/students/{student_id}/*`)**, 상벌점 부여 대상 (`studentId`). |
| **교원 고유 ID** (`teacherId`) | `teachers.teachers_id`| `INT` (정수) | `1`, `15` | 평가 항목별 전담교사 배정 (`teacherIds`), 심사자 식별 (`reviewerId`). |

> [!WARNING] **FE 필수 주의사항 (IDOR 및 404/422 차단)**
> - `/api/students/{student_id}/certification-status` 또는 `/api/students/{student_id}/submissions` 호출 시 **절대 로그인 아이디 문자열(`"student01"`)이나 `users.uuid`를 넣지 마십시오.**
> - `POST /api/auth/login` 또는 `GET /api/auth/me` 응답의 `data.user` 객체는 다음과 같이 구성되어 있습니다:
>   - **`data.user.id`**: 문자열 로그인 아이디 (`"student01"`) $\rightarrow$ **API 경로 파라미터로 사용 금지!**
>   - **`data.user.studentId`**: 정수형 학적 PK (`101`) $\rightarrow$ **`/api/students/{studentId}/*`에 바인딩할 값!**
>   - **`data.user.teacherId`**: 정수형 교원 PK (`15`, 교사인 경우) $\rightarrow$ **교사 식별용 PK!**
> - 프론트엔드 전역 상태(Zustand / Redux / Pinia)에 `studentId` 및 `teacherId`를 별도로 저장하여 API 호출 파라미터로 사용해야 합니다.

---

## 3. 공통 응답 봉투(SrFormat) 및 에러 핸들링 가이드

파일 다운로드 바이너리 스트림을 제외한 모든 API 응답은 백엔드의 표준 봉투인 `SrFormat` 구조로 래핑되어 반환됩니다.

### 3.1 표준 응답 구조 (`TypeScript Interface`)

```typescript
export interface SrFormat<T = any> {
  status_code: number; // HTTP 상태 코드와 동일 (200, 201, 400, 403, 404, 409, 429, 500 등)
  success: boolean;    // 요청 성공 여부 (성공 시 true, 실패 시 false)
  data: T | null;      // 성공 시 데이터 객체/배열, 실패 시 null
  error: {
    code: string;      // 표준화된 에러 코드 (예: 'FORBIDDEN', 'VALIDATION_ERROR')
    message: string;   // 사용자/개발자 안내 메시지 (한국어)
  } | null;
}
```

---

### 3.2 HTTP 상태 코드 및 FE 에러 대응 매핑표

| HTTP Status | `error.code` | 원인 및 FE 추천 처리 액션 |
| :---: | :--- | :--- |
| **`400`** | `VALIDATION_ERROR`<br>`INVALID_SCORE`<br>`INVALID_POINT_SIGN`<br>`INACTIVE_YEAR` | **폼 유효성 검증 실패**: `error.message`를 해당 폼 필드 아래에 에러 텍스트나 토스트로 표시하십시오. |
| **`401`** | `UNAUTHORIZED`<br>`TOKEN_EXPIRED`<br>`INVALID_TICKET` | **인증 만료/실패**: 클라이언트의 인증 토큰을 초기화하고 로그인 페이지(`/login`)로 리다이렉트하십시오. |
| **`403`** | `FORBIDDEN`<br>`FORBIDDEN_ROLE` | **권한 부족**: 교무분장 인가 매트릭스에 따른 차단입니다. "접근 권한이 없습니다" 경고 토스트를 노출하고 이전 화면으로 이동합니다. |
| **`404`** | `NOT_FOUND`<br>`ITEM_NOT_FOUND`<br>`USER_NOT_FOUND` | **리소스 미존재**: 대상 학생, 제출물, 항목이 삭제되었거나 존재하지 않습니다. 목록 페이지를 새로고침(Refetch)하십시오. |
| **`409`** | `CONFLICT` | **동시성 충돌**: 이미 다른 교사가 승인했거나, 학생이 미승인 상태에서 삭제한 제출물을 심사하려 할 때 발생합니다. **즉시 최신 상태를 재조회(Refetch)하여 UI를 갱신하십시오.** |
| **`429`** | `RATE_LIMIT_EXCEEDED` | **호출 한도 초과**: 로그인 5회 실패 또는 AI 호출 분당 한도 초과 시 발생합니다. `error.message`에 명시된 대기 시간(초) 후 재시도하도록 안내하십시오. |
| **`500`** | `INTERNAL_SERVER_ERROR`<br>`AI_CONVERSATION_VERIFY_ERROR` | **서버 내부 오류**: 일시적 장애 안내 팝업을 노출하고 시스템 관리자에게 문의하도록 유도하십시오. |

---

## 4. 보안 파일 처리 및 1회용 다운로드 티켓 플로우

본 시스템은 학생의 개인정보 및 증빙자료 보안을 위해 파일 경로를 직접 공개 서빙하지 않고, **14종 바이너리 매직바이트 검증**과 **단 1회만 소비되는 일회용 티켓(One-Time Ticket)**을 통해 안전하게 중계합니다.

### 4.1 연동 시퀀스 다이어그램

```
[ FE 브라우저 ]               [ Backend API ]               [ Redis 캐시 ]
      │                             │                             │
      │ 1. 증빙 업로드 (multipart)   │                             │
      ├────────────────────────────>│ (매직바이트/50MB 검증)        │
      │                             │ UUIDv4 변환 로컬 디스크 저장  │
      │ 2. 업로드 완료 및 제출 등록   │                             │
      │<────────────────────────────┤                             │
      │                             │                             │
      │ 3. 다운로드 티켓 발급 요청    │                             │
      │    POST /api/submissions/   │                             │
      │    {id}/download-ticket     │ (RBAC 인가 검사)             │
      ├────────────────────────────>│                             │
      │                             │ 4. 일회용 티켓 발행 (TTL 60s)│
      │                             ├────────────────────────────>│
      │ 5. 티켓 반환 (ticket UUID)   │                             │
      │<────────────────────────────┤                             │
      │                             │                             │
      │ 6. 실제 파일 다운로드 요청    │                             │
      │    GET /uploads/{path}      │ 7. 티켓 원자적 단일 소비     │
      │        ?ticket={ticket}     │    (Redis GETDEL)           │
      ├────────────────────────────>├────────────────────────────>│ (즉시 삭제됨)
      │ 8. 파일 바이너리 스트림 서빙 │                             │
      │<────────────────────────────┤                             │
      │                             │                             │
      │ 9. [보안] 동일 티켓 재요청   │                             │
      │    GET /uploads/{path}      │ 10. 티켓 부재 (이미 소멸)    │
      ├────────────────────────────>├────────────────────────────>│ (Key 없음)
      │ 11. 401 UNAUTHORIZED 차단   │                             │
      │<────────────────────────────┤                             │
```

### 4.2 단계별 프론트엔드 구현 코드 예시

#### Step 1: 증빙자료 업로드 (`multipart/form-data`)
- **허용 확장자 (14종)**: `pdf`, `png`, `jpg`, `jpeg`, `gif`, `webp`, `zip`, `hwp`, `hwpx`, `doc`, `docx`, `xls`, `xlsx`, `csv`
- **단일 파일 용량 제한**: **최대 50MB**

```typescript
const uploadSubmission = async (itemId: number, detail: string, file: File) => {
  const formData = new FormData();
  formData.append('item_id', String(itemId));
  formData.append('detail', detail);
  formData.append('file', file);

  const res = await fetch('/api/submissions', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
    body: formData,
  });
  return await res.json();
};
```

#### Step 2: 일회용 다운로드 티켓 발급 (2가지 방식 지원)
- **티켓 유효시간**: 발급 후 **60초 (TTL 60s)**
- **단일 소비(Single-Use)**: 다운로드 시작 즉시 백엔드 Redis에서 `GETDEL`로 삭제되므로 재사용이 불가능합니다. (보안 격리)

##### 방식 A. 제출 ID 기준 발급 (권장):
제출 상세/목록 화면에서 특정 제출건의 첨부파일을 다운로드할 때 사용합니다.
- **엔드포인트**: `POST /api/submissions/{submissionId}/download-ticket`
- **인가 정책**: 학생 본인, 담임/부장/교과/부서 교사(의심-02 열람 허용), 1순위 평가자, 총괄부서장, 관리자

##### 방식 B. 파일 경로 기준 직접 발급:
프론트엔드가 이미지 미리보기 또는 파일 경로(`/uploads/...` 또는 상대경로)만 직접 알고 있을 때 사용합니다.
- **엔드포인트**: `POST /api/uploads/ticket?file_path=/uploads/2026/101/cert_501.pdf`
- **인가 정책**: 대상 파일이 연결된 제출건의 RBAC 인가 규칙을 동일하게 자동 검증

```typescript
// [방식 A] 제출 ID 기준 다운로드 헬퍼
const downloadBySubmissionId = async (submissionId: number) => {
  const res = await fetch(`/api/submissions/${submissionId}/download-ticket`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  const json: SrFormat<{ ticket: string; downloadUrl: string; signedToken: string }> = await res.json();
  if (!json.success || !json.data) {
    alert(json.error?.message || '다운로드 티켓 발급 실패');
    return;
  }

  // 브라우저 팝업/다운로드 트리거 (downloadUrl에 이미 ?ticket= 파라미터 포함)
  triggerFileDownload(json.data.downloadUrl);
};

// [방식 B] 파일 경로 기준 다운로드 헬퍼
const downloadByFilePath = async (filePath: string) => {
  const queryPath = encodeURIComponent(filePath);
  const res = await fetch(`/api/uploads/ticket?file_path=${queryPath}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  const json: SrFormat<{ ticket: string; downloadUrl: string; signedToken: string }> = await res.json();
  if (!json.success || !json.data) {
    alert(json.error?.message || '다운로드 티켓 발급 실패');
    return;
  }

  triggerFileDownload(json.data.downloadUrl);
};

const triggerFileDownload = (downloadUrl: string) => {
  const link = document.createElement('a');
  link.href = downloadUrl; // 예: /uploads/abc-123.pdf?ticket=9f8c...
  link.target = '_blank';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
};
```

---

## 5. 상벌점(Points) API DTO 및 부호 규약

상벌점 관리 API는 프론트엔드의 다양한 입력 방식(절댓값 입력 vs 부호 포함 입력)을 모두 유연하게 수용할 수 있도록 하위 호환성을 제공합니다.

### 5.1 점수 필드 명칭 호환성
- 백엔드는 요청 본문(Request Body)의 **`points`** 와 **`score`** 필드를 모두 동일한 점수로 인식합니다.
- 점수 절댓값 허용 범위: **`0 < |점수| <= 100`** (소수점 2자리까지 지원).

### 5.2 부호(Sign) 엄격 검증 규칙
1. **상점 (`type="상점"` 또는 `"MERIT"`)**:
   - `points: 5` (양수) $\rightarrow$ 정상 반영 (`+5.00`)
   - `score: 5` (양수) $\rightarrow$ 정상 반영 (`+5.00`)
   - `score: -5` (음수) 전송 시 $\rightarrow$ **`400 INVALID_POINT_SIGN` 에러 발생**
2. **벌점 (`type="벌점"` 또는 `"DEMERIT"`)**:
   - `score: -3` (음수) $\rightarrow$ 정상 반영 (`-3.00`)
   - `points: 3` (양수 절댓값) 전송 시 $\rightarrow$ 백엔드가 자동으로 음수(`-3.00`)로 정규화 저장
   - `score: +3` (양수) 전송 시 $\rightarrow$ **`400 INVALID_POINT_SIGN` 에러 발생**

### 5.3 상벌점 부여 DTO 예시 (`POST /api/points`)

```json
// 벌점 부여 예시 (score 음수 직접 전달 방식 권장)
{
  "studentId": 101,
  "type": "벌점",
  "score": -2.0,
  "reason": "기숙사 소등 시간 이후 전자기기 무단 사용",
  "occurredAt": "2026-10-14",
  "relatedArea": "인성/직업의식"
}
```

### 5.4 상벌점 수정 DTO 예시 (`PATCH /api/points/{pointId}`)
- 상벌점 수정 시에도 `score` 또는 `points` 필드를 사용할 수 있습니다.

```json
{
  "score": -1.0,
  "reason": "소명 자료 확인 후 벌점 1점 감경"
}
```

---

## 6. AI 서비스 연동 엔드포인트 및 메타데이터 응답 규약

마이스터 역량인증제는 Multi-Provider AI 아키텍처를 지원하며, AI 공급자(Gemini, OpenAI, Claude, Local LLM)의 응답 상태를 표준 메타데이터 필드로 프론트엔드에 전달합니다.

### 6.1 엔드포인트별 인가 및 역할 명세

| 엔드포인트 | Method | 대상 사용자 | 주 기능 | FE 권한 가드 |
| :--- | :---: | :---: | :--- | :--- |
| `/api/ai/classify-submission` | `POST` | 학생 (본인), 관리자 | 증빙자료 작성 시 평가 영역/항목 자동 추천 | 교사 호출 시 `403 FORBIDDEN_ROLE` |
| `/api/ai/student/chat` | `POST` | 학생 (본인), 관리자 | 학생 맞춤형 역량 달성 도우미 챗봇 (미달성 분석) | 교사 호출 시 `403 FORBIDDEN_ROLE` |
| `/api/ai/teacher/review-draft` | `POST` | 심사 권한 교사, 관리자 | 증빙자료 심사 초안(인정/반려, 점수, 종합의견) 생성 | 심사 권한 없을 시 `403 FORBIDDEN` |
| `/api/ai/teacher/chat` | `POST` | 모든 교사, 관리자 | 학급별 역량 달성률 질의 및 학생 지도 챗봇 | 학생 호출 시 `403 FORBIDDEN_ROLE` |
| `/api/ai/admin/summary-report`| `POST` | 최고 관리자 (`admin`) | 전교생 영역별 이수율 종합 통계 보고서 생성 | 교사/학생 호출 시 `403 FORBIDDEN` |
| `/api/ai/admin/audit-anomalies`| `POST` | 최고 관리자 (`admin`) | 동일 파일 중복 제출, 점수 급변 등 부정 의심 탐지 | 교사/학생 호출 시 `403 FORBIDDEN` |

---

### 6.2 표준 AI 메타데이터 응답 구조

모든 AI 엔드포인트는 `data` 객체 내에 아래 공통 메타데이터를 포함하여 반환합니다:

```typescript
export interface AIMetaResponse {
  analysisSource: 'gemini' | 'openai' | 'claude' | 'openai_compatible' | 'rule_based_fallback';
  fallbackUsed: boolean;       // AI 호출 실패/타임아웃으로 규칙 기반 엔진이 작동했는지 여부
  fallbackReason: string | null; // 폴백 작동 원인 (예: "GEMINI_QUOTA_EXCEEDED", "TIMEOUT_5S")
  capabilityMode: 'llm_agent' | 'rule_based_engine';
  model?: string;              // 실제 사용된 모델명 (예: "gemini-3.8-flash", "gpt-4o-mini")
}
```

#### FE UI 렌더링 가이드:
- `fallbackUsed === true` 일 때: UI 상단에 작게 *"안정적인 학사 운영을 위해 내장 규칙 엔진 기반으로 분석되었습니다."* 라는 안내 배지(Badge)를 표시하면 좋습니다.
- 에러가 발생해도 백엔드가 5초 내에 무중단 Fallback 결과를 반환하므로, 프론트엔드에서 로딩 스피너가 영구히 멈추는 현상이 발생하지 않습니다.

---

### 6.3 주요 AI 서비스별 요청/응답 DTO 규약

#### (1) 학생 증빙 자동 분류 및 항목 추천 (`POST /api/ai/classify-submission`)
- **호출 권한**: 학생 본인, 관리자 (교사 호출 시 403 `FORBIDDEN_ROLE`)
- **Request Body**:
  ```json
  {
    "detail": "정보처리기능사 필기 및 실기 최종 합격증 제출",
    "description": "2026년 정기 기능사 1회 정보처리기능사 취득 증빙입니다."
  }
  ```
- **Response (`data` 객체)**:
  ```json
  {
    "area": "전문기술역량",
    "itemId": 1,
    "itemName": "국가기술자격증(기능사/산업기사) 취득",
    "confidence": 0.95,
    "reason": "정보처리기능사 자격증 취득은 전문기술역량의 국가기술자격증 항목에 해당합니다.",
    "suggestions": [
      {
        "area": "전문기술역량",
        "itemId": 1,
        "itemName": "국가기술자격증(기능사/산업기사) 취득",
        "confidence": 0.95,
        "reason": "..."
      }
    ],
    "analysisSource": "gemini",
    "fallbackUsed": false,
    "fallbackReason": null,
    "capabilityMode": "llm_agent",
    "model": "gemini-3.8-flash"
  }
  ```

#### (2) 학생 맞춤형 역량 챗봇 (`POST /api/ai/student/chat` 및 별칭 `/api/ai/chat`)
- **호출 권한**: 학생 본인, 관리자 (교사 호출 시 403 `FORBIDDEN_ROLE`)
- **Request Body**:
  ```json
  {
    "message": "인성 영역 점수가 부족한데 어떤 활동을 하면 채울 수 있어?",
    "conversationId": "conv-a1b2c3d4e5f6..."
  }
  ```
- **Response (`data` 객체)**:
  ```json
  {
    "reply": "현재 인성/직업의식 영역에서 12점을 취득하여 기준(15점)까지 3점이 남았습니다. 교내 봉사활동(10시간 이상) 또는 학급 자치활동에 참여하시면 5점을 추가 취득할 수 있습니다.",
    "conversationId": "conv-a1b2c3d4e5f6...",
    "suggestions": [
      "내 역량인증 취득 총점 확인",
      "상벌점 반영 기준 안내",
      "반려된 증빙자료 보완 방법"
    ],
    "analysisSource": "gemini",
    "fallbackUsed": false,
    "capabilityMode": "llm_agent"
  }
  ```

#### (3) 교사용 심사 Copilot 초안 생성 (`POST /api/ai/teacher/review-draft`)
- **호출 권한**: 해당 제출물 심사 권한 교사, 관리자 (권한 없을 시 403 `FORBIDDEN`)
- **Request Body**:
  ```json
  {
    "submissionId": 123,
    "area": "전문기술역량",
    "itemName": "국가기술자격증(기능사/산업기사) 취득",
    "detail": "정보처리기능사 취득",
    "description": "한국산업인력공단 발행 확인서 첨부합니다.",
    "criteria": "기능사 취득 시 10점 부여",
    "maxScore": 10.0
  }
  ```
- **Response (`data` 객체)**:
  ```json
  {
    "submissionId": 123,
    "status": "APPROVED",
    "recommendedStatus": "APPROVED",
    "score": 10.0,
    "recommendedScore": 10.0,
    "teacherCommentDraft": "한국산업인력공단 발행 자격증 사본을 확인하였으며, 전문기술역량 기준에 부합하여 10점을 인정합니다.",
    "draftComment": "한국산업인력공단 발행 자격증 사본을 확인하였으며, 전문기술역량 기준에 부합하여 10점을 인정합니다.",
    "guidelineSummary": "전문기술역량 (국가기술자격증 취득) - 평가 기준 부합 및 최대 배점(10.0점) 인정 요건 충족",
    "checkPoints": [
      "제출 서류의 진위 여부 확인",
      "취득일자 및 공인기관 직인 확인"
    ],
    "confidence": 0.95,
    "analysisSource": "gemini",
    "fallbackUsed": false
  }
  ```

#### (4) 교사용 학급 지도 Copilot 챗봇 (`POST /api/ai/teacher/chat`)
- **호출 권한**: 모든 교사, 관리자 (학생 호출 시 403 `FORBIDDEN_ROLE`)
- **Request Body**:
  ```json
  {
    "message": "우리 반에서 외국어 능력이 취약한 학생 명단과 추천 지도방안 알려줘",
    "conversationId": "conv-teacher-001",
    "grade": 1,
    "classNo": 2
  }
  ```
- **Response (`data` 객체)**:
  ```json
  {
    "reply": "1학년 2반 총 18명 중 3명의 학생이 외국어 영역 이수율 40% 미만입니다. TOEIC Bridge 또는 방과후 영어 회화 스피치 참여를 지도하십시오.",
    "conversationId": "conv-teacher-001",
    "scope": {
      "grade": 1,
      "classNo": 2,
      "totalStudents": 18
    },
    "targetStudents": [
      { "studentId": 105, "name": "이영희", "score": 2.0, "status": "미달성" }
    ],
    "suggestions": [
      "미달성 학생 학부모 상담 자료 정리",
      "영역별 인증 현황 엑셀 다운로드"
    ],
    "analysisSource": "gemini",
    "fallbackUsed": false
  }
  ```

#### (5) 관리자 종합 통계 리포트 생성 (`POST /api/ai/admin/summary-report`)
- **호출 권한**: 최고 관리자 (`admin`, role=2 전용)
- **Request Body**:
  ```json
  {
    "year": 2026,
    "grade": 1
  }
  ```
- **Response (`data` 객체)**:
  ```json
  {
    "year": 2026,
    "targetGrade": 1,
    "totalStudents": 64,
    "overallCompletionRate": 72.5,
    "statusCounts": {
      "certified": { "count": 28, "rate": 43.8 },
      "reviewing": { "count": 31, "rate": 48.4 },
      "supplement": { "count": 5, "rate": 7.8 }
    },
    "summaryReport": "## 2026학년도 1학년 역량인증제 종합 분석 리포트\n...",
    "keyInsights": [
      "전문기술역량 영역 이수율이 85%로 가장 높음",
      "인문학적 소양 독서 마일리지 영역의 제출이 전월 대비 15% 감소"
    ],
    "recommendations": [
      "기말고사 전 인문학 독서 주간 운영 권장"
    ],
    "analysisSource": "gemini",
    "fallbackUsed": false
  }
  ```

#### (6) 관리자 부정 의심 감사 리포트 (`POST /api/ai/admin/audit-anomalies`)
- **호출 권한**: 최고 관리자 (`admin`, role=2 전용)
- **Request Body**:
  ```json
  {
    "year": 2026,
    "threshold": 0.7,
    "limit": 50
  }
  ```
- **Response (`data` 객체)**:
  ```json
  {
    "year": 2026,
    "anomalies": [
      {
        "type": "DUPLICATE_FILE",
        "submissionId": 204,
        "studentId": 108,
        "riskScore": 0.9,
        "reason": "동일한 해시(SHA-256)의 첨부파일이 학생 102의 기승인 제출물과 중복 검출됨"
      }
    ],
    "totalAnomalies": 1,
    "riskLevel": "MEDIUM",
    "auditAnalysis": "중복 증빙 파일 1건이 검출되었습니다. 해당 학생 및 심사 교사에게 소명을 요구하십시오.",
    "recommendations": [
      "해당 제출물 심사 보류 및 원본 파일 대조"
    ],
    "analysisSource": "gemini",
    "fallbackUsed": false
  }
  ```

---

## 7. 심사 후 studentTotals 처리 및 Fallback Refetch 패턴

교사가 학생의 증빙자료를 승인(`PATCH /api/submissions/{id}/approve`), 반려(`PATCH /api/submissions/{id}/reject`), 또는 점수 사후 수정(`PATCH /api/submissions/{id}/score`)할 때, 백엔드는 학생의 최신 총점 및 영역별 점수를 자동 재계산하여 반환합니다.

### 7.1 심사 완료 응답 DTO

```json
{
  "status_code": 200,
  "success": true,
  "data": {
    "id": 123,
    "submissionId": 123,
    "status": "인정완료",
    "statusCode": 3,
    "score": 10.0,
    "grantedScore": 10.0,
    "teacherComment": "우수한 포트폴리오로 평가 기준을 완벽히 충족함.",
    "reviewerId": 5,
    "studentTotals": {
      "totalScore": 68.5,
      "certStatus": "진행중",
      "pointTotal": 4.0,
      "pendingCount": 1,
      "areas": [
        {
          "areaId": 1,
          "name": "직업기초능력",
          "totalScore": 18.0,
          "maxScore": 20,
          "isPassed": true
        }
      ]
    }
  },
  "error": null
}
```

### 7.2 Nullable 처리 및 Fallback Refetch 규약

네트워크 순간 지연이나 트랜잭션 격리 수준에 따라 드물게 `studentTotals` 계산이 스킵되어 `null`이 반환될 수 있습니다. 프론트엔드는 반드시 아래와 같이 **옵셔널 체이닝 및 재조회 Fallback**을 적용해야 합니다.

```typescript
const handleApproveSubmission = async (submissionId: number, score: number, comment: string, studentId: number) => {
  const response = await fetch(`/api/submissions/${submissionId}/approve`, {
    method: 'PATCH',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ score, comment }),
  });
  const result: SrFormat = await response.json();

  if (result.success && result.data) {
    if (result.data.studentTotals) {
      // 1. 응답에 포함된 즉시 계산 점수로 프론트엔드 캐시/스토어 갱신
      updateStudentScoreInStore(studentId, result.data.studentTotals);
    } else {
      // 2. [Fallback] studentTotals가 null인 경우 명시적 종합 현황 API 재조회
      const refetchRes = await fetch(`/api/students/${studentId}/certification-status`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const refetchJson = await refetchRes.json();
      if (refetchJson.success) {
        updateStudentScoreInStore(studentId, refetchJson.data);
      }
    }
  }
};
```

---

## 8. 연동 체크리스트 요약 (Quick Reference)

1. [ ] **식별자 검증**: Path 파라미터 `student_id`에 로그인 아이디 문자열(`users.id`) 대신 학적 고유 숫자 PK(`data.user.studentId`)를 넣었는가?
2. [ ] **응답 봉투**: 모든 비즈니스 응답에서 `response.data`와 `response.error`를 분기 처리하였는가?
3. [ ] **409 Conflict 처리**: 심사/삭제 시 409 발생 시 사용자에게 충돌 안내 후 상세/목록을 자동 Refetch하도록 구현했는가?
4. [ ] **파일 다운로드**: 파일 직접 링크 대신 `/api/submissions/{id}/download-ticket` 또는 `/api/uploads/ticket?file_path=`을 호출하여 1회용 티켓을 발급받아 서빙하는가?
5. [ ] **상벌점 부호**: 벌점 부여 시 `score: -N` 또는 `type: "벌점", points: N` 형식을 준수하여 `INVALID_POINT_SIGN`을 방지했는가?
6. [ ] **AI 권한**: 학생 챗봇/자동분류는 학생/관리자만, 심사초안은 심사 권한 교사만 호출하도록 UI 버튼 노출 가드를 적용했는가?
