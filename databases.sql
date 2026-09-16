

CREATE TABLE academic_years
(
  year_id      INT  NOT NULL AUTO_INCREMENT COMMENT '학년도 ID',
  year         INT  NOT NULL COMMENT '대상 연도',
  is_activated BOOL NULL     DEFAULT FALSE COMMENT '학년도 활성 여부',
  PRIMARY KEY (year_id)
) COMMENT '학년도 관리 테이블';

ALTER TABLE academic_years
  ADD CONSTRAINT UQ_year UNIQUE (year);

CREATE TABLE certification_areas
(
  area_id   INT         NOT NULL AUTO_INCREMENT COMMENT '인증 영역 고유 ID',
  year_id   INT         NOT NULL COMMENT '대상 학년도 ID',
  name      VARCHAR(30) NOT NULL COMMENT '인증 영역',
  max_score INT         NOT NULL,
  PRIMARY KEY (area_id)
) COMMENT '인증 영역 관리 테이블';

CREATE TABLE chat_conversations
(
  conversation_id VARCHAR(36)  NOT NULL,
  user_uuid       VARCHAR(36)  NOT NULL COMMENT '유저 고유 ID',
  title           VARCHAR(100) NULL    ,
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (conversation_id)
);

CREATE TABLE chat_messages
(
  message_id      BIGINT      NOT NULL AUTO_INCREMENT,
  conversation_id VARCHAR(36) NOT NULL,
  sender_role     VARCHAR(10) NOT NULL COMMENT 'USER/ASSISTANT',
  content         TEXT        NOT NULL,
  created_at      TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (message_id)
);

CREATE TABLE evaluation_items
(
  item_id           INT          NOT NULL AUTO_INCREMENT COMMENT '고유 ID',
  area_id           INT          NOT NULL COMMENT '인증 영역 고유 ID',
  name              VARCHAR(50)  NULL     COMMENT '평가 항목 이름',
  target_grade      TINYINT      NULL     DEFAULT 0 COMMENT '대상 학년',
  max_score         DECIMAL(5,2) NULL     COMMENT '최대 제한 점수',
  scoring_type      TINYINT      NOT NULL COMMENT '점수 산정 방식',
  requires_evidence BOOL         NOT NULL DEFAULT TRUE,
  is_active         BOOL         NULL     DEFAULT FALSE COMMENT '활성 상태',
  PRIMARY KEY (item_id)
) COMMENT '세부 평가 항목 테이블';

CREATE TABLE merits
(
  merits_point_id INT          NOT NULL AUTO_INCREMENT COMMENT '사안 ID',
  student_id      INT          NOT NULL COMMENT '학생 고유 ID',
  teachers_id     INT          NOT NULL COMMENT '교사 고유 ID',
  type            VARCHAR(1)   NULL     DEFAULT '-' COMMENT '상/벌점 구분',
  points          DECIMAL(3,1) NULL     DEFAULT 1 COMMENT '상벌점 점수',
  reason          TEXT         NOT NULL COMMENT '부여 사유',
  related_area    VARCHAR(20)  NULL     COMMENT '반영 영역',
  occurred_at     DATE         NULL     COMMENT '발생일',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  is_reflected    BOOL         NOT NULL DEFAULT TRUE,
  is_deleted      BOOL         NULL     DEFAULT FALSE COMMENT 'Soft Delete',
  PRIMARY KEY (merits_point_id)
) COMMENT '상/벌점 테이블';

CREATE TABLE merits_log
(
  log_id          INT          NOT NULL AUTO_INCREMENT COMMENT '기록 고유 ID',
  merits_point_id INT          NOT NULL COMMENT '수정할 사안 ID',
  modifier_uuid   VARCHAR(36)  NOT NULL COMMENT '변경자의 UUID',
  action_type     VARCHAR(20)  NOT NULL COMMENT '수정 / 삭제 구분자',
  old_points      DECIMAL(3,1) NULL     COMMENT '변경 전 점수',
  new_points      DECIMAL(3,1) NULL     COMMENT '변경 후 점수',
  modify_reason   TEXT         NULL     COMMENT '수정 사유',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '변경 시각',
  PRIMARY KEY (log_id)
) COMMENT '상벌점 수정 기록 테이블';

CREATE TABLE scoring_rules
(
  rule_id         INT          NOT NULL AUTO_INCREMENT COMMENT '고유 ID',
  item_id         INT          NOT NULL COMMENT '평가 항목 고유 ID',
  condition_value VARCHAR(100) NULL     COMMENT '조건값',
  score           DECIMAL(5,2) NOT NULL COMMENT '부여 점수',
  PRIMARY KEY (rule_id)
) COMMENT '상세 점수 기준 테이블';

