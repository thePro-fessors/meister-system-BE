"""
core/pdf.py - 마이스터역량인증제 PDF 리포트 생성 및 폰트 관리 모듈

대회 공식 요구사항 명세서(5.1, 7.4, 7.5) 준수:
- 1페이지 A4 규격 종합 통계 리포트 렌더링
- TTF 폰트(fonts.ttf / Pretendard GOV) 등록 및 관리
- 벡터 차트(수직 막대 그래프, 수평 막대 그래프) 및 요약 테이블 렌더링
"""

import io
import os
from datetime import datetime
from typing import Any, Dict, List, Optional


_FONT_REGISTERED = False
KOREAN_FONT_NAME = "PretendardGOV"


def ensure_korean_font_registered() -> str:
    """
    ReportLab에 한글 TTF 폰트(fonts.ttf 우선, Pretendard GOV 폴백)를 안전하게 1회 등록합니다.
    """
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return KOREAN_FONT_NAME

    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)

        font_candidate_paths = [
            os.path.join(project_root, "fonts.ttf"),
            os.path.join(project_root, "assets", "fonts", "fonts.ttf"),
            os.path.join(current_dir, "fonts.ttf"),
            os.path.join(project_root, "assets", "fonts", "PretendardGOV.ttf"),
            "/Users/satellite/Library/Fonts/PretendardGOVVariable.ttf",
            "/Library/Fonts/PretendardGOVVariable.ttf",
            "/System/Library/Fonts/PretendardGOVVariable.ttf",
        ]

        chosen_path = None
        for p in font_candidate_paths:
            if os.path.exists(p):
                chosen_path = p
                break

        if chosen_path:
            pdfmetrics.registerFont(TTFont(KOREAN_FONT_NAME, chosen_path))
            _FONT_REGISTERED = True
            return KOREAN_FONT_NAME
    except Exception:
        pass

    return "Helvetica-Bold"


