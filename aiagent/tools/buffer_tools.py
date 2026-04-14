from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .common import ToolError, failure, is_probably_text_file, resolve_path, success


MAX_READ_CHARS = 12000
DEFAULT_READ_CHARS = 4000
BLOCKED_WRITE_NAMES = {".env", ".env.local", ".env.production", ".env.development"}


@dataclass
class FileBuffer:
    buffer_id: str
    path: Path
    original_text: str
    current_text: str
    encoding: str
    newline: str
    had_trailing_newline: bool


def build_buffer_tool_schemas() -> List[Dict[str, Any]]:
    return [
        {
            "name": "open_buffer",
            "description": "Open a file into an editable in-memory buffer.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "get_buffer",
            "description": "Read a chunk from the current in-memory buffer content.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "buffer_id": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_READ_CHARS},
                },
                "required": ["buffer_id"],
            },
        },
        {
            "name": "apply_buffer_patch",
            "description": "Apply structured line-based patch operations to an in-memory buffer.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "buffer_id": {"type": "string"},
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {"type": "string", "enum": ["replace_range", "insert_at", "delete_range"]},
                                "start_line": {"type": "integer", "minimum": 1},
                                "end_line": {"type": "integer", "minimum": 1},
                                "line": {"type": "integer", "minimum": 1},
                                "content": {"type": "string"},
                            },
                            "required": ["op"],
                        },
                    },
                },
                "required": ["buffer_id", "operations"],
            },
        },
        {
            "name": "preview_buffer_diff",
            "description": "Show a unified diff between original and current buffer contents.",
            "input_schema": {
                "type": "object",
                "properties": {"buffer_id": {"type": "string"}},
                "required": ["buffer_id"],
            },
        },
        {
            "name": "save_buffer",
            "description": "Write a buffer back to disk while preserving original newline style.",
            "input_schema": {
                "type": "object",
                "properties": {"buffer_id": {"type": "string"}},
                "required": ["buffer_id"],
            },
        },
        {
            "name": "discard_buffer",
            "description": "Discard an in-memory buffer without saving.",
            "input_schema": {
                "type": "object",
                "properties": {"buffer_id": {"type": "string"}},
                "required": ["buffer_id"],
            },
        },
    ]


