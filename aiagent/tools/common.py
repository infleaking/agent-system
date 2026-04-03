from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


TEXT_FILE_MAX_BYTES = 512_000


class ToolError(Exception):
    """Raised when a tool call is invalid or unsafe."""


def success(tool: str, **payload: Any) -> Dict[str, Any]:
    return {"ok": True, "tool": tool, **payload}


def failure(tool: str, message: str, **payload: Any) -> Dict[str, Any]:
    return {"ok": False, "tool": tool, "error": message, **payload}


def resolve_path(project_root: Path, path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ToolError(f"path escapes project root: {path}") from exc
    return resolved


def is_probably_text_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.stat().st_size > TEXT_FILE_MAX_BYTES:
        return False
    blocked_suffixes = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".pdf",
        ".zip",
        ".exe",
        ".dll",
        ".pyd",
        ".pyc",
        ".so",
        ".bin",
        ".db",
    }
    return path.suffix.lower() not in blocked_suffixes
