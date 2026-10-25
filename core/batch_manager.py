"""
core/batch_manager.py - 엑셀 대량 등록 비동기 작업 관리 및 SSE 진행률 스트리밍 모듈
"""

import asyncio
import csv
import io
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional


class BatchTaskManager:
    """
    엑셀 대량 등록 비동기 작업 상태 추적 및 SSE 진행률 스트리밍 관리자
    """

    def __init__(self, ttl_seconds: int = 3600):
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self._ttl_seconds = ttl_seconds

    def _get_redis(self):
        try:
            import database
            return database.redis_client
        except Exception:
            return None

    async def _redis_save(self, task_id: str) -> None:
        redis = self._get_redis()
        task = self._tasks.get(task_id)
        if redis and task:
            try:
                import json
                data_str = json.dumps(task, ensure_ascii=False)
                await redis.set(f"batch_task:{task_id}", data_str, ex=self._ttl_seconds)
                await redis.publish(f"batch_task_channel:{task_id}", json.dumps(self._task_summary(task), ensure_ascii=False))
            except Exception:
                pass

    def _trigger_redis_save(self, task_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._redis_save(task_id))
        except RuntimeError:
            pass

    def create_task(self, mode: str, total_count: int, filename: Optional[str] = None) -> str:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        self._tasks[task_id] = {
            "taskId": task_id,
            "mode": mode,
            "filename": filename or "batch_upload",
            "status": "PROCESSING",
            "progress": 0,
            "totalCount": total_count,
            "processedCount": 0,
            "createdCount": 0,
            "updatedCount": 0,
            "skippedCount": 0,
            "parsingErrors": [],
            "error": None,
            "createdAt": time.time(),
            "updatedAt": time.time(),
        }
        self._events[task_id] = asyncio.Event()
        self._cleanup_expired_tasks()
        self._trigger_redis_save(task_id)
        return task_id

    def update_progress(
        self,
        task_id: str,
        processed_count: int,
        created_count: int = 0,
        updated_count: int = 0,
        skipped_count: int = 0,
        parsing_errors: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return

        task["processedCount"] = processed_count
        task["createdCount"] = created_count
        task["updatedCount"] = updated_count
        task["skippedCount"] = skipped_count
        if parsing_errors:
            task["parsingErrors"].extend(parsing_errors)

        total = task["totalCount"]
        task["progress"] = min(100, int((processed_count / total * 100))) if total > 0 else 100
        task["updatedAt"] = time.time()

        # 대기 중인 SSE 리스너에 알림
        event = self._events.get(task_id)
        if event:
            event.set()
        self._trigger_redis_save(task_id)

    def complete_task(
        self,
        task_id: str,
        created_count: int,
        updated_count: int,
        skipped_count: int,
        parsing_errors: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return

        task["status"] = "COMPLETED"
        task["progress"] = 100
        task["processedCount"] = task["totalCount"]
        task["createdCount"] = created_count
        task["updatedCount"] = updated_count
        task["skippedCount"] = skipped_count
        if parsing_errors:
            task["parsingErrors"].extend(parsing_errors)
        task["updatedAt"] = time.time()

        event = self._events.get(task_id)
        if event:
            event.set()
        self._trigger_redis_save(task_id)

    def fail_task(self, task_id: str, error_msg: str) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return

        task["status"] = "FAILED"
        task["error"] = error_msg
        task["updatedAt"] = time.time()

        event = self._events.get(task_id)
        if event:
            event.set()
        self._trigger_redis_save(task_id)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._tasks.get(task_id)

    async def get_task_async(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self._tasks.get(task_id)
        if task:
            return task
        redis = self._get_redis()
        if redis:
            try:
                import json
                raw = await redis.get(f"batch_task:{task_id}")
                if raw:
                    task = json.loads(raw)
                    self._tasks[task_id] = task
                    return task
            except Exception:
                pass
        return None

    async def subscribe_progress(self, task_id: str) -> AsyncGenerator[str, None]:
        """SSE 규격 (data: {JSON}\n\n) 실시간 스트림 생성기"""
        task = await self.get_task_async(task_id)
        if not task:
            for _ in range(15):
                await asyncio.sleep(0.2)
                task = await self.get_task_async(task_id)
                if task:
                    break

        if not task:
            import json
            yield f"data: {json.dumps({'error': 'TASK_NOT_FOUND', 'status': 'NOT_FOUND'})}\n\n"
            return

        import json

        # 최초 상태 즉시 전송
        yield f"data: {json.dumps(self._task_summary(task), ensure_ascii=False)}\n\n"

        if task["status"] in ("COMPLETED", "FAILED"):
            return

        redis = self._get_redis()
        if redis:
            pubsub = None
            try:
                pubsub = redis.pubsub()
                await pubsub.subscribe(f"batch_task_channel:{task_id}")
                while True:
                    try:
                        msg = await asyncio.wait_for(pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0), timeout=3.0)
                        if msg and msg.get("data"):
                            yield f"data: {msg['data']}\n\n"
                            cur_task = await self.get_task_async(task_id)
                            if cur_task and cur_task["status"] in ("COMPLETED", "FAILED"):
                                break
                        else:
                            yield ": keep-alive\n\n"
                            cur_task = await self.get_task_async(task_id)
                            if cur_task:
                                yield f"data: {json.dumps(self._task_summary(cur_task), ensure_ascii=False)}\n\n"
                                if cur_task["status"] in ("COMPLETED", "FAILED"):
                                    break
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        cur_task = await self.get_task_async(task_id)
                        if cur_task:
                            yield f"data: {json.dumps(self._task_summary(cur_task), ensure_ascii=False)}\n\n"
                            if cur_task["status"] in ("COMPLETED", "FAILED"):
                                break
            except Exception:
                pass
            finally:
                if pubsub:
                    try:
                        await pubsub.unsubscribe(f"batch_task_channel:{task_id}")
                        await pubsub.aclose()
                    except Exception:
                        pass
            return

        while True:
            event = self._events.get(task_id)
            if not event:
                break

            try:
                # 3초 타임아웃 또는 이벤트 트리거 대기
                await asyncio.wait_for(event.wait(), timeout=3.0)
                event.clear()
            except asyncio.TimeoutError:
                # Keep-Alive 핑 전송
                yield ": keep-alive\n\n"

            task = self._tasks.get(task_id)
            if not task:
                break

            yield f"data: {json.dumps(self._task_summary(task), ensure_ascii=False)}\n\n"

            if task["status"] in ("COMPLETED", "FAILED"):
                break

    def generate_csv_report(self, task_id: str) -> str:
        """실패 행 상세 사유 CSV 리포트 생성 (UTF-8-BOM)"""
        task = self._tasks.get(task_id)
        if not task:
            return "\ufeff행 번호,성명,실패 사유\n"

        output = io.StringIO()
        output.write("\ufeff")  # Excel 한글 깨짐 방지 BOM
        writer = csv.writer(output)
        writer.writerow(["행 번호", "성명", "실패 사유"])

        for err in task.get("parsingErrors", []):
            writer.writerow([
                err.get("row", "-"),
                err.get("name", "-"),
                err.get("reason", "알 수 없는 오류"),
            ])

        return output.getvalue()

    async def generate_csv_report_async(self, task_id: str) -> str:
        """실패 행 상세 사유 CSV 리포트 비동기 생성 (Redis 지원)"""
        task = await self.get_task_async(task_id)
        if not task:
            return "\ufeff행 번호,성명,실패 사유\n"

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(["행 번호", "성명", "실패 사유"])

        for err in task.get("parsingErrors", []):
            writer.writerow([
                err.get("row", "-"),
                err.get("name", "-"),
                err.get("reason", "알 수 없는 오류"),
            ])

        return output.getvalue()

    def _task_summary(self, task: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "taskId": task["taskId"],
            "mode": task["mode"],
            "status": task["status"],
            "progress": task["progress"],
            "totalCount": task["totalCount"],
            "processedCount": task["processedCount"],
            "createdCount": task["createdCount"],
            "updatedCount": task["updatedCount"],
            "skippedCount": task["skippedCount"],
            "errorCount": len(task.get("parsingErrors", [])),
            "error": task.get("error"),
        }

    def _cleanup_expired_tasks(self) -> None:
        now = time.time()
        expired = [
            tid
            for tid, t in self._tasks.items()
            if now - t.get("createdAt", 0) > self._ttl_seconds
        ]
        for tid in expired:
            self._tasks.pop(tid, None)
            self._events.pop(tid, None)


# 전역 싱글톤 인스턴스
batch_task_manager = BatchTaskManager()