CREATE TABLE student_academic_records
(
  record_id  INT     NOT NULL AUTO_INCREMENT,
  student_id INT     NOT NULL COMMENT '학생 고유 ID',
  year_id    INT     NOT NULL COMMENT '학년도 ID',
  grade      TINYINT NOT NULL,
  class      TINYINT NOT NULL,
  number     TINYINT NOT NULL,
  PRIMARY KEY (record_id)
);

CREATE TABLE students
(
  uuid       VARCHAR(36)  NULL     COMMENT '유저 고유 ID',
  student_id INT          NOT NULL AUTO_INCREMENT COMMENT '학생 고유 ID',
  name       VARCHAR(20)  NOT NULL COMMENT '학생 이름',
  email      VARCHAR(100) NOT NULL COMMENT '학생 이메일',
  status     TINYINT      NULL     DEFAULT 0 COMMENT '상태',
  is_deleted BOOL         NULL     DEFAULT FALSE COMMENT 'Soft Delete',
  PRIMARY KEY (student_id)
) COMMENT '학생 정보 관리 테이블';

ALTER TABLE students
  ADD CONSTRAINT UQ_uuid UNIQUE (uuid);

ALTER TABLE students
  ADD CONSTRAINT UQ_email UNIQUE (email);

CREATE TABLE submissions
(
  submission_id   INT          NOT NULL AUTO_INCREMENT COMMENT '증빙자료 고유 ID',
  student_id      INT          NOT NULL COMMENT '학생 고유 ID',
  item_id         INT          NOT NULL COMMENT '제출 대상 평가 항목',
  activity_date   DATE         NULL     COMMENT '활동 및 취득일',
  file_path       VARCHAR(500) NULL     COMMENT '파일 주소',
  link_url        VARCHAR(500) NULL     COMMENT '링크 주소',
  description     TEXT         NULL     COMMENT '학생의 설명',
  status_code     TINYINT      NOT NULL DEFAULT 1 COMMENT '자료 상태 코드',
  granted_score   DECIMAL(5,2) NULL     COMMENT '최종 점수',
  reviewer_id     INT          NULL     COMMENT '검토 교사 ID',
  teacher_comment TEXT         NULL     COMMENT '교사 의견',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  is_deleted      BOOL         NULL     DEFAULT FALSE COMMENT 'Soft Delete',
  PRIMARY KEY (submission_id)
) COMMENT '증빙자료 관리 테이블';

CREATE TABLE submissions_logs
(
  log_id          INT          NOT NULL AUTO_INCREMENT COMMENT '기록 고유 ID',
  submission_id   INT          NOT NULL COMMENT '수정할 항목 ID',
  modifier_uuid   VARCHAR(36)  NOT NULL COMMENT '수정자 UUID',
  action_type     VARCHAR(20)  NOT NULL COMMENT '행위 구분',
  old_status_code TINYINT      NULL     COMMENT '변경 전 상태 코드',
  new_status_code TINYINT      NOT NULL COMMENT '변경 후 상태 코드',
  old_score       DECIMAL(5,2) NULL     COMMENT '변경 전 부여 점수',
  new_score       DECIMAL(5,2) NULL     COMMENT '변경 후 부여 점수',
  comment         TEXT         NULL     COMMENT '변경 사유',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '변경 일시',
  PRIMARY KEY (log_id)
) COMMENT '증빙자료 심사 이력 테이블';

CREATE TABLE submissions_status
(
  status_code TINYINT     NOT NULL AUTO_INCREMENT COMMENT '자료 상태 코드',
  description VARCHAR(30) NOT NULL COMMENT '자료 상태 설명',
  PRIMARY KEY (status_code)
) COMMENT '증빙자료 상태 관리 테이블';

CREATE TABLE teachers
(
  uuid        VARCHAR(36)  NULL     COMMENT '유저 고유 ID',
  teachers_id INT          NOT NULL AUTO_INCREMENT COMMENT '교사 고유 ID',
  name        VARCHAR(20)  NOT NULL COMMENT '교사 성명',
  subject     VARCHAR(20)  NULL     COMMENT '담당교과',
  grade       TINYINT      NULL     COMMENT '담당 학년',
  class       TINYINT      NULL     COMMENT '담당 반',
  email       VARCHAR(100) NOT NULL COMMENT '교사 이메일',
  is_deleted  BOOL         NULL     DEFAULT FALSE COMMENT 'Soft Delete',
  PRIMARY KEY (teachers_id)
) COMMENT '교사 정보 관리 테이블';

ALTER TABLE teachers
  ADD CONSTRAINT UQ_uuid UNIQUE (uuid);

ALTER TABLE teachers
  ADD CONSTRAINT UQ_email UNIQUE (email);

