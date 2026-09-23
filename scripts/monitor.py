#!/usr/bin/env python3
"""Meister 관제 터미널 모니터 (scripts/monitor.py)

서버(FastAPI)가 UDP(127.0.0.1:9999)로 브로드캐스팅하는 JSON 로그를 수신하여
Rich 라이브러리로 시각화합니다.
향후 UDP -> WebSocket 브릿지로 확장하여 웹 대시보드로도 연결 가능합니다.
"""

import socket
import json
import sys

from rich.console import Console
from rich.panel import Panel

UDP_IP = "127.0.0.1"
UDP_PORT = 5140

console = Console()


def start_monitor():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((UDP_IP, UDP_PORT))
    except Exception as e:
        console.print(Panel(
            f"""[bold red]소켓 바인딩 중 오류가 발생했습니다.[/bold red]

[green]수신 주소: {UDP_IP}[/green]
[green]수신 포트: {UDP_PORT}[/green]
            
[red]에러[/red] : {e}""",
            title = "[bold red]실행중 오류가 발생했습니다[/bold red]",
            border_style="red"
        ))
        sys.exit(1)

    console.print(
        Panel(
            f"[bold green]🛰️ Meister 백엔드 UDP 관제 모니터 가동 중[/bold green]\n"
            f"[dim]• 수신 주소:[/dim] udp://{UDP_IP}:{UDP_PORT}\n"
            f"[dim]• 기능:[/dim] 실시간 오류사항 수신 및 개발용 OTP 코드 수신, 관리자 인증코드 수신\n"
            f"[dim]• 종료:[/dim] Ctrl + C",
            title="[bold cyan]Meister Realtime Log Monitor[/bold cyan]",
            border_style="cyan"
        )
    )

    try:
        while True:
            data, _ = sock.recvfrom(65535)
            line = data.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                log = json.loads(line)
            except Exception:
                console.print(f"[dim]{line}[/dim]")
                continue

            level = log.get("level", "INFO")
            msg = log.get("message", "")
            time_str = log.get("time", "")
            pathname = log.get("pathname", "")
            lineno = log.get("lineno", "")
            extra = log.get("extra")

            # 1. OTP 로그 전용 패널
            if extra and isinstance(extra, dict) and extra.get("type") == "OTP":
                email = extra.get("email")
                otp = extra.get("otp")
                console.print(
                    Panel(
                        f"[bold cyan]수신 대상:[/bold cyan] {email}\n"
                        f"[bold green]인증 번호:[/bold green] [bold yellow on black] {otp} [/bold yellow on black]  [dim](유효: 5분)[/dim]\n"
                        f"[dim]발송 시각: {time_str}[/dim]",
                        title="[bold blue]📨 DEV OTP 발송 감지[/bold blue]",
                        border_style="bright_blue",
                        expand=False
                    )
                )
            # 2. ERROR / CRITICAL 경고 패널
            elif level in ["ERROR", "CRITICAL"]:
                console.print(
                    Panel(
                        f"[bold red]{msg}[/bold red]\n\n"
                        f"[dim]발생 위치: {pathname}:{lineno}\n시각: {time_str}[/dim]",
                        title=f"[bold white on red] 🚨 ALERT [{level}] [/bold white on red]",
                        border_style="red"
                    )
                )
            # 3. WARNING 로그
            elif level == "WARNING":
                console.print(f"[yellow]⚠️  [{time_str}] [WARN] {msg}[/yellow] [dim]({pathname}:{lineno})[/dim]")
            # 4. 일반 INFO / DEBUG 로그
            else:
                level_color = "green" if level == "INFO" else "white"
                console.print(f"[dim][{time_str}][/dim] [{level_color}][{level}][/{level_color}] {msg}")

    except KeyboardInterrupt:
        console.print("\n[yellow]모니터링을 종료합니다.[/yellow]")
    finally:
        sock.close()


if __name__ == "__main__":
    start_monitor()
