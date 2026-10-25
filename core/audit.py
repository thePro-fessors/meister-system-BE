"""
core/audit.py - 시스템 전방위 종합 감사 로그 (System Audit Logs) 공통 서비스
학생, 교원, 관리자, 시스템의 모든 활동(생성, 수정, 삭제, 승인, 반려, 로그인 등)을 실시간 기록합니다.
"""

import json
import logging
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)


async def record_audit_log(
    conn: Any,
    actor: Optional[Dict[str, Any]] = None,
    action_type: str = "UNKNOWN",
    target_type: str = "SYSTEM",
    target_id: Optional[Union[int, str]] = None,
    target_name: Optional[str] = None,
    details: Optional[Union[Dict[str, Any], str]] = None,
    ip_address: Optional[str] = None,
) -> None:
    """
    system_audit_logs 테이블에 감사 로그를 기록합니다.
    트랜잭션 중이든 자동 커밋 모드든 안전하게 동작하며, 오류 발생 시 본 로직의 실패를 야기하지 않도록 방어합니다.
    """
    actor_uuid = None
    actor_id = None
    actor_name = None
    actor_role = "system"

    if actor:
        actor_uuid = actor.get("uuid") or actor.get("sub")
        actor_id = actor.get("id") or actor.get("user_id") or actor.get("studentId") or actor.get("teacherId")
        actor_name = actor.get("name")
        raw_role = actor.get("role")
        if raw_role in (2, "2", "admin"):
            actor_role = "admin"
        elif raw_role in (1, "1", "teacher"):
            actor_role = "teacher"
        elif raw_role in (0, "0", "student"):
            actor_role = "student"
        elif raw_role:
            actor_role = str(raw_role).lower()

    # details 직렬화
    details_json = None
    if details is not None:
        if isinstance(details, (dict, list)):
            try:
                details_json = json.dumps(details, ensure_ascii=False)
            except Exception:
                details_json = str(details)
        else:
            details_json = str(details)

    target_id_str = str(target_id) if target_id is not None else None

    sql = """
        INSERT INTO system_audit_logs (
            actor_uuid, actor_id, actor_name, actor_role,
            action_type, target_type, target_id, target_name,
            ip_address, details, created_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, NOW()
        )
    """

    try:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (
                    actor_uuid,
                    str(actor_id) if actor_id else None,
                    actor_name,
                    actor_role,
                    action_type,
                    target_type,
                    target_id_str,
                    target_name,
                    ip_address,
                    details_json,
                ),
            )
    except Exception as e:
        logger.warning(f"Failed to record system audit log [{action_type}]: {e}")
