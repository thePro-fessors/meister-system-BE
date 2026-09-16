## 0. 공통 사항

| 항목 | 내용 |
|---|---|
| 인증 방식 | 로그인 성공 시 발급되는 세션 토큰(JWT 등)을 `Authorization: Bearer {token}` 헤더로 전달 |
| 응답 포맷 | JSON, 공통 래퍼 `{ "success": boolean, "data": ..., "error": { "code": string, "message": string } }` |
| 에러 코드 예시 | `UNAUTHORIZED`(401), `FORBIDDEN`(403), `NOT_FOUND`(404), `VALIDATION_ERROR`(400), `SERVER_ERROR`(500) |
| 권한 구분 | 모든 엔드포인트는 로그인한 사용자의 `role`(student/teacher/admin)에 따라 접근이 제한됨. 아래 표의 "호출 주체" 참고 |
| 보안/안정성 등급 | 상(반드시 서버 검증·감사로그 필요) / 중(권한 검증 필요) / 하(단순 조회, 캐싱 가능) |

---

## 1. 인증 API (요구사항 3.1, 3.3, 3.4)

### 1.1 로그인
- **기능**: 아이디/비밀번호로 로그인하고 역할에 맞는 세션을 발급한다.
- **호출 주체**: 비로그인 사용자
- **Method / URL**: `POST /api/auth/login`

| 구분 | 필드 | 타입 | 설명 |
|---|---|---|---|
| 요청 | `id` | string | 로그인 아이디 |
| 요청 | `password` | string | 비밀번호 |
| 응답 | `token` | string | 세션 토큰 |
| 응답 | `user.id/name/role` | string | 사용자 기본 정보 |
| 응답 | `user.grade/classNo/number` | number \| null | 학생인 경우만 |
| 응답 | `user.homeroom` | string \| null | 담임 교사인 경우만 |

- **보안/안정성**: **상**. 비밀번호는 해시(bcrypt 등) 저장, 로그인 실패 시 시도 횟수 제한(브루트포스 방지), HTTPS 필수.

### 1.2 로그아웃
- **기능**: 현재 세션을 만료시킨다.
- **호출 주체**: 로그인 사용자
- **Method / URL**: `POST /api/auth/logout`
- **요청**: 없음(토큰만 헤더로 전달) / **응답**: `{ success: true }`
- **보안/안정성**: 중. 토큰 즉시 무효화(블랙리스트) 처리 필요.

### 1.3 내 정보 조회
- **기능**: 로그인한 사용자의 기본 정보 표시 (요구사항 3.3)
- **호출 주체**: 로그인 사용자
- **Method / URL**: `GET /api/auth/me`
- **응답**: `user` 객체 (1.1 응답과 동일 구조)
- **보안/안정성**: 중. 본인 토큰의 사용자 정보만 반환해야 함.

---

## 2. 학생 API (요구사항 5장)

### 2.1 인증 현황 조회
- **기능**: 학년도별 영역 점수/등급/전체 인증 상태 조회 (요구사항 5.1)
- **호출 주체**: 본인(student)
- **Method / URL**: `GET /api/students/{studentId}/certification-status?year={year}`

| 구분 | 필드 | 설명 |
|---|---|---|
| 요청(path) | `studentId` | 조회 대상 학생 ID (본인만 허용) |
| 요청(query) | `year` | 조회할 학년도 |
| 응답 | `year, grade, classNo, number` | 소속 정보 |
| 응답 | `areas[]` = `{ area, score, maxScore, grade(S/A/B/미달성), status }` | 영역별 결과 |
| 응답 | `totalScore` | 총점 |
| 응답 | `certStatus` | 인증 가능 / 미달성 / 검토중 / 보완 필요 |
| 응답 | `pointTotal` | 상벌점 반영 점수 |

- **보안/안정성**: **상**. `studentId`가 요청자 본인과 다르면 403 (다른 학생 정보 열람 차단, 요구사항 3.2).

### 2.2 증빙자료 제출
- **기능**: 증빙자료 신규 제출 (요구사항 5.2)
- **호출 주체**: 본인(student)
- **Method / URL**: `POST /api/submissions`

| 구분 | 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| 요청 | `year` | number | O | 학년도 |
| 요청 | `area` | string | O | 인증 영역 |
| 요청 | `itemId` | string | O | 평가 항목 ID |
| 요청 | `detail` | string | O | 세부 항목 정보 |
| 요청 | `activityDate` | date | O | 활동/취득일 |
| 요청 | `file` | file(multipart) | 조건부 | 파일 또는 link 중 최소 1개 |
| 요청 | `link` | string(URL) | 조건부 | 파일 또는 link 중 최소 1개 |
| 요청 | `description` | string | O | 자료 설명 |
| 응답 | `id, status(제출완료), submittedAt` | - | - | 생성된 제출 건 |

