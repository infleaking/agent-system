#!/usr/bin/env python3
"""
Minimal terminal UI for inspecting aiagent state and injecting interventions
while the main agent loop is running in a separate process.
"""

from __future__ import annotations

import json
import os
import time

from .agent import INTERVENTION_FILE, PROJECT_ROOT, TRANSCRIPT_DIR
from pathlib import Path

from .runtime import AgentRegistry, Mailbox, RuntimeStateStore, SessionStore
from .tools import ToolRegistry
from .tools.skill_tools import build_skill_tool_schemas
from .tools.task_tools import TaskManager
from .tools.task_tools import build_task_tool_schemas
from .tools.todo_tools import build_todo_tool_schemas


class AgentConsoleUI:
    def __init__(self, project_root: Path, session_id: str):
        self.project_root = project_root
        self.session_id = session_id
        self.session_store = SessionStore(project_root, session_id=session_id)
        self.task_manager = TaskManager(project_root / ".tasks")
        self.tools = ToolRegistry(project_root, lambda _action: None)
        self.tool_schemas = (
            self.tools.tool_schemas[:-1]
            + build_todo_tool_schemas()
            + build_skill_tool_schemas()
            + build_task_tool_schemas()
            + [
                {
                    "name": "compact",
                    "description": "Trigger manual conversation compression.",
                    "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}},
                },
                {
                    "name": "task",
                    "description": "Spawn a subagent with fresh context.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["prompt"],
                    },
                },
                self.tools.tool_schemas[-1],
            ]
        )
        self.runtime_state = self._latest_runtime_store()
        self.registry = self._latest_registry()
        self.mailbox = self._latest_mailbox()
        self.ui_recipient = f"ui_{os.getpid()}"

    def run(self) -> None:
        print("aiagent console")
        print("Type 'help' for commands.")
        while True:
            try:
                raw = input("\033[32mui >> \033[0m").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not raw:
                continue
            if raw.lower() in {"q", "quit", "exit"}:
                break
            if not self._handle_command(raw):
                self._send_user_prompt(raw)

    def _handle_command(self, raw: str) -> bool:
        command, _, remainder = raw.partition(" ")
        command = command.lower()

        if command == "help":
            self._print_help()
            return True
        if command == "status":
            self._print_json(self._status_payload())
            return True
        if command == "tools":
            self._print_tools()
            return True
        if command == "agents":
            self._print_json(self.registry.list_all())
            return True
        if command == "todos":
            runtime = self._status_payload()
            print(runtime.get("todo", {}).get("rendered", "No shared todo state yet."))
            return True
        if command == "tasks":
            self._print_json(self.task_manager.list_all())
            return True
        if command == "intervention":
            self._show_intervention_state()
            return True
        if command == "transcripts":
            self._list_transcripts()
            return True
        if command == "read":
            if not remainder:
                print("Usage: read <path> [start_line] [max_lines]")
                return True
            self._read_file(remainder)
            return True
        if command == "pause":
            prompt = remainder.strip() or "Pause current workflow and reassess before continuing."
            self._print_json(
                self.mailbox.send(
                    sender="human",
                    recipient=self._target_agent_id(),
                    kind="intervention",
                    action="pause_and_inject",
                    body=prompt,
                    reason="ui command",
                )
            )
            return True
        if command == "pause_only":
            reason = remainder.strip() or "ui command"
            self._print_json(
                self.mailbox.send(
                    sender="human",
                    recipient=self._target_agent_id(),
                    kind="intervention",
                    action="pause_only",
                    body="",
                    reason=reason,
                )
            )
            return True
        if command == "stop":
            reason = remainder.strip() or "ui command"
            self._print_json(
                self.mailbox.send(
                    sender="human",
                    recipient=self._target_agent_id(),
                    kind="intervention",
                    action="stop",
                    body="",
                    reason=reason,
                )
            )
            return True
        if command == "supervisor":
            self._handle_supervisor(remainder)
            return True

        return False

    def _print_help(self) -> None:
        print(
            "\n".join(
                [
                    "Commands:",
                    "  help                          Show this help",
                    "  status                        Show shared runtime status from the running agent",
                    "  tools                         List registered tool names",
                    "  agents                        Show registered agent records",
                    "  todos                         Show shared todo state from the running agent",
                    "  tasks                         Show persistent tasks from .tasks/",
                    "  intervention                  Show pending mailbox items and legacy intervention state",
                    "  transcripts                   List saved transcript files",
                    "  read <path> [start] [count]   Read a repo file via tool layer",
                    "  pause <prompt>                Queue a human pause_and_inject",
                    "  pause_only [reason]           Queue a human pause_only",
                    "  stop [reason]                 Queue a human stop",
                    "  supervisor <action> <prompt>  Queue supervisor intervention",
                    "  <any other text>              Send a user request to the root agent",
                    "  quit                          Exit the UI",
                ]
            )
        )

    def _print_json(self, payload: object) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    def _print_tools(self) -> None:
        names = [tool["name"] for tool in self.tool_schemas]
        print("\n".join(names))

    def _latest_runtime_store(self) -> RuntimeStateStore:
        return RuntimeStateStore(self.project_root, self.session_store.session_root)

    def _latest_registry(self) -> AgentRegistry:
        return AgentRegistry(self.project_root, self.session_store.session_root)

    def _latest_mailbox(self) -> Mailbox:
        return Mailbox(self.project_root, self.session_store.session_root)

    def _status_payload(self) -> dict:
        self.runtime_state = self._latest_runtime_store()
        self.registry = self._latest_registry()
        self.mailbox = self._latest_mailbox()
        runtime = self.runtime_state.read()
        if runtime.get("ok") is False:
            return {
                "message": "No running agent state found yet.",
                "sessions_root": str(self.session_store.sessions_root),
                "latest_session_index": str(self.session_store.index_path),
                "runtime_state_dir": str(self.runtime_state.root),
                "mailbox_dir": str(self.mailbox.root),
                "registry_file": str(self.registry.path),
                "legacy_intervention_file": str(INTERVENTION_FILE),
            }
        return runtime

    def _show_intervention_state(self) -> None:
        payload = {
            "mailbox": {
                "unread": [self._mail_summary(item) for item in self.mailbox.pending(state="unread")],
                "in_progress": [self._mail_summary(item) for item in self.mailbox.pending(state="in_progress")],
                "done": [self._mail_summary(item) for item in self.mailbox.pending(state="done")[-10:]],
            },
            "legacy_intervention_file_exists": INTERVENTION_FILE.exists(),
        }
        if INTERVENTION_FILE.exists():
            try:
                payload["legacy_intervention"] = json.loads(INTERVENTION_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                payload["legacy_intervention_error"] = str(exc)
        self._print_json(payload)

    def _list_transcripts(self) -> None:
        if not TRANSCRIPT_DIR.exists():
            print("No transcripts directory yet.")
            return
        files = sorted(TRANSCRIPT_DIR.glob("*.jsonl"))
        if not files:
            print("No transcript files yet.")
            return
        for path in files:
            print(path.name)

    def _read_file(self, remainder: str) -> None:
        parts = remainder.split()
        path = parts[0]
        start_line = int(parts[1]) if len(parts) >= 2 else 1
        max_lines = int(parts[2]) if len(parts) >= 3 else 40
        result = self.tools.call_tool("read", path=path, start_line=start_line, max_lines=max_lines)
        self._print_json(result)

    def _target_agent_id(self) -> str:
        runtime = self._status_payload()
        return runtime.get("agent_id", "agent_root")

    def _handle_supervisor(self, remainder: str) -> None:
        action, _, prompt = remainder.strip().partition(" ")
        action = action.strip() or "pause_and_inject"
        self._print_json(
            self.mailbox.send(
                sender="supervisor",
                recipient=self._target_agent_id(),
                kind="intervention",
                action=action,
                body=prompt.strip(),
                reason="ui command",
            )
        )

    def _mail_summary(self, payload: dict) -> dict:
        return {
            "id": payload.get("id"),
            "thread_id": payload.get("thread_id"),
            "sender": payload.get("sender"),
            "recipient": payload.get("recipient"),
            "kind": payload.get("kind"),
            "action": payload.get("action"),
            "title": payload.get("title"),
            "state": payload.get("state"),
            "created_at": payload.get("created_at"),
            "claimed_by": payload.get("claimed_by"),
            "claimed_at": payload.get("claimed_at"),
            "completed_at": payload.get("completed_at"),
        }

    def _send_user_prompt(self, prompt: str) -> None:
        envelope = self.mailbox.send(
            sender=self.ui_recipient,
            recipient=self._target_agent_id(),
            kind="user_request",
            action="prompt",
            body=prompt,
            reason="ui prompt",
            metadata={"prompt": prompt},
        )
        thread_id = envelope["message"]["thread_id"]
        reply = self._wait_for_agent_reply(thread_id)
        if reply is None:
            print("(timed out waiting for agent reply)")
            return
        print(reply.get("body", ""))

    def _wait_for_agent_reply(self, thread_id: str, timeout_seconds: float = 300.0, poll_interval_seconds: float = 0.5) -> dict | None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            items = self.mailbox.list_unread(self.ui_recipient, include_broadcast=False)
            for item in items:
                message = item["message"]
                if message.get("kind") != "agent_reply":
                    continue
                if message.get("thread_id") != thread_id:
                    continue
                claimed = self.mailbox.claim(message["id"], recipient=self.ui_recipient, claimed_by=self.ui_recipient, include_broadcast=False)
                if not claimed.get("ok"):
                    continue
                full_message = claimed["message"]
                self.mailbox.complete(full_message["id"], recipient=self.ui_recipient, claimed_by=self.ui_recipient, include_broadcast=False)
                return full_message
            time.sleep(max(0.1, float(poll_interval_seconds)))
        return None


def launch_console() -> None:
    sessions_root = PROJECT_ROOT / ".aiagent-sessions"
    index_path = sessions_root / "latest.json"
    if not index_path.exists():
        raise RuntimeError(f"latest session index not found: {index_path}")
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    manifest_path = Path(payload["manifest_path"])
    if not manifest_path.exists():
        raise RuntimeError(f"latest session manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    AgentConsoleUI(PROJECT_ROOT, manifest["session_id"]).run()