class BufferTools:
    def __init__(self, project_root: Path, record_action):
        self.project_root = project_root
        self.record_action = record_action
        self.buffers: Dict[str, FileBuffer] = {}

    def _detect_newline(self, text: str) -> str:
        return "\r\n" if "\r\n" in text else "\n"

    def _read_file_text(self, resolved: Path) -> tuple[str, str, bool]:
        with open(resolved, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        newline = self._detect_newline(text)
        had_trailing_newline = text.endswith("\n") or text.endswith("\r\n")
        normalized = text.replace("\r\n", "\n")
        return normalized, newline, had_trailing_newline

    def _rebuild_output(self, text: str, newline: str, had_trailing_newline: bool) -> str:
        normalized = text.replace("\r\n", "\n")
        rebuilt = normalized.replace("\n", newline)
        if not had_trailing_newline and rebuilt.endswith(newline):
            rebuilt = rebuilt[: -len(newline)]
        if had_trailing_newline and normalized and not rebuilt.endswith(newline):
            rebuilt += newline
        return rebuilt

    def _get_buffer(self, buffer_id: str) -> FileBuffer:
        buffer = self.buffers.get(buffer_id)
        if buffer is None:
            raise ToolError(f"Unknown buffer {buffer_id}")
        return buffer

    def _chunk_text(self, text: str, start_line: int, max_lines: int, max_chars: int) -> Dict[str, Any]:
        start_line = max(1, int(start_line))
        max_lines = max(1, min(int(max_lines), 1000))
        max_chars = max(1, min(int(max_chars), MAX_READ_CHARS))
        lines = text.splitlines()
        start_index = start_line - 1
        chunk_lines = lines[start_index : start_index + max_lines]
        chunk = "\n".join(chunk_lines)
        truncated = False
        if len(chunk) > max_chars:
            chunk = chunk[:max_chars]
            truncated = True
        end_line = start_index + len(chunk_lines)
        return {
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(lines),
            "truncated": truncated,
            "content": chunk,
        }

    def _normalize_operations(self, lines: List[str], operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not operations:
            raise ToolError("operations must not be empty")
        normalized = []
        for op in operations:
            op_name = op.get("op")
            if op_name == "replace_range":
                start = int(op["start_line"])
                end = int(op["end_line"])
                if start < 1 or end < start or end > len(lines):
                    raise ToolError("invalid replace_range bounds")
                normalized.append({"op": op_name, "start": start, "end": end, "content": op.get("content", "")})
            elif op_name == "insert_at":
                line = int(op["line"])
                if line < 1 or line > len(lines) + 1:
                    raise ToolError("invalid insert_at line")
                normalized.append({"op": op_name, "line": line, "content": op.get("content", "")})
            elif op_name == "delete_range":
                start = int(op["start_line"])
                end = int(op["end_line"])
                if start < 1 or end < start or end > len(lines):
                    raise ToolError("invalid delete_range bounds")
                normalized.append({"op": op_name, "start": start, "end": end})
            else:
                raise ToolError(f"unsupported patch op: {op_name}")
        return normalized

    def _apply_operations_to_text(self, text: str, operations: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
        lines = text.splitlines()
        normalized = self._normalize_operations(lines, operations)

        def sort_key(item: Dict[str, Any]):
            return item.get("line", item.get("start", 0))

        for item in sorted(normalized, key=sort_key, reverse=True):
            if item["op"] == "replace_range":
                lines[item["start"] - 1 : item["end"]] = item["content"].splitlines()
            elif item["op"] == "insert_at":
                lines[item["line"] - 1 : item["line"] - 1] = item["content"].splitlines()
            elif item["op"] == "delete_range":
                del lines[item["start"] - 1 : item["end"]]
        return "\n".join(lines), normalized

    def open_buffer(self, path: str) -> Dict[str, Any]:
        try:
            resolved = resolve_path(self.project_root, path)
            if not resolved.exists():
                raise ToolError("file does not exist")
            if not resolved.is_file():
                raise ToolError("path is not a file")
            if not is_probably_text_file(resolved):
                raise ToolError("file is not an allowed text file")
            text, newline, had_trailing_newline = self._read_file_text(resolved)
            buffer_id = str(uuid.uuid4())[:8]
            self.buffers[buffer_id] = FileBuffer(
                buffer_id=buffer_id,
                path=resolved,
                original_text=text,
                current_text=text,
                encoding="utf-8",
                newline=newline,
                had_trailing_newline=had_trailing_newline,
            )
            self.record_action(f"open_buffer: {resolved.relative_to(self.project_root).as_posix()}")
            return success("open_buffer", buffer_id=buffer_id, path=str(resolved), newline_style="crlf" if newline == "\r\n" else "lf", had_trailing_newline=had_trailing_newline)
        except UnicodeDecodeError:
            return failure("open_buffer", "file is not valid utf-8 text", path=path)
        except ToolError as exc:
            return failure("open_buffer", str(exc), path=path)
        except Exception as exc:
            return failure("open_buffer", str(exc), path=path)

    def get_buffer(self, buffer_id: str, start_line: int = 1, max_lines: int = 200, max_chars: int = DEFAULT_READ_CHARS) -> Dict[str, Any]:
        try:
            buffer = self._get_buffer(buffer_id)
            payload = self._chunk_text(buffer.current_text, start_line, max_lines, max_chars)
            return success("get_buffer", buffer_id=buffer_id, path=str(buffer.path), **payload)
        except ToolError as exc:
            return failure("get_buffer", str(exc), buffer_id=buffer_id)

    def apply_buffer_patch(self, buffer_id: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            buffer = self._get_buffer(buffer_id)
            updated_text, normalized = self._apply_operations_to_text(buffer.current_text, operations)
            buffer.current_text = updated_text
            self.record_action(f"apply_buffer_patch: {buffer.path.relative_to(self.project_root).as_posix()}")
            return success("apply_buffer_patch", buffer_id=buffer_id, path=str(buffer.path), line_number_basis="current_buffer", changed=buffer.original_text != buffer.current_text, operations=normalized)
        except ToolError as exc:
            return failure("apply_buffer_patch", str(exc), buffer_id=buffer_id)

    def preview_buffer_diff(self, buffer_id: str) -> Dict[str, Any]:
        try:
            buffer = self._get_buffer(buffer_id)
            diff = "\n".join(
                difflib.unified_diff(
                    buffer.original_text.splitlines(),
                    buffer.current_text.splitlines(),
                    fromfile=f"a/{buffer.path.relative_to(self.project_root).as_posix()}",
                    tofile=f"b/{buffer.path.relative_to(self.project_root).as_posix()}",
                    lineterm="",
                )
            )
            return success("preview_buffer_diff", buffer_id=buffer_id, path=str(buffer.path), changed=buffer.original_text != buffer.current_text, diff=diff)
        except ToolError as exc:
            return failure("preview_buffer_diff", str(exc), buffer_id=buffer_id)

    def save_buffer(self, buffer_id: str) -> Dict[str, Any]:
        try:
            buffer = self._get_buffer(buffer_id)
            if buffer.path.name in BLOCKED_WRITE_NAMES:
                raise ToolError("saving env-style files is blocked")
            if buffer.original_text == buffer.current_text:
                return success("save_buffer", buffer_id=buffer_id, path=str(buffer.path), changed=False, content="No changes to save.")
            output = self._rebuild_output(buffer.current_text, buffer.newline, buffer.had_trailing_newline)
            with open(buffer.path, "w", encoding=buffer.encoding, newline="") as handle:
                handle.write(output)
            buffer.original_text = buffer.current_text
            self.record_action(f"save_buffer: {buffer.path.relative_to(self.project_root).as_posix()}")
            return success("save_buffer", buffer_id=buffer_id, path=str(buffer.path), changed=True, newline_style="crlf" if buffer.newline == "\r\n" else "lf")
        except ToolError as exc:
            return failure("save_buffer", str(exc), buffer_id=buffer_id)

    def discard_buffer(self, buffer_id: str) -> Dict[str, Any]:
        try:
            buffer = self._get_buffer(buffer_id)
            self.buffers.pop(buffer_id, None)
            return success("discard_buffer", buffer_id=buffer_id, path=str(buffer.path))
        except ToolError as exc:
            return failure("discard_buffer", str(exc), buffer_id=buffer_id)

    def edit_file(self, path: str, old_text: str, new_text: str) -> Dict[str, Any]:
        opened = self.open_buffer(path)
        if not opened.get("ok"):
            return failure("edit_file", opened.get("error", "failed to open buffer"), path=path)
        buffer_id = opened["buffer_id"]
        try:
            buffer = self._get_buffer(buffer_id)
            if old_text not in buffer.current_text:
                return failure("edit_file", f"Text not found in {path}", path=path)
            updated = buffer.current_text.replace(old_text, new_text, 1)
            if updated == buffer.current_text:
                return success("edit_file", path=str(buffer.path), changed=False, content=f"No changes applied to {path}")
            buffer.current_text = updated
            saved = self.save_buffer(buffer_id)
            if not saved.get("ok"):
                return failure("edit_file", saved.get("error", "save failed"), path=path)
            return success("edit_file", path=str(buffer.path), changed=saved.get("changed", False), content=f"Edited {path}")
        finally:
            self.buffers.pop(buffer_id, None)

    def apply_patch(self, path: str, operations: List[Dict[str, Any]], dry_run: bool = False) -> Dict[str, Any]:
        opened = self.open_buffer(path)
        if not opened.get("ok"):
            return failure("apply_patch", opened.get("error", "failed to open buffer"), path=path)
        buffer_id = opened["buffer_id"]
        try:
            patched = self.apply_buffer_patch(buffer_id, operations)
            if not patched.get("ok"):
                return failure("apply_patch", patched.get("error", "buffer patch failed"), path=path)
            previewed = self.preview_buffer_diff(buffer_id)
            preview = {
                "operations_applied": len(patched.get("operations", [])),
                "changed": previewed.get("changed", False),
                "diff": previewed.get("diff", ""),
            }
            if dry_run:
                return success("apply_patch", path=str(self._get_buffer(buffer_id).path), dry_run=True, line_number_basis="current_buffer", preview=preview, operations=patched.get("operations", []))
            saved = self.save_buffer(buffer_id)
            if not saved.get("ok"):
                return failure("apply_patch", saved.get("error", "save failed"), path=path)
            return success("apply_patch", path=str(self._get_buffer(buffer_id).path), dry_run=False, line_number_basis="current_buffer", preview=preview, operations=patched.get("operations", []), newline_style=saved.get("newline_style"))
        finally:
            self.buffers.pop(buffer_id, None)
