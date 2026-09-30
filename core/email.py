import os
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import aiosmtplib
import httpx
from dotenv import load_dotenv

from core.logger import log_otp, logger
import html as html_module

load_dotenv()

# 이메일 공급자 모드: "console" | "smtp" | "brevo"
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "console").lower()

# SMTP 설정 (Gmail, 교내 워크스페이스, Amazon SES, 자체 docker-mailserver 호환)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "부산소프트웨어마이스터고등학교")

# Brevo API 설정 (향후 도메인 연동 시 사용)
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "")


def _create_otp_html(otp_code: str, user_name: str = "사용자") -> str:
    """OTP 이메일 HTML 본문 템플릿"""
    # [보안 1.3] HTML Injection 방어: 사용자 이름 내 특수문자 이스케이프
    user_name = html_module.escape(user_name)
    return f"""
    <!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="ko">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>회원가입 인증번호 안내</title>
  <!-- 웹폰트 지원 메일 클라이언트(Apple Mail, Thunderbird 등)용 CDN -->
  <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard-gov.min.css" />
  <style type="text/css">
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard-gov.min.css');
    body, table, td, p, h1, span {{
      font-family: 'Pretendard GOV', 'Pretendard GOV Variable', 'Pretendard', -apple-system, BlinkMacSystemFont, 'Apple SD Gothic Neo', 'Malgun Gothic', '맑은 고딕', sans-serif !important;
    }}
  </style>
</head>
<body style="margin: 0; padding: 0; background-color: #f6f8fa; font-family: 'Pretendard GOV', 'Pretendard GOV Variable', 'Pretendard', -apple-system, BlinkMacSystemFont, 'Apple SD Gothic Neo', 'Malgun Gothic', '맑은 고딕', sans-serif; -webkit-font-smoothing: antialiased;">

  <!-- 전체 배경 래퍼 테이블 -->
  <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: #f6f8fa; width: 100%; table-layout: fixed;">
    <tr>
      <td align="center" style="padding: 40px 10px;">

        <!-- 본문 컨테이너 (최대 640px) -->
        <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 640px; background-color: #f6f8fa; padding: 20px 40px; box-sizing: border-box;">
          
          <!-- 1. BSSM 심볼 로고 (GitHub Raw 2x PNG -> 100x59 렌더링) -->
          <tr>
            <td align="left" style="padding-bottom: 26px;">
              <img 
                src="https://raw.githubusercontent.com/thePro-fessors/meister-system-BE/main/Vector.png" 
                alt="부산소프트웨어마이스터고등학교" 
                width="100" 
                height="59" 
                style="display: block; width: 100px; height: 59px; border: 0; outline: none; text-decoration: none; -ms-interpolation-mode: bicubic;" 
              />
            </td>
          </tr>

          <!-- 2. 제목 (Pretendard GOV 24px Bold) -->
          <tr>
            <td align="left" style="padding-bottom: 47px;">
              <h1 style="margin: 0; font-family: 'Pretendard GOV', 'Pretendard GOV Variable', 'Pretendard', sans-serif; font-size: 24px; font-weight: 700; line-height: 1.3; color: #191c21; letter-spacing: -0.5px;">
                회원가입 인증번호 안내
              </h1>
            </td>
          </tr>

          <!-- 3. 본문 인사말 (Pretendard GOV 16px Regular) -->
          <tr>
            <td align="left" style="padding-bottom: 40px; font-family: 'Pretendard GOV', 'Pretendard GOV Variable', 'Pretendard', sans-serif; font-size: 16px; line-height: 1.6; color: #191c21;">
              <p style="margin: 0 0 16px 0;">안녕하세요, {user_name}님.</p>
              <p style="margin: 0;">본인확인을 위하여 아래의 6자리 인증코드를 입력해주세요.</p>
            </td>
          </tr>

          <!-- 4. 인증코드 박스 (Pretendard GOV 28px) -->
          <tr>
            <td align="center" style="padding: 10px 0 40px 0;">
              <table role="presentation" border="0" cellpadding="0" cellspacing="0" style="margin: 0 auto; text-align: center;">
                <tr>
                  <td align="center" style="font-family: 'Pretendard GOV', 'Pretendard GOV Variable', 'Pretendard', sans-serif; font-size: 14px; font-weight: 400; color: #191c21; padding-bottom: 8px;">
                    인증코드:
                  </td>
                </tr>
                <tr>
                  <td align="center" style="font-family: 'Pretendard GOV', 'Pretendard GOV Variable', 'Pretendard', sans-serif; font-size: 28px; font-weight: 700; color: #191c21; letter-spacing: 5px;">
                    {otp_code}
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- 5. 유효시간 및 도용 경고 문구 (Pretendard GOV 12px) -->
          <tr>
            <td align="left" style="padding-bottom: 25px; border-bottom: 1px solid #e1e4e8; font-family: 'Pretendard GOV', 'Pretendard GOV Variable', 'Pretendard', sans-serif;">
              <p style="margin: 0 0 6px 0; font-size: 12px; line-height: 1.5; color: #6e7781;">
                인증코드는 5분간 유효합니다.
              </p>
              <p style="margin: 0 0 20px 0; font-size: 12px; line-height: 1.5; color: #6e7781;">
                이 코드를 요청하지 않았다면, 다른 사람이 본인의 명의를 도용하려고 시도하는 것일 수 있습니다.
                <strong style="font-weight: 700; color: #191c21;">절대 다른 사람에게 이 코드를 전달하거나 제공하지 마세요.</strong>
              </p>
            </td>
          </tr>

          <!-- 6. 하단 푸터 (Pretendard GOV 12px) -->
          <tr>
            <td align="left" style="padding-top: 25px; font-family: 'Pretendard GOV', 'Pretendard GOV Variable', 'Pretendard', sans-serif; font-size: 12px; line-height: 1.6; color: #6e7781;">
              <p style="margin: 0 0 6px 0;">이 이메일은 발신 전용으로, 회신하실 수 없습니다.</p>
              <p style="margin: 0 0 6px 0;">
                46708 부산 강서구 가락대로 1393 (봉림동 15)<br />
                교무실(입학처) : 051-971-2153 &nbsp;&nbsp;|&nbsp;&nbsp; 행정실 : 051-971-2152
              </p>
              <p style="margin: 0;">ⓒ 부산소프트웨어마이스터고등학교 all rights reserved.</p>
            </td>
          </tr>

        </table>

      </td>
    </tr>
  </table>

</body>
</html>
    """