- **검증 규칙**: 파일/링크 중 최소 1개 필수(요구사항 5.2), 항목별 최대점 초과 여부는 검토 시 확인.
- **보안/안정성**: **상**. 파일 업로드는 확장자/용량 제한, 악성 파일 검사 필요. `studentId`는 토큰에서 추출(요청 바디로 받지 않음 — 위변조 방지).

### 2.3 내 제출 내역 조회
- **기능**: 본인 제출 자료 목록/상태 확인 (요구사항 5.3, 5.4)
- **호출 주체**: 본인(student)
- **Method / URL**: `GET /api/submissions?studentId={me}&year={year}`
- **응답**: 제출 목록 배열 `{ id, area, itemName, detail, status, score, teacherComment, submittedAt, reviewedAt, fileUrl, link }`
- **보안/안정성**: **상**. 본인 것만 조회되도록 서버에서 강제(요구사항 5.4 "학생은 본인의 제출 내역만 볼 수 있어야 한다").

### 2.4 재제출
- **기능**: 반려된 자료를 보완하여 재제출 (요구사항 5.3, 5.4)
- **호출 주체**: 본인(student)
- **Method / URL**: `PATCH /api/submissions/{submissionId}/resubmit`

| 구분 | 필드 | 설명 |
|---|---|---|
| 요청 | `description, file/link` | 수정된 설명/증빙 |
| 응답 | `status(검토중), submittedAt` | 갱신된 상태 |

- **검증 규칙**: 현재 상태가 `반려`/`재제출요청`일 때만 허용. `인정완료` 건은 수정 불가(요구사항 5.4).
- **보안/안정성**: 상. 본인 소유 여부 + 현재 상태 검증 필요.

### 2.5 상벌점 내역 조회
- **기능**: 본인 상벌점 내역 확인 (요구사항 5.6)
- **호출 주체**: 본인(student)
- **Method / URL**: `GET /api/points?studentId={me}`
- **응답**: `{ id, type(상점/벌점), score, reason, date, teacherName, reflected, reflectedArea }[]`
- **보안/안정성**: 중. 본인 데이터만 조회.

---

## 3. 교사 API (요구사항 6장)

### 3.1 교사 대시보드
- **기능**: 검토 대기/재제출/미부여 자료 수 및 권한 범위 표시 (요구사항 6.1)
- **호출 주체**: teacher
- **Method / URL**: `GET /api/teacher/dashboard`
- **응답**: `{ pendingCount, resubmittedCount, unscoredCount, scopeLabel, recentSubmissions[] }`
- **보안/안정성**: 중. 담당 학급/학생 범위 밖 데이터는 집계에서 제외되어야 함.

### 3.2 학생 자료 조회(검색)
- **기능**: 조건별 학생 목록 조회 (요구사항 6.2)
- **호출 주체**: teacher
- **Method / URL**: `GET /api/teacher/students`

| 구분 | 필드 | 설명 |
|---|---|---|
| 요청(query) | `year, grade, classNo, name, studentNo, area, status, hasPoints` | 검색 조건(전부 선택) |
| 응답 | `{ studentId, name, grade, classNo, number, totalScore, certStatus, pendingCount, pointTotal }[]` | 목록 |

- **보안/안정성**: **상**. "교사는 자신에게 권한이 부여된 학생 또는 항목만 확인" (요구사항 3.2) — 서버에서 `teacherId`의 담당 범위로 강제 필터링, 클라이언트 쿼리로 우회 불가하게 처리.

### 3.3 학생 상세(개별) 조회
- **기능**: 특정 학생의 인증 현황 + 제출 목록 (2.1, 2.3과 동일 데이터 구조를 교사 권한으로 조회)
- **호출 주체**: teacher (권한 범위 내 학생만)
- **Method / URL**: `GET /api/teacher/students/{studentId}`
- **보안/안정성**: 상. 권한 범위 검증 필수.

### 3.4 증빙자료 승인
- **기능**: 제출 자료 확인 후 점수 부여·인정 처리 (요구사항 6.3, 6.4)
- **호출 주체**: teacher (권한 범위 내)
- **Method / URL**: `PATCH /api/submissions/{submissionId}/approve`

| 구분 | 필드 | 필수 | 설명 |
|---|---|---|---|
| 요청 | `score` | O | 인정 점수 |
| 요청 | `comment` | X | 승인 의견 |
| 응답 | `status(인정완료), score, reviewerId, reviewedAt` | - | - |
| 응답 | `studentTotals` | - | 갱신된 학생 영역별/총점 (총점 자동 갱신, 요구사항 6.4) |