def generate_stats_pdf(
    target_year: int,
    total_students: int,
    cert_counts: Dict[str, int],
    grade_counts: Optional[Dict[str, int]] = None,
    area_averages: Optional[List[Dict[str, Any]]] = None,
) -> bytes:
    """
    마이스터역량인증제 대회 공식 요구사항 명세서(5.1, 7.4, 7.5) 준수 1페이지 한글 종합 통계 리포트 PDF 생성
    - ReportLab 기반 고품질 렌더링 + TTF 한글 폰트 적용
    - A4 규격 (595 x 842 pt)
    - 상단: 부산소프트웨어마이스터고 공식 배너 및 핵심 인증 지표 KPI 카드 (전교생, 인증률, 판정기준)
    - 중단 좌측: [Chart 1] 전체 인증 상태 분포 수직 막대 그래프 (인증 가능, 검토중, 보완 필요, 미달성)
    - 중단 우측: [Chart 2] 5대 인증 영역별 평균 점수 달성률 수평 막대 그래프 (직업기초, 전문기술, 인성, 인문, 외국어)
    - 하단: 성취 등급(S / A / B / 미달성) 환산 분포 및 인증 판정 기준(전 영역 A 이상) 안내 요약 테이블
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.colors import HexColor

    font_name = ensure_korean_font_registered()

    g_counts = grade_counts or {"S": 0, "A": 0, "B": 0, "미달성": 0}
    a_avgs = area_averages or [
        {"name": "직업기초능력", "avg_score": 35.0, "max_score": 40.0, "ratio": 87.5},
        {"name": "전문기술역량", "avg_score": 98.0, "max_score": 120.0, "ratio": 81.7},
        {"name": "인성/직업의식", "avg_score": 72.0, "max_score": 80.0, "ratio": 90.0},
        {"name": "인문학적 소양", "avg_score": 68.0, "max_score": 80.0, "ratio": 85.0},
        {"name": "외국어 능력", "avg_score": 62.0, "max_score": 80.0, "ratio": 77.5},
    ]

    cert_pass = cert_counts.get("인증 가능", 0)
    pass_rate = (cert_pass / total_students * 100.0) if total_students > 0 else 0.0

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))

    # --------------------------------------------------------------------------
    # 1. 헤더: 학교 공식 배너
    # --------------------------------------------------------------------------
    c.setFillColor(HexColor("#102136"))
    c.rect(0, 765, 595, 77, fill=1, stroke=0)
    c.setFillColor(HexColor("#D9A722"))
    c.rect(0, 762, 595, 3, fill=1, stroke=0)

    c.setFont(font_name, 16)
    c.setFillColor(HexColor("#FFFFFF"))
    c.drawString(40, 805, "부산소프트웨어마이스터고등학교")

    c.setFont(font_name, 12)
    c.setFillColor(HexColor("#E0E8F8"))
    c.drawString(40, 782, f"마이스터역량인증제 종합 통계 보고서 - {target_year}학년도")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.setFont(font_name, 9)
    c.setFillColor(HexColor("#BFD0EC"))
    c.drawRightString(555, 782, f"출력일시: {now_str}")

    # --------------------------------------------------------------------------
    # 2. KPI 요약 카드 (3개 블록)
    # --------------------------------------------------------------------------
    cards = [
        ("총 재학생 수", f"{total_students}명", f"{target_year}학년도 전체 학생", HexColor("#F2F5FA")),
        ("최종 인증 통과율", f"{pass_rate:.1f}%", f"{cert_pass}명 / {total_students}명 충족", HexColor("#EBF6EE")),
        ("단계별 인증 판정 기준", "전 영역 A등급 이상", "1~3단계 필수 인증 요건", HexColor("#F5F3FA")),
    ]
    card_x = 40.0
    card_w = 163.0
    card_h = 50.0
    card_y = 702.0

    for i, (title, main_val, sub_val, bg_col) in enumerate(cards):
        cx = card_x + (i * 176.0)
        c.setFillColor(bg_col)
        c.setStrokeColor(HexColor("#D2D6E0"))
        c.setLineWidth(0.8)
        c.rect(cx, card_y, card_w, card_h, fill=1, stroke=1)

        c.setFont(font_name, 8)
        c.setFillColor(HexColor("#555555"))
        c.drawString(cx + 10, card_y + 34, title)

        c.setFont(font_name, 13)
        c.setFillColor(HexColor("#1A2538"))
        c.drawString(cx + 10, card_y + 18, main_val)

        c.setFont(font_name, 7)
        c.setFillColor(HexColor("#777777"))
        c.drawString(cx + 10, card_y + 6, sub_val)

    # --------------------------------------------------------------------------
    # 3. 중단 좌측: [Chart 1] 전체 인증 상태 분포 (Vertical Bar Chart)
    # --------------------------------------------------------------------------
    c1_x = 40.0
    c1_y = 445.0
    c1_w = 250.0
    c1_h = 242.0

    c.setFillColor(HexColor("#FCFCFD"))
    c.setStrokeColor(HexColor("#D8DAE2"))
    c.setLineWidth(0.8)
    c.rect(c1_x, c1_y, c1_w, c1_h, fill=1, stroke=1)

    c.setFont(font_name, 10)
    c.setFillColor(HexColor("#1A2538"))
    c.drawString(c1_x + 12, c1_y + c1_h - 20, "전체 인증 상태 분포")

    c1_plot_x = c1_x + 36.0
    c1_plot_y = c1_y + 36.0
    c1_plot_w = 200.0
    c1_plot_h = 165.0

    c1_categories = [
        ("인증 가능", cert_counts.get("인증 가능", 0), HexColor("#2E7D32")),
        ("검토중", cert_counts.get("검토중", 0), HexColor("#1976D2")),
        ("보완 필요", cert_counts.get("보완 필요", 0), HexColor("#F57C00")),
        ("미달성", cert_counts.get("미달성", 0), HexColor("#D32F2F")),
    ]
    c1_max_val = max(c[1] for c in c1_categories) if any(c[1] > 0 for c in c1_categories) else 1
    c1_y_max = max(10, int(c1_max_val * 1.25))

    # Y축 눈금선 (4등분)
    c.setStrokeColor(HexColor("#E5E7EB"))
    c.setLineWidth(0.5)
    for i in range(5):
        val = int(c1_y_max * i / 4)
        y_pos = c1_plot_y + (c1_plot_h * i / 4)
        c.line(c1_plot_x, y_pos, c1_plot_x + c1_plot_w, y_pos)
        c.setFont(font_name, 7)
        c.setFillColor(HexColor("#666666"))
        c.drawRightString(c1_plot_x - 4, y_pos - 2.5, str(val))

    # X, Y축 메인 라인
    c.setStrokeColor(HexColor("#666666"))
    c.setLineWidth(1)
    c.line(c1_plot_x, c1_plot_y, c1_plot_x + c1_plot_w, c1_plot_y)
    c.line(c1_plot_x, c1_plot_y, c1_plot_x, c1_plot_y + c1_plot_h)

    # 막대 렌더링
    c1_slot_w = c1_plot_w / len(c1_categories)
    c1_bar_w = 32.0

    for idx, (label, count, bar_col) in enumerate(c1_categories):
        bx = c1_plot_x + (idx * c1_slot_w) + (c1_slot_w - c1_bar_w) / 2.0
        bh = (count / c1_y_max) * c1_plot_h if c1_y_max > 0 else 0
        bh = max(2.0, bh) if count > 0 else 0.0

        if bh > 0:
            c.setFillColor(bar_col)
            c.rect(bx, c1_plot_y, c1_bar_w, bh, fill=1, stroke=0)
            c.setFont(font_name, 8)
            c.setFillColor(HexColor("#1A2538"))
            c.drawCentredString(bx + (c1_bar_w / 2.0), c1_plot_y + bh + 4, str(count))

        c.setFont(font_name, 7)
        c.setFillColor(HexColor("#333333"))
        c.drawCentredString(bx + (c1_bar_w / 2.0), c1_plot_y - 14, label)

    # --------------------------------------------------------------------------
    # 4. 중단 우측: [Chart 2] 5대 영역별 평균 성취율 (Horizontal Bar Chart)
    # --------------------------------------------------------------------------
    c2_x = 305.0
    c2_y = 445.0
    c2_w = 250.0
    c2_h = 242.0

    c.setFillColor(HexColor("#FCFCFD"))
    c.setStrokeColor(HexColor("#D8DAE2"))
    c.setLineWidth(0.8)
    c.rect(c2_x, c2_y, c2_w, c2_h, fill=1, stroke=1)

    c.setFont(font_name, 10)
    c.setFillColor(HexColor("#1A2538"))
    c.drawString(c2_x + 12, c2_y + c2_h - 20, "5대 역량 영역별 평균 달성률")

    c2_plot_x = c2_x + 85.0
    c2_plot_y = c2_y + 36.0
    c2_plot_w = 145.0
    c2_plot_h = 165.0

    # 가이드 눈금선
    c.setStrokeColor(HexColor("#E5E7EB"))
    c.setLineWidth(0.5)
    for pct in (0, 50, 70, 80, 90, 100):
        gx = c2_plot_x + (c2_plot_w * pct / 100.0)
        c.line(gx, c2_plot_y, gx, c2_plot_y + c2_plot_h)
        c.setFont(font_name, 6)
        c.setFillColor(HexColor("#666666"))
        c.drawCentredString(gx, c2_plot_y - 12, f"{pct}%")

    # A등급(80%) 기준선 강조 표시
    a_line_x = c2_plot_x + (c2_plot_w * 0.8)
    c.setStrokeColor(HexColor("#E53935"))
    c.setLineWidth(0.8)
    c.setDash([2, 2], 0)
    c.line(a_line_x, c2_plot_y, a_line_x, c2_plot_y + c2_plot_h)
    c.setDash([], 0)

    c.setFont(font_name, 6)
    c.setFillColor(HexColor("#D32F2F"))
    c.drawCentredString(a_line_x, c2_plot_y + c2_plot_h + 3, "기준 80%(A)")

    n_areas = min(5, len(a_avgs))
    slot_h = c2_plot_h / n_areas
    bar_h = 18.0

    for idx, a_info in enumerate(a_avgs[:5]):
        ay = c2_plot_y + c2_plot_h - ((idx + 1) * slot_h) + (slot_h - bar_h) / 2.0
        r_val = float(a_info.get("ratio", 0.0))
        bw = (r_val / 100.0) * c2_plot_w if c2_plot_w > 0 else 0
        bw = max(2.0, min(bw, c2_plot_w))

        if r_val >= 90.0:
            bar_col = HexColor("#2E7D32")
        elif r_val >= 80.0:
            bar_col = HexColor("#388E3C")
        elif r_val >= 70.0:
            bar_col = HexColor("#F57C00")
        else:
            bar_col = HexColor("#D32F2F")

        c.setFillColor(bar_col)
        c.rect(c2_plot_x, ay, bw, bar_h, fill=1, stroke=0)

        aname = a_info.get("name", f"영역 {idx+1}")
        c.setFont(font_name, 7)
        c.setFillColor(HexColor("#333333"))
        c.drawString(c2_x + 8, ay + 5, aname)

        c.setFont(font_name, 7)
        c.setFillColor(HexColor("#111111"))
        c.drawString(c2_plot_x + bw + 4, ay + 5, f"{r_val:.1f}%")

    # --------------------------------------------------------------------------
    # 5. 하단: 등급 환산 기준표 및 통계 요약 테이블
    # --------------------------------------------------------------------------
    t_x = 40.0
    t_y = 398.0
    t_w = 515.0

    c.setFillColor(HexColor("#102136"))
    c.rect(t_x, t_y - 20, t_w, 20, fill=1, stroke=0)

    c.setFont(font_name, 8)
    c.setFillColor(HexColor("#FFFFFF"))
    c.drawString(t_x + 12, t_y - 14, "성취 등급")
    c.drawString(t_x + 170, t_y - 14, "등급 환산 기준")
    c.drawString(t_x + 320, t_y - 14, "현재 인원 / 비율")
    c.drawString(t_x + 440, t_y - 14, "인증 판정 결과")

    grade_table_rows = [
        ("S등급 (최우수)", "영역 최대점의 90% 이상", g_counts.get("S", 0), "모두 S: 특별 표창", HexColor("#2E7D32")),
        ("A등급 (우수)", "영역 최대점의 80% 이상", g_counts.get("A", 0), "전 영역 A 이상: 인증 통과", HexColor("#388E3C")),
        ("B등급 (보통)", "영역 최대점의 70% 이상", g_counts.get("B", 0), "보완 필요 (추가 증빙)", HexColor("#F57C00")),
        ("미달성 (기준미달)", "B등급 기준 미만 (70% 미만)", g_counts.get("미달성", 0), "인증 미달성 (보완 요망)", HexColor("#D32F2F")),
    ]

    curr_ty = t_y - 20
    for g_title, criteria_text, cnt, cert_note, ind_col in grade_table_rows:
        curr_ty -= 20
        c.setStrokeColor(HexColor("#E0E2E8"))
        c.setLineWidth(0.5)
        c.rect(t_x, curr_ty, t_w, 20, fill=0, stroke=1)

        c.setFillColor(ind_col)
        c.rect(t_x + 12, curr_ty + 5, 9, 9, fill=1, stroke=0)

        c.setFont(font_name, 8)
        c.setFillColor(HexColor("#222222"))
        c.drawString(t_x + 26, curr_ty + 6, g_title)

        c.setFont(font_name, 7.5)
        c.setFillColor(HexColor("#444444"))
        c.drawString(t_x + 170, curr_ty + 6, criteria_text)

        g_pct = (cnt / total_students * 100.0) if total_students > 0 else 0.0
        c.drawString(t_x + 320, curr_ty + 6, f"{cnt}명 ({g_pct:.1f}%)")

        c.setFont(font_name, 7)
        c.setFillColor(HexColor("#666666"))
        c.drawString(t_x + 440, curr_ty + 6, cert_note)

    # --------------------------------------------------------------------------
    # 6. 푸터: 공식 인증 규정 및 안내 문구
    # --------------------------------------------------------------------------
    c.setStrokeColor(HexColor("#D8DAE0"))
    c.setLineWidth(0.5)
    c.line(40, 280, 555, 280)

    c.setFont(font_name, 7.5)
    c.setFillColor(HexColor("#555555"))
    c.drawString(40, 262, "* 인증 판정 규칙: 2026학년도 부산소프트웨어마이스터고 마이스터역량인증제 운영계획에 따라, 전 영역 A등급 이상 취득 시 인증이 부여됩니다.")
    c.drawString(40, 248, "* 상벌점은 인성/직업의식 영역 및 최종 총점에 원자적으로 가감 반영되며, 학생별 세부 원천 데이터는 CSV/XLSX로 다운로드할 수 있습니다.")

    c.setFont(font_name, 7.5)
    c.setFillColor(HexColor("#777777"))
    c.drawString(40, 234, "시스템 버전: v1.0.0 (FastAPI Production Engine) | 부산소프트웨어마이스터고등학교 마이스터교육부")

    c.save()
    return buf.getvalue()


# 하위 호환 별칭
_ensure_korean_font_registered = ensure_korean_font_registered
_generate_stats_pdf = generate_stats_pdf
