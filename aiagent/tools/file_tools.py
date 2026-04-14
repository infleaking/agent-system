from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Dict, List

from .common import ToolError, failure, is_probably_text_file, resolve_path, success


MAX_READ_CHARS = 12000
DEFAULT_READ_CHARS = 4000
MAX_LIST_FILES = 500
MAX_SEARCH_RESULTS = 200
SKIP_DIR_NAMES = {".git", ".venv", "__pycache__", ".worktrees", "node_modules"}
BLOCKED_WRITE_NAMES = {".env", ".env.local", ".env.production", ".env.development"}


def build_file_tool_schemas() -> List[Dict[str, Any]]:
    return [
        {
            "name": "read_file",
            "description": "Compatibility alias for reading file contents.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Compatibility alias for writing file contents.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "edit_file",
            "description": "Compatibility alias for exact single replacement in a file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
        {
            "name": "list_files",
            "description": "List repository files. Use this before reading unknown locations.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "root": {"type": "string"},
                    "glob": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_FILES},
                },
            },
        },
        {
            "name": "search",
            "description": "Search text files and return matching lines with locations.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "root": {"type": "string"},
                    "glob": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "read",
            "description": "Read a file chunk by line range. Use this for large files.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_READ_CHARS},
                },
                "required": ["path"],
            },
        },
        {
            "name": "write",
            "description": "Create a new file or overwrite an existing file when overwrite=true.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                    "create_dirs": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "apply_patch",
            "description": "Apply structured line-based patch operations to a text file. Line numbers are interpreted against the original file state.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": ["replace_range", "insert_at", "delete_range"],
                                },
                                "start_line": {"type": "integer", "minimum": 1},
                                "end_line": {"type": "integer", "minimum": 1},
                                "line": {"type": "integer", "minimum": 1},
                                "content": {"type": "string"},
                            },
                            "required": ["op"],
                        },
                    },
                    "dry_run": {"type": "boolean"},
                },
                "required": ["path", "operations"],
            },
        },
    ]