- **검증 규칙**: `score`는 해당 평가 항목의 `maxScore`를 초과할 수 없음(요구사항 6.4 필수 조건) → 위반 시 `VALIDATION_ERROR`.
- **점수 수정 시**: 별도 `PATCH /api/submissions/{id}/score` 로 수정 전/후 점수와 수정 사유(`reason`)를 필수로 받아 이력(`scoreHistory`)에 기록.
- **보안/안정성**: **상**. 점수 데이터 무결성이 걸린 핵심 API — 트랜잭션 처리, 최대점 서버 재검증(클라이언트 값 신뢰 금지), 변경 이력(감사로그) 필수.

### 3.5 증빙자료 반려
- **기능**: 기준 미충족 자료 반려 처리 (요구사항 6.3)
- **호출 주체**: teacher (권한 범위 내)
- **Method / URL**: `PATCH /api/submissions/{submissionId}/reject`
- **요청**: `comment`(필수, 반려 사유) / **응답**: `status(반려), teacherComment, reviewedAt`
- **검증 규칙**: `comment` 미입력 시 `VALIDATION_ERROR` (요구사항 6.3 "반드시 반려 사유를 입력").
- **보안/안정성**: 상. 학생에게 그대로 노출되는 문구이므로 XSS 방지를 위한 출력 이스케이프 필요.

### 3.6 상벌점 등록
- **기능**: 상점/벌점 등록 (요구사항 6.5)
- **호출 주체**: teacher (담당/권한 부여된 학생 대상만)
- **Method / URL**: `POST /api/points`

| 구분 | 필드 | 필수 | 설명 |
|---|---|---|---|
| 요청 | `studentId, type(상점/벌점), score, date, reason` | O | 기본 정보 |
| 요청 | `reflectedArea` | X | 인증 점수 반영 시 대상 영역 |
| 응답 | `id, teacherId(자동), createdAt` | - | - |

- **검증 규칙**: `reason` 미입력 시 거부(요구사항 6.5 "반드시 사유를 입력"). `teacherId`는 토큰에서 자동 저장.
- **보안/안정성**: **상**. 학생 성적/생활기록에 영향을 주는 민감 데이터 — 권한 범위 검증 + 감사로그 필수.

### 3.7 상벌점 수정
- **기능**: 잘못 등록된 상벌점 수정 (요구사항 6.5)
- **호출 주체**: teacher
- **Method / URL**: `PATCH /api/points/{pointId}`
- **요청**: `score, reason(수정 사유, 필수)` / **응답**: `editHistory[]에 { editorId, reason, at } 추가`
- **보안/안정성**: **상**. 수정 사유·수정자 기록이 요구사항에 명시된 필수 항목 — 이력 없는 수정은 금지.

---

## 4. 관리자 API (요구사항 7장)

### 4.1 학년도 목록/생성
- **기능**: 학년도 생성 및 목록 조회 (요구사항 7.1)
- **호출 주체**: admin
- **Method / URL**: `GET /api/years`, `POST /api/years { year }`
- **보안/안정성**: 중. 중복 학년도 생성 방지(서버 유니크 제약).

### 4.2 이전 학년도 기준 복사
- **기능**: 기존 학년도 기준을 복사해 새 학년도 생성 (요구사항 7.1)
- **호출 주체**: admin
- **Method / URL**: `POST /api/years/{targetYear}/copy-from/{sourceYear}`
- **보안/안정성**: 중. 대량 쓰기 작업 — 트랜잭션 처리 권장.

### 4.3 학년도 활성화/비활성화
- **기능**: 현재 사용 학년도 지정 (요구사항 7.1)
- **호출 주체**: admin
- **Method / URL**: `PATCH /api/years/{year} { active: boolean }`
- **검증 규칙**: 활성 학년도는 시스템 전체에 1개만 존재하도록 서버에서 강제.
- **보안/안정성**: **상**. 전체 학생의 표시 기준이 바뀌는 시스템 전역 설정 — 관리자 외 접근 완전 차단, 변경 로그 필수.

### 4.4 학년별 기준/배점/산정방식 조회·수정
- **기능**: 영역·평가항목·배점·산정방식·사용여부·증빙필요여부 관리 (요구사항 7.2, 7.3)
- **호출 주체**: admin
- **Method / URL**:
  - `GET /api/criteria?year={year}&grade={grade}`
  - `PATCH /api/criteria/{year}/{grade}/areas/{area} { maxScore }`
  - `POST /api/criteria/{year}/{grade}/areas/{area}/items { name, maxScore, scoringType, active, requiresEvidence, description }`
  - `PATCH /api/criteria/.../items/{itemId} { ...동일 필드 }`
  - `DELETE /api/criteria/.../items/{itemId}`
