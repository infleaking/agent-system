from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .buffer_tools import BufferTools, build_buffer_tool_schemas
from .common import failure, success
from .file_tools import FileTools, build_file_tool_schemas
from .shell_tools import ShellTools, build_shell_tool_schemas


class ToolRegistry:
    def __init__(
        self,
        project_root: Path,
        record_action: Callable[[str], None],
        extra_handlers: Optional[Dict[str, Callable[..., Dict[str, Any]]]] = None,
        extra_schemas: Optional[List[Dict[str, Any]]] = None,
        extra_notes: Optional[List[str]] = None,
    ):
        self.project_root = project_root
        self.record_action = record_action
        self.file_tools = FileTools(project_root, record_action)
        self.buffer_tools = BufferTools(project_root, record_action)
        self.shell_tools = ShellTools(project_root, record_action)

        self._handlers = {
            "bash": self.shell_tools.bash,
            "read_file": self.file_tools.read_file,
            "write_file": self.file_tools.write_file,
            "edit_file": self.buffer_tools.edit_file,
            "list_files": self.file_tools.list_files,
            "search": self.file_tools.search,
            "read": self.file_tools.read,
            "write": self.file_tools.write,
            "open_buffer": self.buffer_tools.open_buffer,
            "get_buffer": self.buffer_tools.get_buffer,
            "apply_buffer_patch": self.buffer_tools.apply_buffer_patch,
            "preview_buffer_diff": self.buffer_tools.preview_buffer_diff,
            "save_buffer": self.buffer_tools.save_buffer,
            "discard_buffer": self.buffer_tools.discard_buffer,
            "apply_patch": self.buffer_tools.apply_patch,
            "describe_tools": self.describe_tools,
        }
        if extra_handlers:
            self._handlers.update(extra_handlers)
        self.extra_notes = extra_notes or []
        self.tool_schemas = (
            build_shell_tool_schemas()
            + build_file_tool_schemas()
            + build_buffer_tool_schemas()
            + (extra_schemas or [])
            + [self._describe_tools_schema()]
        )

    def _describe_tools_schema(self) -> Dict[str, Any]:
        return {
            "name": "describe_tools",
            "description": "Return tool contracts and usage notes for planning.",
            "input_schema": {"type": "object", "properties": {}},
        }

    def describe_tools(self) -> Dict[str, Any]:
        return success(
            "describe_tools",
            tools=self.tool_schemas,
            notes=[
                "Use list_files and search before editing unknown areas.",
                "Use read in chunks for large files.",
                "For code edits, prefer open_buffer -> apply_buffer_patch -> preview_buffer_diff -> save_buffer.",
                "Use apply_patch as a compatibility wrapper when you want one-shot structured edits.",
                "Use edit_file only for small exact replacements.",
                "save_buffer preserves the original file newline style and avoids no-op rewrites.",
                "Use write or write_file for new files or intentional full replacement only.",
                "Use bash only for safe repository-local inspection or test commands.",
            ]
            + self.extra_notes,
        )

    def call_tool(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return failure("tool_dispatch", f"unknown tool: {tool_name}", requested_tool=tool_name)
        try:
            return handler(**kwargs)
        except TypeError as exc:
            return failure(tool_name, f"invalid parameters: {exc}")
