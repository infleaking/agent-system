from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from .common import ToolError, failure, success


DEFAULT_BASH_TIMEOUT = 15
MAX_BASH_TIMEOUT = 30


def build_shell_tool_schemas() -> List[Dict[str, Any]]:
    return [
        {
            "name": "bash",
            "description": "Run a shell command inside the repository with safety filters.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_BASH_TIMEOUT},
                },
                "required": ["command"],
            },
        }
    ]


class ShellTools:
    def __init__(self, project_root: Path, record_action):
        self.project_root = project_root
        self.record_action = record_action

    def _check_bash_command(self, command: str) -> None:
        lowered = command.lower()
        blocked_tokens = [
            "del ",
            "rmdir ",
            "remove-item",
            "format ",
            "shutdown",
            "reboot",
            "restart-computer",
            "stop-computer",
            "invoke-webrequest",
            "curl ",
            "wget ",
            "scp ",
            "ssh ",
            "git push",
            "git reset",
            "git clean",
            ".env",
        ]
        blocked_chars = [";", "&&", "||", ">", ">>", "<", "|"]

        if any(token in lowered for token in blocked_tokens):
            raise ToolError("bash command contains a blocked operation")
        if any(token in command for token in blocked_chars):
            raise ToolError("bash command chaining, piping, or redirection is not allowed")

        first = shlex.split(command, posix=False)[0].lower() if command.strip() else ""
        allowed_starts = {
            "dir",
            "type",
            "findstr",
            "where",
            "git",
            "python",
            "py",
            "get-childitem",
            "get-content",
            "select-string",
        }
        if first not in allowed_starts:
            raise ToolError(f"bash command is not in the allowlist: {first or '<empty>'}")

    def bash(self, command: str, timeout_seconds: int = DEFAULT_BASH_TIMEOUT) -> Dict[str, Any]:
        try:
            if not command.strip():
                raise ToolError("command must not be empty")
            timeout = max(1, min(int(timeout_seconds), MAX_BASH_TIMEOUT))
            self._check_bash_command(command)

            result = subprocess.run(
                command,
                cwd=self.project_root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            self.record_action(f"bash: {command}")
            return success(
                "bash",
                command=command,
                exit_code=result.returncode,
                stdout=(result.stdout or "").strip(),
                stderr=(result.stderr or "").strip(),
                output=output,
            )
        except ToolError as exc:
            return failure("bash", str(exc), command=command)
        except Exception as exc:
            return failure("bash", str(exc), command=command)
