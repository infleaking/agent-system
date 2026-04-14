from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from .common import ToolError, failure, success


def build_todo_tool_schemas() -> List[Dict[str, Any]]:
    return [
        {
            "name": "todo",
            "description": "Update the current todo list incrementally. Full-list rewrites are not allowed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": ["add", "set_status", "set_text", "append_note", "remove", "prune_completed"],
                                },
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                                "note": {"type": "string"},
                                "owner": {"type": "string"},
                            },
                            "required": ["op"],
                        },
                    }
                },
                "required": ["operations"],
            },
        },
        {
            "name": "todo_update",
            "description": "Alias for todo incremental updates.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": ["add", "set_status", "set_text", "append_note", "remove", "prune_completed"],
                                },
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                                "note": {"type": "string"},
                                "owner": {"type": "string"},
                            },
                            "required": ["op"],
                        },
                    }
                },
                "required": ["operations"],
            },
        },
    ]


class TodoManager:
    def __init__(self):
        self.items: List[Dict[str, Any]] = []

    def _now(self) -> str:
        return datetime.now().isoformat()

    def _find_index(self, item_id: str) -> int:
        for index, item in enumerate(self.items):
            if item["id"] == item_id:
                return index
        raise ToolError(f"Unknown todo id '{item_id}'")

    def _validate_status(self, status: str) -> str:
        normalized = str(status).strip().lower()
        if normalized not in ("pending", "in_progress", "completed"):
            raise ToolError(f"invalid status '{status}'")
        return normalized

    def _validate_global_state(self) -> None:
        if len(self.items) > 20:
            raise ToolError("Max 20 todos allowed")
        seen = set()
        in_progress_count = 0
        for item in self.items:
            item_id = str(item.get("id", "")).strip()
            if not item_id:
                raise ToolError("todo id required")
            if item_id in seen:
                raise ToolError(f"duplicate todo id '{item_id}'")
            seen.add(item_id)
            text = str(item.get("text", "")).strip()
            if not text:
                raise ToolError(f"Item {item_id}: text required")
            item["status"] = self._validate_status(item.get("status", "pending"))
            if item["status"] == "in_progress":
                in_progress_count += 1
        if in_progress_count > 1:
            raise ToolError("Only one task can be in_progress at a time")

    def _apply_add(self, op: Dict[str, Any]) -> None:
        item_id = str(op.get("id", "")).strip()
        text = str(op.get("text", "")).strip()
        if not item_id:
            raise ToolError("add requires id")
        if not text:
            raise ToolError(f"Item {item_id}: text required")
        if any(item["id"] == item_id for item in self.items):
            raise ToolError(f"todo id '{item_id}' already exists")
        item = {
            "id": item_id,
            "text": text,
            "status": self._validate_status(op.get("status", "pending")),
            "notes": [],
            "owner": str(op.get("owner", "")).strip(),
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        self.items.append(item)

    def _apply_set_status(self, op: Dict[str, Any]) -> None:
        item_id = str(op.get("id", "")).strip()
        if not item_id:
            raise ToolError("set_status requires id")
        index = self._find_index(item_id)
        self.items[index]["status"] = self._validate_status(op.get("status", ""))
        self.items[index]["updated_at"] = self._now()

    def _apply_set_text(self, op: Dict[str, Any]) -> None:
        item_id = str(op.get("id", "")).strip()
        text = str(op.get("text", "")).strip()
        if not item_id:
            raise ToolError("set_text requires id")
        if not text:
            raise ToolError("set_text requires text")
        index = self._find_index(item_id)
        self.items[index]["text"] = text
        self.items[index]["updated_at"] = self._now()

    def _apply_append_note(self, op: Dict[str, Any]) -> None:
        item_id = str(op.get("id", "")).strip()
        note = str(op.get("note", "")).strip()
        if not item_id:
            raise ToolError("append_note requires id")
        if not note:
            raise ToolError("append_note requires note")
        index = self._find_index(item_id)
        notes = self.items[index].setdefault("notes", [])
        notes.append(note)
        self.items[index]["updated_at"] = self._now()

    def _apply_remove(self, op: Dict[str, Any]) -> None:
        item_id = str(op.get("id", "")).strip()
        if not item_id:
            raise ToolError("remove requires id")
        index = self._find_index(item_id)
        if self.items[index]["status"] != "completed":
            raise ToolError("remove is only allowed for completed todo items")
        self.items.pop(index)

    def _apply_prune_completed(self) -> int:
        before = len(self.items)
        self.items = [item for item in self.items if item["status"] != "completed"]
        return before - len(self.items)

    def update(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            if not operations:
                raise ToolError("operations must not be empty")
            applied: List[Dict[str, Any]] = []
            for op in operations:
                op_name = str(op.get("op", "")).strip()
                if not op_name and any(key in op for key in ("id", "text", "status")):
                    raise ToolError("full-list todo rewrites are not allowed; use incremental operations with an 'op' field")
                if op_name == "add":
                    self._apply_add(op)
                elif op_name == "set_status":
                    self._apply_set_status(op)
                elif op_name == "set_text":
                    self._apply_set_text(op)
                elif op_name == "append_note":
                    self._apply_append_note(op)
                elif op_name == "remove":
                    self._apply_remove(op)
                elif op_name == "prune_completed":
                    removed_count = self._apply_prune_completed()
                    op = {**op, "removed_count": removed_count}
                else:
                    raise ToolError(f"unsupported todo op '{op_name}'")
                self._validate_global_state()
                applied.append(op)
            return success("todo", content=self.render(), items=self.items, operations=applied)
        except ToolError as exc:
            return failure("todo", str(exc))

    def update_alias(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        result = self.update(operations)
        result["tool"] = "todo_update"
        return result

    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            notes = f" ({len(item.get('notes', []))} notes)" if item.get("notes") else ""
            lines.append(f"{marker} #{item['id']}: {item['text']}{notes}")
        done = sum(1 for item in self.items if item["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)
