# 🗄️ 마이스터 시스템 DB 스키마 변경 내역서 (Schema Changelog)

> **문서 버전**: v1.1  
> **기준 일자**: 2026-09-29  
> **대상 파일**: [databases.sql](databases.sql)  
> **변경 목적**: 프론트엔드 연동 요구사항(`BACKEND_REQUIREMENTS.md`) 충족 및 데이터 무결성/추적성 강화

---

## 📌 변경 요약표

| 테이블명 | 구분 | 컬럼 / 제약조건명 | 타입 / 속성 | 변경 사유 및 FE 요구사항 대응 |
| :--- | :---: | :--- | :--- | :--- |
| `certification_areas` | **컬럼 추가** | `grade` | `TINYINT NOT NULL DEFAULT 1` | 학년별(1, 2, 3학년) 영역 최대 배점 차등 설정 지원 |
| `certification_areas` | **제약 변경** | `UQ_year_grade_area_name` | `UNIQUE (year_id, grade, name)` | 동일 학년도 내 학년별 영역명 고유성 보장 (기존 `UQ_year_area_name` 대체) |
| `submissions` | **컬럼 추가** | `detail` | `VARCHAR(200) NULL` | 학생이 제출한 세부 활동명/자격명 분리 저장 (`description`과 구분) |
| `submissions` | **컬럼 추가** | `original_filename` | `VARCHAR(255) NULL` | 사용자가 업로드한 원본 파일명 보존 및 다운로드 시 표시 지원 |
| `submissions` | **컬럼 추가** | `reviewed_at` | `DATETIME NULL` | 교사의 승인/반려 심사 완료 시각 명확한 기록 (`created_at`과 분리) |

---

## 🔍 상세 변경 내역 및 배경

### 1. `certification_areas` (인증 영역 관리 테이블)

#### 🚨 기존 문제점
- 기존 스키마는 `(area_id, year_id, name, max_score)` 구조로, **학년도(`year_id`)별로만 배점을 관리**할 수 있었습니다.
- 그러나 실제 학교 교육과정 및 **FE 요구사항 6장**에 따르면:
  > *"현재 FE는 **학년(1/2/3학년)마다 영역 최대점을 다르게 설정**할 수 있다. BE의 `certification_areas`에는 학년 구분이 없고 `evaluation_items.target_grade`만 있으므로 학년별 영역 배점 저장 구조를 검토해야 한다."*
- 예시: 1학년의 '전문기술역량' 배점은 80점인데, 3학년은 120점인 경우 기존 구조로는 저장이 불가능했습니다.

#### ✅ 변경 내용
1. `grade` 컬럼 추가:
   ```sql
   grade TINYINT NOT NULL DEFAULT 1 COMMENT '대상 학년 (1, 2, 3)'
   ```
2. 고유 제약조건(Unique Key) 변경:
   - **기존**: `ALTER TABLE certification_areas ADD CONSTRAINT UQ_year_area_name UNIQUE (year_id, name);`
   - **변경**: `ALTER TABLE certification_areas ADD CONSTRAINT UQ_year_grade_area_name UNIQUE (year_id, grade, name);`
   - 효과: 동일 학년도 내에서도 1학년 '직업기초능력', 2학년 '직업기초능력', 3학년 '직업기초능력'을 각각 다른 배점으로 독립 관리 가능.

---

### 2. `submissions` (증빙자료 관리 테이블)

#### 🚨 기존 문제점
1. **원본 파일명 유실**:
   - 서버에는 해시 난수화된 저장 경로(`file_path`: 예: `/uploads/submissions/2026/09/a1b2c3d4.zip`)만 남아, 학생/교사가 화면에서 자신이 올린 원래 파일명(`"정보처리기능사_사본.pdf"`)을 확인할 수 없었습니다.
2. **검토 시각(`reviewed_at`) 부재**:
   - `created_at`(제출 시각)만 존재하여, 교사가 언제 승인/반려했는지 확인하려면 감사 테이블(`submissions_logs`)을 서브쿼리로 조인해야 하는 성능 병목이 발생했습니다.
3. **세부 활동명(`detail`) 분리 부재**:
   - **FE 요구사항 4장** 데이터 모델에는 `detail`(자격명, 대회 종목 등)과 `description`(상세 활동 설명)이 구분되어 요구되었습니다.

#### ✅ 변경 내용
```sql
CREATE TABLE submissions
(
  submission_id     INT          NOT NULL AUTO_INCREMENT COMMENT '증빙자료 고유 ID',
  student_id        INT          NOT NULL COMMENT '학생 고유 ID',
  item_id           INT          NOT NULL COMMENT '제출 대상 평가 항목',
  detail            VARCHAR(200) NULL     COMMENT '세부 활동명 또는 자격명', -- [신규]
  activity_date     DATE         NULL     COMMENT '활동 및 취득일',
  file_path         VARCHAR(500) NULL     COMMENT '파일 저장 경로',
  original_filename VARCHAR(255) NULL     COMMENT '업로드 원본 파일명',        -- [신규]
  link_url          VARCHAR(500) NULL     COMMENT '링크 주소',
  description       TEXT         NULL     COMMENT '학생의 설명',
  status_code       TINYINT      NOT NULL DEFAULT 1 COMMENT '자료 상태 코드',
  granted_score     DECIMAL(5,2) NULL     COMMENT '최종 점수',
  reviewer_id       INT          NULL     COMMENT '검토 교사 ID',
  reviewed_at       DATETIME     NULL     COMMENT '교사 검토 일시',           -- [신규]
  teacher_comment   TEXT         NULL     COMMENT '교사 의견',
  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  is_deleted        BOOL         NULL     DEFAULT FALSE COMMENT 'Soft Delete',
  PRIMARY KEY (submission_id)
) COMMENT '증빙자료 관리 테이블';
```

---

## 🛠️ 이미 운영/개발 중인 DB에 적용할 마이그레이션 DDL (ALTER TABLE)

기존 DB 인스턴스에 데이터를 보존하면서 본 변경사항을 적용하려면 아래 SQL을 실행하면 됩니다:

```sql
-- 1. certification_areas 변경
ALTER TABLE certification_areas 
  ADD COLUMN grade TINYINT NOT NULL DEFAULT 1 COMMENT '대상 학년 (1, 2, 3)' AFTER year_id;

ALTER TABLE certification_areas 
  DROP CONSTRAINT UQ_year_area_name;

ALTER TABLE certification_areas 
  ADD CONSTRAINT UQ_year_grade_area_name UNIQUE (year_id, grade, name);

-- 2. submissions 변경
ALTER TABLE submissions 
  ADD COLUMN detail VARCHAR(200) NULL COMMENT '세부 활동명 또는 자격명' AFTER item_id,
  ADD COLUMN original_filename VARCHAR(255) NULL COMMENT '업로드 원본 파일명' AFTER file_path,
  ADD COLUMN reviewed_at DATETIME NULL COMMENT '교사 검토 일시' AFTER reviewer_id;
```