class FileTools:
    def __init__(self, project_root: Path, record_action):
        self.project_root = project_root
        self.record_action = record_action

    def _iter_files(self, root: str = ".", glob: str = "**/*"):
        resolved_root = resolve_path(self.project_root, root)
        if not resolved_root.exists():
            raise ToolError("root does not exist")
        if not resolved_root.is_dir():
            raise ToolError("root is not a directory")

        for path in resolved_root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            rel = path.relative_to(self.project_root).as_posix()
            if fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(path.name, glob):
                yield path

    def list_files(self, root: str = ".", glob: str = "**/*", limit: int = 200) -> Dict[str, Any]:
        try:
            limit = max(1, min(int(limit), MAX_LIST_FILES))
            paths = []
            truncated = False
            for index, path in enumerate(self._iter_files(root=root, glob=glob), start=1):
                if index > limit:
                    truncated = True
                    break
                paths.append(path.relative_to(self.project_root).as_posix())
            self.record_action(f"list_files: {root} {glob}")
            return success("list_files", root=root, glob=glob, count=len(paths), truncated=truncated, paths=paths)
        except ToolError as exc:
            return failure("list_files", str(exc), root=root, glob=glob)
        except Exception as exc:
            return failure("list_files", str(exc), root=root, glob=glob)

    def search(
        self,
        pattern: str,
        root: str = ".",
        glob: str = "**/*",
        regex: bool = False,
        case_sensitive: bool = False,
        limit: int = 50,
    ) -> Dict[str, Any]:
        try:
            if not pattern:
                raise ToolError("pattern must not be empty")
            limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
            flags = 0 if case_sensitive else re.IGNORECASE
            compiled = re.compile(pattern if regex else re.escape(pattern), flags)

            matches = []
            truncated = False
            for path in self._iter_files(root=root, glob=glob):
                if not is_probably_text_file(path):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if compiled.search(line):
                        matches.append(
                            {
                                "path": path.relative_to(self.project_root).as_posix(),
                                "line": line_number,
                                "text": line[:300],
                            }
                        )
                        if len(matches) >= limit:
                            truncated = True
                            break
                if truncated:
                    break

            self.record_action(f"search: {pattern}")
            return success(
                "search",
                pattern=pattern,
                regex=regex,
                case_sensitive=case_sensitive,
                count=len(matches),
                truncated=truncated,
                matches=matches,
            )
        except ToolError as exc:
            return failure("search", str(exc), pattern=pattern)
        except Exception as exc:
            return failure("search", str(exc), pattern=pattern)

    def read(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = 200,
        max_chars: int = DEFAULT_READ_CHARS,
    ) -> Dict[str, Any]:
        try:
            resolved = resolve_path(self.project_root, path)
            if not resolved.exists():
                raise ToolError("file does not exist")
            if not resolved.is_file():
                raise ToolError("path is not a file")
            if not is_probably_text_file(resolved):
                raise ToolError("file is not an allowed text file")

            start_line = max(1, int(start_line))
            max_lines = max(1, min(int(max_lines), 1000))
            max_chars = max(1, min(int(max_chars), MAX_READ_CHARS))

            text = resolved.read_text(encoding="utf-8")
            lines = text.splitlines()
            start_index = start_line - 1
            chunk_lines = lines[start_index : start_index + max_lines]
            chunk = "\n".join(chunk_lines)

            truncated = False
            if len(chunk) > max_chars:
                chunk = chunk[:max_chars]
                truncated = True

            end_line = start_index + len(chunk_lines)
            self.record_action(f"read: {resolved.relative_to(self.project_root).as_posix()}")
            return success(
                "read",
                path=str(resolved),
                start_line=start_line,
                end_line=end_line,
                total_lines=len(lines),
                truncated=truncated,
                content=chunk,
            )
        except UnicodeDecodeError:
            return failure("read", "file is not valid utf-8 text", path=path)
        except ToolError as exc:
            return failure("read", str(exc), path=path)
        except Exception as exc:
            return failure("read", str(exc), path=path)

    def read_file(self, path: str, limit: int = None) -> Dict[str, Any]:
        max_lines = limit if limit is not None else 200
        result = self.read(path=path, start_line=1, max_lines=max_lines)
        result["tool"] = "read_file"
        return result

    def write(
        self,
        path: str,
        content: str,
        overwrite: bool = False,
        create_dirs: bool = True,
    ) -> Dict[str, Any]:
        try:
            resolved = resolve_path(self.project_root, path)
            if resolved.name in BLOCKED_WRITE_NAMES:
                raise ToolError("writing env-style files is blocked")
            existed_before = resolved.exists()
            if existed_before and not overwrite:
                raise ToolError("file already exists; pass overwrite=true to replace it")
            if create_dirs:
                resolved.parent.mkdir(parents=True, exist_ok=True)
            elif not resolved.parent.exists():
                raise ToolError("parent directory does not exist")

            normalized = content.replace("\r\n", "\n")
            with open(resolved, "w", encoding="utf-8", newline="") as handle:
                handle.write(normalized)
            self.record_action(f"write: {resolved.relative_to(self.project_root).as_posix()}")
            return success(
                "write",
                path=str(resolved),
                bytes_written=len(normalized.encode("utf-8")),
                overwritten=existed_before,
            )
        except ToolError as exc:
            return failure("write", str(exc), path=path)
        except Exception as exc:
            return failure("write", str(exc), path=path)

    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        result = self.write(path=path, content=content, overwrite=True, create_dirs=True)
        result["tool"] = "write_file"
        return result

    def edit_file(self, path: str, old_text: str, new_text: str) -> Dict[str, Any]:
        try:
            resolved = resolve_path(self.project_root, path)
            if resolved.name in BLOCKED_WRITE_NAMES:
                raise ToolError("editing env-style files is blocked")
            if not resolved.exists():
                raise ToolError("file does not exist")
            if not is_probably_text_file(resolved):
                raise ToolError("file is not an allowed text file")
            content = resolved.read_text(encoding="utf-8")
            if old_text not in content:
                raise ToolError(f"Text not found in {path}")
            resolved.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
            self.record_action(f"edit_file: {resolved.relative_to(self.project_root).as_posix()}")
            return success("edit_file", path=str(resolved), content=f"Edited {path}")
        except UnicodeDecodeError:
            return failure("edit_file", "file is not valid utf-8 text", path=path)
        except ToolError as exc:
            return failure("edit_file", str(exc), path=path)
        except Exception as exc:
            return failure("edit_file", str(exc), path=path)

    def apply_patch(self, path: str, operations: List[Dict[str, Any]], dry_run: bool = False) -> Dict[str, Any]:
        try:
            if not operations:
                raise ToolError("operations must not be empty")
            resolved = resolve_path(self.project_root, path)
            if resolved.name in BLOCKED_WRITE_NAMES:
                raise ToolError("patching env-style files is blocked")
            if not resolved.exists():
                raise ToolError("file does not exist")
            if not is_probably_text_file(resolved):
                raise ToolError("file is not an allowed text file")

            original_text = resolved.read_text(encoding="utf-8")
            original_had_trailing_newline = original_text.endswith("\n")
            lines = original_text.splitlines()
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

            def sort_key(item: Dict[str, Any]):
                anchor = item.get("line", item.get("start", 0))
                return anchor

            for item in sorted(normalized, key=sort_key, reverse=True):
                if item["op"] == "replace_range":
                    replacement_lines = item["content"].splitlines()
                    lines[item["start"] - 1 : item["end"]] = replacement_lines
                elif item["op"] == "insert_at":
                    insertion_lines = item["content"].splitlines()
                    lines[item["line"] - 1 : item["line"] - 1] = insertion_lines
                elif item["op"] == "delete_range":
                    del lines[item["start"] - 1 : item["end"]]

            updated_text = "\n".join(lines)
            if lines and original_had_trailing_newline:
                updated_text += "\n"

            preview = {
                "before_line_count": len(original_text.splitlines()),
                "after_line_count": len(updated_text.splitlines()),
                "operations_applied": len(normalized),
            }

            self.record_action(f"apply_patch: {resolved.relative_to(self.project_root).as_posix()}")
            if dry_run:
                return success(
                    "apply_patch",
                    path=str(resolved),
                    dry_run=True,
                    line_number_basis="original_file",
                    preview=preview,
                    operations=normalized,
                )

            resolved.write_text(updated_text, encoding="utf-8")
            return success(
                "apply_patch",
                path=str(resolved),
                dry_run=False,
                line_number_basis="original_file",
                preview=preview,
                operations=normalized,
            )
        except UnicodeDecodeError:
            return failure("apply_patch", "file is not valid utf-8 text", path=path)
        except ToolError as exc:
            return failure("apply_patch", str(exc), path=path)
        except Exception as exc:
            return failure("apply_patch", str(exc), path=path)
