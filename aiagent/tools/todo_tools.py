from __future__ import annotations

from typing import Any, Dict, List

from .common import ToolError, failure, success


def build_todo_tool_schemas() -> List[Dict[str, Any]]:
    return [
        {
            "name": "todo",
            "description": "Update the current todo list for multi-step tasks.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["id", "text", "status"],
                        },
                    }
                },
                "required": ["items"],
            },
        }
    ]


class TodoManager:
    def __init__(self):
        self.items: List[Dict[str, str]] = []

    def update(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            if len(items) > 20:
                raise ToolError("Max 20 todos allowed")
            validated = []
            in_progress_count = 0
            for index, item in enumerate(items):
                text = str(item.get("text", "")).strip()
                status = str(item.get("status", "pending")).lower()
                item_id = str(item.get("id", str(index + 1)))
                if not text:
                    raise ToolError(f"Item {item_id}: text required")
                if status not in ("pending", "in_progress", "completed"):
                    raise ToolError(f"Item {item_id}: invalid status '{status}'")
                if status == "in_progress":
                    in_progress_count += 1
                validated.append({"id": item_id, "text": text, "status": status})
            if in_progress_count > 1:
                raise ToolError("Only one task can be in_progress at a time")
            self.items = validated
            return success("todo", content=self.render(), items=self.items)
        except ToolError as exc:
            return failure("todo", str(exc))

    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for item in self.items if item["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)
