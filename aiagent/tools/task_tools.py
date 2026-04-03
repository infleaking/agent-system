from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .common import ToolError, failure, success


def build_task_tool_schemas() -> List[Dict[str, Any]]:
    return [
        {
            "name": "task_create",
            "description": "Create a new persistent task.",
            "input_schema": {
                "type": "object",
                "properties": {"subject": {"type": "string"}, "description": {"type": "string"}},
                "required": ["subject"],
            },
        },
        {
            "name": "task_update",
            "description": "Update a task's status or dependencies.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    "addBlockedBy": {"type": "array", "items": {"type": "integer"}},
                    "addBlocks": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["task_id"],
            },
        },
        {
            "name": "task_list",
            "description": "List all persistent tasks.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "task_get",
            "description": "Get one task by id.",
            "input_schema": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        },
    ]


class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        ids = [int(path.stem.split("_")[1]) for path in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> Dict[str, Any]:
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ToolError(f"Task {task_id} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, task: Dict[str, Any]) -> None:
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2), encoding="utf-8")

    def create(self, subject: str, description: str = "") -> Dict[str, Any]:
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "blockedBy": [],
            "blocks": [],
            "owner": "",
        }
        self._save(task)
        self._next_id += 1
        return success("task_create", task=task, content=json.dumps(task, indent=2))

    def get(self, task_id: int) -> Dict[str, Any]:
        try:
            task = self._load(task_id)
            return success("task_get", task=task, content=json.dumps(task, indent=2))
        except ToolError as exc:
            return failure("task_get", str(exc), task_id=task_id)

    def _clear_dependency(self, completed_id: int) -> None:
        for path in self.dir.glob("task_*.json"):
            task = json.loads(path.read_text(encoding="utf-8"))
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def update(
        self,
        task_id: int,
        status: str = None,
        addBlockedBy: List[int] = None,
        addBlocks: List[int] = None,
    ) -> Dict[str, Any]:
        try:
            task = self._load(task_id)
            if status:
                if status not in ("pending", "in_progress", "completed"):
                    raise ToolError(f"Invalid status: {status}")
                task["status"] = status
                if status == "completed":
                    self._clear_dependency(task_id)
            if addBlockedBy:
                task["blockedBy"] = list(set(task["blockedBy"] + addBlockedBy))
            if addBlocks:
                task["blocks"] = list(set(task["blocks"] + addBlocks))
                for blocked_id in addBlocks:
                    try:
                        blocked = self._load(blocked_id)
                    except ToolError:
                        continue
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
            self._save(task)
            return success("task_update", task=task, content=json.dumps(task, indent=2))
        except ToolError as exc:
            return failure("task_update", str(exc), task_id=task_id)

    def list_all(self) -> Dict[str, Any]:
        tasks = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.dir.glob("task_*.json"))]
        if not tasks:
            return success("task_list", tasks=[], content="No tasks.")
        lines = []
        for task in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(task["status"], "[?]")
            blocked = f" (blocked by: {task['blockedBy']})" if task.get("blockedBy") else ""
            lines.append(f"{marker} #{task['id']}: {task['subject']}{blocked}")
        return success("task_list", tasks=tasks, content="\n".join(lines))