- **검증 규칙**: `scoringType`은 8종 중 하나(고정점수형/등급환산형/구간점수형/시간계산형/감점형/가산점형/최대점제한형/최상위인정형), 최소 2종 이상 실제 계산 로직 구현 필요(요구사항 7.3).
- **보안/안정성**: **상**. 기준 변경은 전체 학생 점수 계산에 영향 — 변경 시 기존 제출건 재계산 배치 필요, 변경 이력 기록 권장.

### 4.5 전체 현황 조회
- **기능**: 전체 학생의 인증 상태/총점 요약 (요구사항 7.5, 관리자 전체 현황)
- **호출 주체**: admin
- **Method / URL**: `GET /api/admin/overview?year={year}`
- **응답**: `{ totalStudents, statusCounts, pendingSubmissions, students[] }`
- **보안/안정성**: 중. 대량 조회이므로 페이지네이션 권장(응답 지연/부하 방지 — 안정성 이슈).

---

## 5. AI 기능 API (실제 모델 연동 시)

현재는 프론트엔드 내 규칙 기반 로직(`src/lib/ai.ts`, `src/lib/chatbot.ts`)으로 동작합니다.
실제 LLM으로 교체할 경우 아래와 같이 **서버를 경유하는 구조**가 필요합니다(API 키를 브라우저에 노출할 수 없기 때문).

### 5.1 증빙자료 자동 분류
- **기능**: 학생이 입력한 세부항목/설명을 분석해 인증 영역·평가 항목 추천
- **호출 주체**: student
- **Method / URL**: `POST /api/ai/classify-submission`
- **요청**: `{ detail, description }`
- **응답**: `{ suggestions: [{ area, itemName, confidence, reason }] }`
- **보안/안정성**: 중. 추천값은 참고용이며 최종 저장 값은 학생이 확정 → 서버는 참고용 응답만 반환, 신뢰도 낮은 요청 남용 방지를 위한 rate limit 권장.

### 5.2 학생 도우미 챗봇
- **기능**: 학생 질문에 본인 데이터 기반 맞춤 답변
- **호출 주체**: student (본인)
- **Method / URL**: `POST /api/ai/chat`
- **요청**: `{ message, conversationId? }` (studentId는 토큰에서 추출, 클라이언트가 임의 지정 불가)
- **응답**: `{ reply, suggestions?[] }`
- **보안/안정성**: **상**.
  - 서버가 해당 학생의 점수/제출/상벌점 데이터를 조회해 프롬프트에 포함하므로, **본인 데이터만 조회되도록 서버에서 강제** (다른 학생 데이터 유출 방지 — 가장 중요).
  - LLM 응답에 개인정보·타 학생 정보가 섞이지 않도록 프롬프트/응답 검증.
  - 과도한 호출로 인한 API 비용 급증 방지를 위한 사용자별 rate limit 필요.
  - 응답 지연 시 타임아웃 및 폴백 메시지(예: 규칙 기반 답변으로 대체) 권장.

---

## 6. 보안·안정성 우선순위 요약

| 등급 | 해당 API | 이유 |
|---|---|---|
| **상** | 로그인, 증빙자료 제출/승인/반려/재제출, 상벌점 등록/수정, 학년도 활성화, 기준·배점 수정, AI 챗봇 | 개인정보·평가점수 등 민감 데이터를 다루거나, 잘못될 경우 학생 성적/인증 결과에 직접 영향 |
| 중 | 대시보드/현황 조회, 로그아웃, 내 정보 조회, 학년도 생성, AI 자동분류 | 조회 위주지만 권한 범위 통제는 필요 |
| 하 | 등급 환산 기준 등 정적 조회성 정보 | 공개되어도 무방한 참고 정보 |

공통 권장 사항:
1. 모든 쓰기(POST/PATCH/DELETE) API는 서버에서 **요청자 role + 소유권(본인 또는 담당 범위)**을 재검증한다 (클라이언트가 보낸 ID를 그대로 신뢰하지 않음).
2. 점수·상벌점 등 정정 가능한 데이터는 **변경 이력(누가/언제/왜)**을 남긴다.
3. 파일 업로드는 확장자·용량 제한과 악성 파일 검사를 거친다.
4. 관리자 전용 API(학년도/기준 변경)는 시스템 전체에 영향을 주므로 가장 엄격하게 보호한다.