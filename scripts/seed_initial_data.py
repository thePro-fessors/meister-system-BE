"""
scripts/seed_initial_data.py - 초기 운영 데이터 및 관리자 계정 시드 주입 스크립트

용도:
1. 최초 2026 활성 학년도(academic_years) 자동 등록
2. 최초 관리자(admin, role=2) 계정 및 비밀번호 해싱 등록
3. 부산SW마이스터고 5대 핵심 인증 영역(certification_areas) 및 기본 평가 항목(evaluation_items), 배점 규칙(scoring_rules) 자동 주입
4. 선택적 샘플 데이터(--with-sample-data) 지원

실행:
    python scripts/seed_initial_data.py
    python scripts/seed_initial_data.py --admin-pass "SecureAdminPass2026!" --with-sample-data
"""

import argparse
import asyncio
import os
import sys
import uuid
import bcrypt
import dotenv

# 프로젝트 루트 경로 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

dotenv.load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

try:
    import asyncmy
    from asyncmy.cursors import DictCursor
except ImportError:
    print("[ERROR] asyncmy 패키지가 설치되어 있지 않습니다. 가상환경 또는 Docker 컨테이너 내에서 실행하십시오.")
    sys.exit(1)


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


async def seed_data(
    admin_id: str,
    admin_pass: str,
    target_year: int,
    with_sample: bool,
    teacher_pass: str = "password123!",
    student_pass: str = "password123!",
):
    db_host = os.getenv("DATABASE_HOST", "localhost")
    db_port = int(os.getenv("DATABASE_PORT", "3306"))
    db_user = os.getenv("DATABASE_USER", "root")
    db_password = os.getenv("DATABASE_PASSWORD", "")
    db_name = os.getenv("DATABASE_NAME", "swMeister")

    print(f"[*] MySQL 연결 중... ({db_user}@{db_host}:{db_port}/{db_name})")
    try:
        conn = await asyncmy.connect(
            host=db_host,
            port=db_port,
            user=db_user,
            password=db_password,
            database=db_name,
            autocommit=False,
        )
    except Exception as e:
        print(f"[FAIL] 데이터베이스 연결 실패: {e}")
        return False

    try:
        async with conn.cursor(cursor=DictCursor) as cur:
            print("\n[1/4] 학년도(academic_years) 확인 및 생성...")
            await cur.execute("SELECT year_id, is_activated FROM academic_years WHERE year = %s", (target_year,))
            year_row = await cur.fetchone()
            if not year_row:
                # 기존 다른 활성 학년도 비활성화
                await cur.execute("UPDATE academic_years SET is_activated = FALSE, status = 'CLOSED'")
                await cur.execute(
                    "INSERT INTO academic_years (year, is_activated, status) VALUES (%s, TRUE, 'ACTIVE')",
                    (target_year,),
                )
                year_id = cur.lastrowid
                print(f"  [+] {target_year} 학년도 신규 생성 및 활성화 완료 (year_id: {year_id})")
            else:
                year_id = year_row["year_id"]
                if not year_row.get("is_activated") or year_row.get("status") != "ACTIVE":
                    await cur.execute("UPDATE academic_years SET is_activated = FALSE, status = 'CLOSED'")
                    await cur.execute("UPDATE academic_years SET is_activated = TRUE, status = 'ACTIVE' WHERE year_id = %s", (year_id,))
                    print(f"  [*] 기존 {target_year} 학년도를 활성 상태(ACTIVE)로 갱신 (year_id: {year_id})")
                else:
                    print(f"  [=] 이미 {target_year} 학년도가 활성 상태입니다 (year_id: {year_id})")

            print("\n[2/4] 관리자(admin) 계정 확인 및 생성...")
            await cur.execute("SELECT uuid, id, role FROM users WHERE id = %s OR role = 2 LIMIT 1", (admin_id,))
            admin_user = await cur.fetchone()
            if not admin_user:
                admin_uuid = str(uuid.uuid4())
                hashed_pw = hash_password(admin_pass)
                await cur.execute(
                    """
                    INSERT INTO users (id, password, role, uuid)
                    VALUES (%s, %s, 2, %s)
                    """,
                    (admin_id, hashed_pw, admin_uuid),
                )
                print(f"  [+] 관리자 계정 생성 완료: ID='{admin_id}', Role=2 (UUID: {admin_uuid})")
            else:
                hashed_pw = hash_password(admin_pass)
                await cur.execute(
                    "UPDATE users SET password = %s WHERE id = %s",
                    (hashed_pw, admin_user["id"]),
                )
                print(f"  [*] 관리자 계정 비밀번호 갱신: ID='{admin_user['id']}', Role={admin_user['role']}")

            print("\n[3/4] 5대 마이스터 역량 인증 영역 및 기본 평가 기준 주입...")
            areas_spec = [
                {
                    "name": "전공역량",
                    "max_score": 300,
                    "subject": "전공",
                    "department": "마이스터부",
                    "items": [
                        {"name": "전공 프로젝트 개발 및 포트폴리오", "max_score": 100.0, "scoring_type": 1},
                        {"name": "교내외 전공 해커톤/경진대회 수상", "max_score": 100.0, "scoring_type": 8},
                        {"name": "오픈소스 기여 및 기술 블로그 작성", "max_score": 100.0, "scoring_type": 1},
                    ],
                },
                {
                    "name": "외국어역량",
                    "max_score": 150,
                    "subject": "영어",
                    "department": "교무기획부",
                    "items": [
                        {"name": "공인 어학 성적 (TOEIC / TEPS / OPIc 등)", "max_score": 100.0, "scoring_type": 8},
                        {"name": "영어 원문 기술 문서 번역 및 발표", "max_score": 50.0, "scoring_type": 1},
                    ],
                },
                {
                    "name": "자격취득",
                    "max_score": 200,
                    "subject": "자격증",
                    "department": "산학협력부",
                    "items": [
                        {"name": "국가기술자격 (정보처리기능사, 네트워크관리사 등)", "max_score": 100.0, "scoring_type": 1},
                        {"name": "국제공인 IT 자격 (AWS, CKA, Oracle 등)", "max_score": 100.0, "scoring_type": 1},
                    ],
                },
                {
                    "name": "인성/봉사",
                    "max_score": 150,
                    "subject": "인성",
                    "department": "학생생활안전부",
                    "items": [
                        {"name": "교내외 봉사활동 (시간당 점수 부여)", "max_score": 80.0, "scoring_type": 1},
                        {"name": "선행/모범/표창 수상", "max_score": 70.0, "scoring_type": 1},
                    ],
                },
                {
                    "name": "창체/독서",
                    "max_score": 100,
                    "subject": "창체",
                    "department": "교육정보부",
                    "items": [
                        {"name": "인문/전공 독서 서평 작성 및 기록", "max_score": 60.0, "scoring_type": 1},
                        {"name": "동아리 활동 및 학술 세미나 참가", "max_score": 40.0, "scoring_type": 1},
                    ],
                },
            ]

            created_areas = 0
            created_items = 0

            # 1, 2, 3학년 공통 적용
            for grade in (1, 2, 3):
                for a_spec in areas_spec:
                    await cur.execute(
                        """
                        SELECT area_id FROM certification_areas
                        WHERE year_id = %s AND grade = %s AND name = %s
                        LIMIT 1
                        """,
                        (year_id, grade, a_spec["name"]),
                    )
                    ex_area = await cur.fetchone()
                    if not ex_area:
                        await cur.execute(
                            """
                            INSERT INTO certification_areas (year_id, grade, name, max_score, subject, department)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (year_id, grade, a_spec["name"], a_spec["max_score"], a_spec["subject"], a_spec["department"]),
                        )
                        area_id = cur.lastrowid
                        created_areas += 1
                    else:
                        area_id = ex_area["area_id"]

                    for item_spec in a_spec["items"]:
                        await cur.execute(
                            """
                            SELECT item_id FROM evaluation_items
                            WHERE area_id = %s AND name = %s
                            LIMIT 1
                            """,
                            (area_id, item_spec["name"]),
                        )
                        ex_item = await cur.fetchone()
                        if not ex_item:
                            await cur.execute(
                                """
                                INSERT INTO evaluation_items (area_id, name, target_grade, max_score, scoring_type, is_active)
                                VALUES (%s, %s, %s, %s, %s, TRUE)
                                """,
                                (area_id, item_spec["name"], grade, item_spec["max_score"], item_spec["scoring_type"]),
                            )
                            new_item_id = cur.lastrowid
                            created_items += 1
                            # 기본 배점 규칙 1건 주입
                            await cur.execute(
                                """
                                INSERT INTO scoring_rules (item_id, condition_value, score)
                                VALUES (%s, %s, %s)
                                """,
                                (new_item_id, "기본 인정 기준 충족", item_spec["max_score"]),
                            )

            print(f"  [+] 1~3학년 역량 영역 {created_areas}개, 평가 항목 {created_items}개 시드 등록 완료")

            # 4. 샘플 교사/학생 데이터 주입 (선택)
            if with_sample:
                print("\n[4/4] 테스트용 교사 3명(교감, 학년부장, 담임) 및 학생 16명(1반 8명, 2반 8명) 주입...")
                
                # 1) 교사 등록: 교감, 학년부장, 담임 (+ 2반 담임 보조)
                sample_teachers = [
                    {
                        "id": "vp",
                        "name": "박교감",
                        "subject": "공통",
                        "position": "교감",
                        "department": "교무기획부",
                        "grade": None,
                        "class": None,
                        "email": "vp@bssm.hs.kr",
                    },
                    {
                        "id": "head1",
                        "name": "김부장",
                        "subject": "전공",
                        "position": "학년부장",
                        "department": "마이스터부",
                        "grade": 1,
                        "class": None,
                        "email": "head1@bssm.hs.kr",
                    },
                    {
                        "id": "teacher1",
                        "name": "이담임",
                        "subject": "전공",
                        "position": "담임",
                        "department": "마이스터부",
                        "grade": 1,
                        "class": 1,
                        "email": "teacher1@bssm.hs.kr",
                    },
                    {
                        "id": "teacher2",
                        "name": "최담임",
                        "subject": "보통",
                        "position": "담임",
                        "department": "교육정보부",
                        "grade": 1,
                        "class": 2,
                        "email": "teacher2@bssm.hs.kr",
                    },
                ]

                for t in sample_teachers:
                    # 1) 유저 계정 확인 및 갱신/생성
                    await cur.execute("SELECT uuid FROM users WHERE id = %s", (t["id"],))
                    u_row = await cur.fetchone()
                    if u_row:
                        t_uuid = u_row["uuid"]
                        await cur.execute(
                            "UPDATE users SET password = %s, role = 1 WHERE uuid = %s",
                            (hash_password(teacher_pass), t_uuid),
                        )
                    else:
                        await cur.execute("SELECT uuid FROM users WHERE email = %s", (t["email"],))
                        u_by_email = await cur.fetchone()
                        if u_by_email:
                            t_uuid = u_by_email["uuid"]
                            await cur.execute(
                                "UPDATE users SET id = %s, password = %s, role = 1 WHERE uuid = %s",
                                (t["id"], hash_password(teacher_pass), t_uuid),
                            )
                        else:
                            t_uuid = str(uuid.uuid4())
                            await cur.execute(
                                "INSERT INTO users (id, password, role, uuid) VALUES (%s, %s, 1, %s)",
                                (t["id"], hash_password(teacher_pass), t_uuid),
                            )

                    # 2) 교사 프로필 확인 및 갱신/생성
                    await cur.execute(
                        "SELECT teachers_id FROM teachers WHERE uuid = %s OR email = %s LIMIT 1",
                        (t_uuid, t["email"]),
                    )
                    t_row = await cur.fetchone()
                    if not t_row:
                        await cur.execute(
                            """
                            INSERT INTO teachers (name, subject, position, department, grade, class, email, uuid)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (t["name"], t["subject"], t["position"], t["department"], t["grade"], t["class"], t["email"], t_uuid),
                        )
                        print(f"  [+] 교사 생성: ID='{t['id']}', 성명='{t['name']}', 직책='{t['position']}', 담당='{t['grade'] or '전'}학년 {t['class'] or '전'}반'")
                    else:
                        await cur.execute(
                            """
                            UPDATE teachers 
                            SET name = %s, subject = %s, position = %s, department = %s, grade = %s, class = %s, email = %s, uuid = %s
                            WHERE teachers_id = %s
                            """,
                            (t["name"], t["subject"], t["position"], t["department"], t["grade"], t["class"], t["email"], t_uuid, t_row["teachers_id"]),
                        )
                        print(f"  [*] 교사 갱신: ID='{t['id']}', 성명='{t['name']}' ({t['position']})")

                # 2) 학생 16명 등록 (1학년 1반 8명, 1학년 2반 8명)
                sample_students = [
                    # 1학년 1반 (8명)
                    {"id": "student1", "name": "홍길동", "grade": 1, "class": 1, "number": 1, "email": "student1@bssm.hs.kr"},
                    {"id": "student2", "name": "강민준", "grade": 1, "class": 1, "number": 2, "email": "student2@bssm.hs.kr"},
                    {"id": "student3", "name": "김서연", "grade": 1, "class": 1, "number": 3, "email": "student3@bssm.hs.kr"},
                    {"id": "student4", "name": "박도윤", "grade": 1, "class": 1, "number": 4, "email": "student4@bssm.hs.kr"},
                    {"id": "student5", "name": "이지우", "grade": 1, "class": 1, "number": 5, "email": "student5@bssm.hs.kr"},
                    {"id": "student6", "name": "정시우", "grade": 1, "class": 1, "number": 6, "email": "student6@bssm.hs.kr"},
                    {"id": "student7", "name": "최하은", "grade": 1, "class": 1, "number": 7, "email": "student7@bssm.hs.kr"},
                    {"id": "student8", "name": "한지호", "grade": 1, "class": 1, "number": 8, "email": "student8@bssm.hs.kr"},
                    # 1학년 2반 (8명)
                    {"id": "student9", "name": "윤도현", "grade": 1, "class": 2, "number": 1, "email": "student9@bssm.hs.kr"},
                    {"id": "student10", "name": "장민서", "grade": 1, "class": 2, "number": 2, "email": "student10@bssm.hs.kr"},
                    {"id": "student11", "name": "임예준", "grade": 1, "class": 2, "number": 3, "email": "student11@bssm.hs.kr"},
                    {"id": "student12", "name": "오서윤", "grade": 1, "class": 2, "number": 4, "email": "student12@bssm.hs.kr"},
                    {"id": "student13", "name": "서건우", "grade": 1, "class": 2, "number": 5, "email": "student13@bssm.hs.kr"},
                    {"id": "student14", "name": "신채원", "grade": 1, "class": 2, "number": 6, "email": "student14@bssm.hs.kr"},
                    {"id": "student15", "name": "권유준", "grade": 1, "class": 2, "number": 7, "email": "student15@bssm.hs.kr"},
                    {"id": "student16", "name": "황서진", "grade": 1, "class": 2, "number": 8, "email": "student16@bssm.hs.kr"},
                ]

                for s in sample_students:
                    # 1) 유저 계정 확인 및 갱신/생성
                    await cur.execute("SELECT uuid FROM users WHERE id = %s", (s["id"],))
                    u_row = await cur.fetchone()
                    if u_row:
                        s_uuid = u_row["uuid"]
                        await cur.execute(
                            "UPDATE users SET password = %s, role = 0 WHERE uuid = %s",
                            (hash_password(student_pass), s_uuid),
                        )
                    else:
                        await cur.execute("SELECT uuid FROM users WHERE email = %s", (s["email"],))
                        u_by_email = await cur.fetchone()
                        if u_by_email:
                            s_uuid = u_by_email["uuid"]
                            await cur.execute(
                                "UPDATE users SET id = %s, password = %s, role = 0 WHERE uuid = %s",
                                (s["id"], hash_password(student_pass), s_uuid),
                            )
                        else:
                            s_uuid = str(uuid.uuid4())
                            await cur.execute(
                                "INSERT INTO users (id, password, role, uuid) VALUES (%s, %s, 0, %s)",
                                (s["id"], hash_password(student_pass), s_uuid),
                            )

                    # 2) 학생 프로필 확인 및 갱신/생성
                    await cur.execute(
                        "SELECT student_id FROM students WHERE uuid = %s OR email = %s LIMIT 1",
                        (s_uuid, s["email"]),
                    )
                    st_row = await cur.fetchone()
                    if not st_row:
                        await cur.execute(
                            """
                            INSERT INTO students (name, email, uuid)
                            VALUES (%s, %s, %s)
                            """,
                            (s["name"], s["email"], s_uuid),
                        )
                        st_id = cur.lastrowid
                        print(f"  [+] 학생 생성: ID='{s['id']}', 성명='{s['name']}', 학적='{s['grade']}학년 {s['class']}반 {s['number']}번'")
                    else:
                        st_id = st_row["student_id"]
                        await cur.execute(
                            """
                            UPDATE students 
                            SET name = %s, email = %s, uuid = %s
                            WHERE student_id = %s
                            """,
                            (s["name"], s["email"], s_uuid, st_id),
                        )
                        print(f"  [*] 학생 갱신: ID='{s['id']}', 성명='{s['name']}' ({s['grade']}학년 {s['class']}반 {s['number']}번)")

                    # 3) 학적 레코드 확인 및 갱신/생성
                    await cur.execute(
                        "SELECT record_id FROM student_academic_records WHERE student_id = %s AND year_id = %s LIMIT 1",
                        (st_id, year_id),
                    )
                    sar_row = await cur.fetchone()
                    if not sar_row:
                        await cur.execute(
                            """
                            INSERT INTO student_academic_records (student_id, year_id, grade, class, number)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (st_id, year_id, s["grade"], s["class"], s["number"]),
                        )
                    else:
                        await cur.execute(
                            """
                            UPDATE student_academic_records 
                            SET grade = %s, class = %s, number = %s
                            WHERE record_id = %s
                            """,
                            (s["grade"], s["class"], s["number"], sar_row["record_id"]),
                        )
            else:
                print("\n[4/4] 샘플 데이터 주입 건너뜀 (필요 시 --with-sample-data 옵션 사용)")

            await conn.commit()
            print("\n🎉 모든 초기 시드 데이터가 성공적으로 커밋되었습니다!")
            return True

    except Exception as e:
        await conn.rollback()
        print(f"\n[FAIL] 시드 데이터 주입 중 오류 발생 (롤백 완료): {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="마이스터역량인증제 초기 데이터 시드 주입")
    parser.add_argument("--admin-id", default=os.getenv("ADMIN_ID", "admin"), help="최초 관리자 아이디 (기본: admin)")
    parser.add_argument("--admin-pass", default=os.getenv("ADMIN_PASSWORD", "admin123!"), help="최초 관리자 비밀번호 (기본: admin123!)")
    parser.add_argument("--teacher-pass", default=os.getenv("TEACHER_PASSWORD", "password123!"), help="샘플 교사 비밀번호")
    parser.add_argument("--student-pass", default=os.getenv("STUDENT_PASSWORD", "password123!"), help="샘플 학생 비밀번호")
    parser.add_argument("--year", type=int, default=2026, help="최초 활성화 학년도 (기본: 2026)")
    parser.add_argument("--with-sample-data", action="store_true", help="테스트용 교사/학생 계정 추가 생성 여부")
    args = parser.parse_args()

    success = asyncio.run(seed_data(args.admin_id, args.admin_pass, args.year, args.with_sample_data, args.teacher_pass, args.student_pass))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
