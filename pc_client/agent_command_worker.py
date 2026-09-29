"""Run agent commands off the heartbeat thread.

The heartbeat must keep posting while a file download or an installer is in
flight. mark_command_processed stays in front of the side effect: a crash
after the marker must not replay power or lock.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

_REAL_MONOTONIC = time.monotonic
_REAL_SLEEP = time.sleep

CommandHandler = Callable[[dict[str, Any], Any], str | None]


def _agent_module():
    try:
        import client_agent
    except ModuleNotFoundError:
        from pc_client import client_agent
    return client_agent


class CommandExecutor:
    def __init__(self, handler: CommandHandler, *, server_url: str, trust_env: bool) -> None:
        self._handler = handler
        self.server_url = server_url
        self.trust_env = trust_env
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=32)
        self.lock = threading.Lock()
        self.inflight: set[int] = set()
        self.stop_reason: str | None = None
        self.failed_auto_revision = ""
        self._auto_inflight = False
        self._update_running = False
        self._batch = 0
        self._closed = False
        self.thread = threading.Thread(target=self._run, name="xass-agent-commands", daemon=True)
        self.thread.start()

    def next_batch(self) -> int:
        with self.lock:
            self._batch += 1
            return self._batch

    def accept(self, job: dict[str, Any]) -> str:
        command_id = int(job["id"])
        agent = _agent_module()
        with self.lock:
            if command_id in self.inflight:
                return "skipped"
            if not agent._command_needs_execution(command_id):
                return "skipped"
            # Persist before the worker can run the side effect. A crash after
            # this marker must not replay power, lock, or file deletion.
            agent.mark_command_processed(command_id, str(job.get("command") or ""))
            try:
                self.queue.put_nowait(job)
            except queue.Full:
                agent.store_command_result(command_id, False, "агент занят")
                return "busy"
            self.inflight.add(command_id)
            if str(job.get("command") or "") == "update":
                self._update_running = True
            return "queued"

    def complete_duplicate(self, job: dict[str, Any], original_id: int) -> None:
        command_id = int(job["id"])
        agent = _agent_module()
        with self.lock:
            if command_id in self.inflight or command_id == original_id:
                return
            if not agent._command_needs_execution(command_id):
                return
            agent.mark_command_processed(command_id, "update")
            agent.store_command_result(
                command_id,
                False,
                f"Повторный запрос обновления отменён: выполняется команда №{original_id}.",
                {"duplicate_of": original_id},
            )

    def submit_auto(self, job: dict[str, Any]) -> None:
        revision = str(job.get("revision") or "")
        with self.lock:
            if self._update_running or self._auto_inflight or (revision and revision == self.failed_auto_revision):
                return
            item = {"kind": "auto", **job}
            try:
                self.queue.put_nowait(item)
            except queue.Full:
                return
            self._auto_inflight = True

    def wait(self, timeout: float = 2.0) -> bool:
        deadline = _REAL_MONOTONIC() + timeout
        while _REAL_MONOTONIC() < deadline:
            with self.lock:
                idle = self.queue.unfinished_tasks == 0 and not self.inflight and not self._auto_inflight
            if idle:
                return True
            _REAL_SLEEP(0.01)
        return False

    def close(self) -> None:
        self._closed = True
        self.thread.join(timeout=5)

    def _finish(self, job: dict[str, Any]) -> None:
        with self.lock:
            if job.get("kind") == "auto":
                self._auto_inflight = False
            else:
                if str(job.get("command") or "") == "update":
                    self._update_running = False
                command_id = job.get("id")
                if command_id is not None:
                    self.inflight.discard(int(command_id))
        self.queue.task_done()

    def _execute(self, job: dict[str, Any], client: Any) -> None:
        agent = _agent_module()
        try:
            reason = self._handler(job, client)
        except Exception:
            command_id = job.get("id")
            if command_id is not None:
                agent.store_command_result(int(command_id), False, "Команда агента не выполнена")
            reason = None
        if job.get("kind") == "auto":
            if reason:
                self.stop_reason = reason
            else:
                self.failed_auto_revision = str(job.get("revision") or "")
        elif reason:
            self.stop_reason = reason

    def _run(self) -> None:
        agent = _agent_module()
        while not self._closed and self.stop_reason is None:
            client_cm = None
            client = None
            try:
                client_cm = agent.create_http_client(self.server_url, timeout=20, trust_env=self.trust_env)
                client = client_cm.__enter__()
                while not self._closed and self.stop_reason is None:
                    try:
                        job = self.queue.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    try:
                        self._execute(job, client)
                    finally:
                        self._finish(job)
            except Exception:
                print("[pc-client] command worker restarted", flush=True)
                _REAL_SLEEP(0.5)
            finally:
                if client_cm is not None and client is not None:
                    client_cm.__exit__(None, None, None)
