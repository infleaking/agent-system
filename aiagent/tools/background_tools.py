from __future__ import annotations

import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List

from .common import success


def build_background_tool_schemas() -> List[Dict[str, Any]]:
    return [
        {
            "name": "background_run",
            "description": "Run a command in a background thread and return a task id immediately.",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
        {
            "name": "check_background",
            "description": "Check one background task or list all.",
            "input_schema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
            },
        },
    ]


class BackgroundManager:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._notification_queue: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def run(self, command: str) -> Dict[str, Any]:
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        thread = threading.Thread(target=self._execute, args=(task_id, command), daemon=True)
        thread.start()
        return success(
            "background_run",
            task_id=task_id,
            status="running",
            content=f"Background task {task_id} started: {command[:80]}",
        )

    def _execute(self, task_id: str, command: str) -> None:
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as exc:
            output = f"Error: {exc}"
            status = "error"

        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"
        with self._lock:
            self._notification_queue.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "command": command[:80],
                    "result": (output or "(no output)")[:500],
                }
            )

    def check(self, task_id: str = None) -> Dict[str, Any]:
        if task_id:
            task = self.tasks.get(task_id)
            if not task:
                return {"ok": False, "tool": "check_background", "error": f"Unknown task {task_id}"}
            content = f"[{task['status']}] {task['command'][:60]}\n{task.get('result') or '(running)'}"
            return success("check_background", task_id=task_id, task=task, content=content)
        lines = [f"{tid}: [{task['status']}] {task['command'][:60]}" for tid, task in self.tasks.items()]
        return success("check_background", tasks=self.tasks, content="\n".join(lines) if lines else "No background tasks.")

    def drain_notifications(self) -> List[Dict[str, Any]]:
        with self._lock:
            notifications = list(self._notification_queue)
            self._notification_queue.clear()
        return notifications

    def snapshot(self) -> Dict[str, Any]:
        return {
            "count": len(self.tasks),
            "tasks": {
                task_id: {
                    "status": task.get("status"),
                    "command": task.get("command"),
                    "result_preview": (task.get("result") or "")[:200],
                }
                for task_id, task in self.tasks.items()
            },
        }