CREATE TABLE users
(
  uuid       VARCHAR(36)  NOT NULL COMMENT '유저 고유 ID',
  id         VARCHAR(50)  NOT NULL COMMENT '유저 ID',
  password   VARCHAR(255) NOT NULL COMMENT '유저 PW',
  email      VARCHAR(320) NULL     COMMENT '유저 이메일',
  role       TINYINT      NOT NULL COMMENT '학생 / 교사 / 관리자 구분',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  is_deleted BOOL         NOT NULL DEFAULT FALSE COMMENT 'soft delete',
  PRIMARY KEY (uuid)
) COMMENT '사용자 관리 테이블';

ALTER TABLE users
  ADD CONSTRAINT UQ_id UNIQUE (id);

ALTER TABLE users
  ADD CONSTRAINT UQ_email UNIQUE (email);

ALTER TABLE teachers
  ADD CONSTRAINT FK_users_TO_teachers
    FOREIGN KEY (uuid)
    REFERENCES users (uuid);

ALTER TABLE students
  ADD CONSTRAINT FK_users_TO_students
    FOREIGN KEY (uuid)
    REFERENCES users (uuid);

ALTER TABLE merits
  ADD CONSTRAINT FK_teachers_TO_merits
    FOREIGN KEY (teachers_id)
    REFERENCES teachers (teachers_id);

ALTER TABLE merits
  ADD CONSTRAINT FK_students_TO_merits
    FOREIGN KEY (student_id)
    REFERENCES students (student_id);

ALTER TABLE merits_log
  ADD CONSTRAINT FK_merits_TO_merits_log
    FOREIGN KEY (merits_point_id)
    REFERENCES merits (merits_point_id);

ALTER TABLE submissions
  ADD CONSTRAINT FK_students_TO_submissions
    FOREIGN KEY (student_id)
    REFERENCES students (student_id);

ALTER TABLE submissions
  ADD CONSTRAINT FK_submissions_status_TO_submissions
    FOREIGN KEY (status_code)
    REFERENCES submissions_status (status_code);

ALTER TABLE submissions
  ADD CONSTRAINT FK_teachers_TO_submissions
    FOREIGN KEY (reviewer_id)
    REFERENCES teachers (teachers_id);

ALTER TABLE certification_areas
  ADD CONSTRAINT FK_academic_years_TO_certification_areas
    FOREIGN KEY (year_id)
    REFERENCES academic_years (year_id);

ALTER TABLE evaluation_items
  ADD CONSTRAINT FK_certification_areas_TO_evaluation_items
    FOREIGN KEY (area_id)
    REFERENCES certification_areas (area_id);

ALTER TABLE scoring_rules
  ADD CONSTRAINT FK_evaluation_items_TO_scoring_rules
    FOREIGN KEY (item_id)
    REFERENCES evaluation_items (item_id);

ALTER TABLE submissions
  ADD CONSTRAINT FK_evaluation_items_TO_submissions
    FOREIGN KEY (item_id)
    REFERENCES evaluation_items (item_id);

ALTER TABLE submissions_logs
  ADD CONSTRAINT FK_submissions_TO_submissions_logs
    FOREIGN KEY (submission_id)
    REFERENCES submissions (submission_id);

ALTER TABLE submissions_logs
  ADD CONSTRAINT FK_users_TO_submissions_logs
    FOREIGN KEY (modifier_uuid)
    REFERENCES users (uuid);

ALTER TABLE student_academic_records
  ADD CONSTRAINT FK_academic_years_TO_student_academic_records
    FOREIGN KEY (year_id)
    REFERENCES academic_years (year_id);

ALTER TABLE student_academic_records
  ADD CONSTRAINT FK_students_TO_student_academic_records
    FOREIGN KEY (student_id)
    REFERENCES students (student_id);

ALTER TABLE merits_log
  ADD CONSTRAINT FK_users_TO_merits_log
    FOREIGN KEY (modifier_uuid)
    REFERENCES users (uuid);

ALTER TABLE chat_conversations
  ADD CONSTRAINT FK_users_TO_chat_conversations
    FOREIGN KEY (user_uuid)
    REFERENCES users (uuid);

ALTER TABLE chat_messages
  ADD CONSTRAINT FK_chat_conversations_TO_chat_messages
    FOREIGN KEY (conversation_id)
    REFERENCES chat_conversations (conversation_id);

CREATE INDEX IX_academic_records_lookup
  ON student_academic_records (year_id ASC, grade ASC, class ASC);

CREATE INDEX IX_submissions_status_date
  ON submissions (status_code ASC, created_at DESC);

ALTER TABLE student_academic_records ADD CONSTRAINT UQ_student_year UNIQUE (student_id, year_id);
ALTER TABLE certification_areas ADD CONSTRAINT UQ_year_area_name UNIQUE (year_id, name);