async def _send_via_smtp(to_email: str, subject: str, html_content: str):
    """aiosmtplib 기반 비동기 SMTP 발송 (Gmail, SES, 자체 docker-mailserver 등)"""
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("SMTP 설정 누락: SMTP_USER 또는 SMTP_PASSWORD가 설정되지 않았습니다.")

    msg = EmailMessage()
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    
    # ── 표준 메일 헤더 보강 (스팸 필터 감점 방지) ──
    domain = SMTP_USER.split("@")[-1] if "@" in SMTP_USER else "local"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["X-Mailer"] = "Meister-Auth-Mailer/1.0"
    msg["Auto-Submitted"] = "auto-generated"  # 자동 발송 안내 (스팸 점수 개선)

    # 텍스트 대체 본문 및 HTML 본문 (MIME 멀티파트)
    msg.set_content("보안 및 기타 오류를 방지하기 위하여, HTML 뷰어가 제공되지 않는 경우 인증번호를 불러올 수 없습니다.")
    msg.add_alternative(html_content, subtype="html")

    use_tls = (SMTP_PORT == 465)
    start_tls = (SMTP_PORT == 587)

    await aiosmtplib.send(
        msg,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=SMTP_USER,
        password=SMTP_PASSWORD,
        use_tls=use_tls,
        start_tls=start_tls,
        timeout=10,
    )


async def _send_via_brevo(to_email: str, subject: str, html_content: str):
    """Brevo REST API 기반 발송"""
    if not BREVO_API_KEY:
        raise RuntimeError("Brevo 설정 누락: BREVO_API_KEY가 설정되지 않았습니다.")

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "api-key": BREVO_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "sender": {
            "name": SMTP_FROM_NAME,
            "email": BREVO_SENDER_EMAIL or SMTP_USER or "no-reply@meister.domain",
        },
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(url, json=payload, headers=headers)
        if res.status_code >= 400:
            raise RuntimeError(f"Brevo API 전송 실패 ({res.status_code}): {res.text}")


async def send_otp_email(to_email: str, otp_code: str, user_name: str = "사용자") -> None:
    """환경변수 EMAIL_PROVIDER에 따라 OTP 이메일을 비동기로 발송

    Provider 종류:
      - "console": 로컬 로거/UDP 모니터로 전송 (개발/테스트용)
      - "smtp": Gmail / Amazon SES 등 SMTP 비동기 발송
      - "brevo": Brevo HTTP REST API 발송

    추후 docker_mailserver 도입을 통한 자체 발신 시스템을 구현할 수 있습니다.
    따라서, MVP 배포시 docker_mailserver를 서버에 올려 실제로 테스트를 진행할 예정입니다.
    """
    subject = f"[{SMTP_FROM_NAME}] 본인인증 번호 안내 ({otp_code})"
    html_content = _create_otp_html(otp_code, user_name)

    if EMAIL_PROVIDER == "console":
        log_otp(to_email, otp_code)
        return

    try:
        if EMAIL_PROVIDER == "smtp":
            await _send_via_smtp(to_email, subject, html_content)
            return

        if EMAIL_PROVIDER == "brevo":
            await _send_via_brevo(to_email, subject, html_content)
            return

        raise ValueError(f"지원하지 않는 EMAIL_PROVIDER입니다: {EMAIL_PROVIDER}")
    except Exception as exc:
        logger.critical(
            f"[MAIL_DISPATCH_FAILURE] 이메일 발송 실패: to={to_email}, provider={EMAIL_PROVIDER}, error={exc}",
            extra={"extra_data": {"type": "MAIL_FAILURE", "to": to_email, "provider": EMAIL_PROVIDER, "error": str(exc)}}
        )
