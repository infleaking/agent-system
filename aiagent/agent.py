#!/usr/bin/env python3
"""
Minimal aiagent scaffold with structured local tools for LLM-style tool calling.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .tools import ToolRegistry
from .tools.background_tools import BackgroundManager, build_background_tool_schemas
from .tools.skill_tools import SkillLoader, build_skill_tool_schemas
from .tools.task_tools import TaskManager, build_task_tool_schemas
from .tools.todo_tools import TodoManager, build_todo_tool_schemas

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "claude-3-5-sonnet-20241022"
TASKS_DIR = PROJECT_ROOT / ".tasks"
SKILLS_DIR = PROJECT_ROOT / "skills"
TRANSCRIPT_DIR = PROJECT_ROOT / ".transcripts"
INTERVENTION_FILE = PROJECT_ROOT / ".aiagent-intervention.json"
COMPACT_THRESHOLD = 50000
KEEP_RECENT_TOOL_RESULTS = 3

class CustomAIAgent:
    """Small agent shell with a structured tool layer."""

    def __init__(self, model_name: str = DEFAULT_MODEL, enable_task_tool: bool = True):
        self.model_name = model_name
        self.agent_name = "aiagent"
        self.enable_task_tool = enable_task_tool
        self.conversation_history: List[Dict[str, Any]] = []
        self.tool_results: List[Dict[str, Any]] = []
        self.execution_context: Dict[str, Any] = {
            "agent_name": self.agent_name,
            "environment": "powershell",
            "project_root": str(PROJECT_ROOT),
            "working_directory": str(PROJECT_ROOT),
            "recent_actions": [],
            "last_updated": self._now(),
        }
        self.todo_manager = TodoManager()
        self.skill_loader = SkillLoader(SKILLS_DIR)
        self.task_manager = TaskManager(TASKS_DIR)
        self.background_manager = BackgroundManager(PROJECT_ROOT)
        self.rounds_since_todo = 0
        self.tools = self._build_registry()
        self.tool_schemas = self.tools.tool_schemas
        self.system_prompt = self._build_system_prompt()
        self.conversation_history.append({"role": "system", "content": self.system_prompt})
        self.client = self._create_client()
        self.intervention_state: Dict[str, Any] = {
            "last_applied": None,
            "applied_count": 0,
        }

    def _create_client(self):
        load_dotenv(override=True)
        if os.getenv("ANTHROPIC_BASE_URL"):
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        if Anthropic is None:
            return None
        base_url = os.getenv("ANTHROPIC_BASE_URL")
        try:
            return Anthropic(base_url=base_url)
        except TypeError:
            return Anthropic()

    def _build_system_prompt(self) -> str:
        task_line = "- Use the task tool to delegate exploration or subtasks." if self.enable_task_tool else ""
        return f"""You are aiagent, a coding agent at {PROJECT_ROOT}.

Rules:
- Operate only inside the current repository.
- Prefer list_files/search/read before modifying code.
- Prefer apply_patch over full-file rewrites whenever practical.
- Use todo to plan multi-step tasks and keep it updated.
- Use load_skill before tackling unfamiliar topics.
- Use background_run for long-running commands.
- Keep tool calls small, explicit, and auditable.
{task_line}

Skills available:
{self.skill_loader.descriptions()}
"""

    def _build_registry(self) -> ToolRegistry:
        extra_handlers = {
            "todo": self.todo_manager.update,
            "load_skill": self.skill_loader.load_skill,
            "task_create": self.task_manager.create,
            "task_update": self.task_manager.update,
            "task_list": self.task_manager.list_all,
            "task_get": self.task_manager.get,
            "background_run": self.background_manager.run,
            "check_background": self.background_manager.check,
            "compact": self.compact_tool,
        }
        extra_schemas = (
            build_todo_tool_schemas()
            + build_skill_tool_schemas()
            + build_task_tool_schemas()
            + build_background_tool_schemas()
            + [self._compact_tool_schema()]
        )
        if self.enable_task_tool:
            extra_handlers["task"] = self.run_subagent_tool
            extra_schemas.append(self._subagent_tool_schema())
        extra_notes = [
            "Use todo for multi-step work; only one item should be in_progress.",
            "Use load_skill to fetch full skill instructions on demand.",
            "Use task_create/task_update/task_get/task_list for persistent tasks in .tasks.",
            "Use background_run/check_background for long-running commands.",
            "Use compact when you want the conversation summarized and reset.",
        ]
        if self.enable_task_tool:
            extra_notes.append("Use task to spawn a subagent with fresh context; only its summary returns.")
        return ToolRegistry(
            PROJECT_ROOT,
            self._record_action,
            extra_handlers=extra_handlers,
            extra_schemas=extra_schemas,
            extra_notes=extra_notes,
        )

    def _compact_tool_schema(self) -> Dict[str, Any]:
        return {
            "name": "compact",
            "description": "Trigger manual conversation compression.",
            "input_schema": {
                "type": "object",
                "properties": {"focus": {"type": "string"}},
            },
        }

    def _subagent_tool_schema(self) -> Dict[str, Any]:
        return {
            "name": "task",
            "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["prompt"],
            },
        }

    def _now(self) -> str:
        return datetime.now().isoformat()

    def _record_action(self, action: str) -> None:
        self.execution_context["last_updated"] = self._now()
        recent = self.execution_context["recent_actions"]
        recent.append(action)
        self.execution_context["recent_actions"] = recent[-12:]

    def _store_tool_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        self.tool_results.append(result)
        return result

    def call_tool(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        result = self.tools.call_tool(tool_name, **kwargs)
        return self._store_tool_result(result)

    def _tool_result_to_content(self, result: Dict[str, Any]) -> str:
        return json.dumps(result, ensure_ascii=False, indent=2)

    def _assistant_text(self, content_blocks: Any) -> str:
        parts: List[str] = []
        for block in content_blocks:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()

    def _log_tool_call(self, tool_name: str, tool_input: Dict[str, Any]) -> None:
        try:
            rendered = json.dumps(tool_input, ensure_ascii=False)
        except Exception:
            rendered = str(tool_input)
        print(f"\033[33m> {tool_name}: {rendered[:300]}\033[0m")

    def _log_intervention(self, request: Dict[str, Any]) -> None:
        source = request.get("source", "unknown")
        action = request.get("action", "unknown")
        reason = request.get("reason", "")
        print(f"\033[35m[intervention] source={source} action={action} reason={reason}\033[0m")

    def _require_client(self):
        if self.client is None:
            raise RuntimeError("anthropic package is not installed")
        if not self.model_name:
            raise RuntimeError("model_name is empty")

    def submit_intervention(self, request: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "source": request.get("source", "human"),
            "action": request.get("action", "pause_and_inject"),
            "prompt": request.get("prompt", ""),
            "reason": request.get("reason", ""),
            "metadata": request.get("metadata", {}),
            "created_at": self._now(),
        }
        INTERVENTION_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "ok": True,
            "intervention_file": str(INTERVENTION_FILE),
            "request": payload,
        }

    def request_human_pause(self, prompt: str, reason: str = "terminal command") -> Dict[str, Any]:
        return self.submit_intervention({
            "source": "human",
            "action": "pause_and_inject",
            "prompt": prompt,
            "reason": reason,
        })

    def request_supervisor_intervention(
        self,
        action: str,
        prompt: str = "",
        reason: str = "supervisor command",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return self.submit_intervention({
            "source": "supervisor",
            "action": action,
            "prompt": prompt,
            "reason": reason,
            "metadata": metadata or {},
        })

    def poll_intervention(self) -> Dict[str, Any] | None:
        if not INTERVENTION_FILE.exists():
            return None
        try:
            request = json.loads(INTERVENTION_FILE.read_text(encoding="utf-8"))
        finally:
            INTERVENTION_FILE.unlink(missing_ok=True)
        return request

    def handle_intervention(self, request: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Any] | None:
        source = request.get("source", "human")
        action = request.get("action", "pause_and_inject")
        prompt = request.get("prompt", "").strip()
        reason = request.get("reason", "")
        tag = "human_intervention" if source == "human" else "supervisor_intervention"
        self._log_intervention(request)
        self.intervention_state["last_applied"] = {
            "source": source,
            "action": action,
            "reason": reason,
            "at": self._now(),
        }
        self.intervention_state["applied_count"] += 1

        if action == "pause_and_inject":
            body = prompt or "Pause current workflow and reassess before continuing."
            messages.append({"role": "user", "content": f"<{tag} reason=\"{reason}\">\n{body}\n</{tag}>"})
            messages.append({"role": "assistant", "content": "Intervention received. Reassessing approach."})
            return None
        if action == "pause_only":
            return {
                "ok": True,
                "status": "paused",
                "source": source,
                "action": action,
                "reason": reason,
                "assistant_text": "",
                "message_count": len(messages),
            }
        if action == "stop":
            return {
                "ok": True,
                "status": "stopped",
                "source": source,
                "action": action,
                "reason": reason,
                "assistant_text": prompt,
                "message_count": len(messages),
            }
        messages.append({"role": "user", "content": f"<{tag} reason=\"{reason}\">\nUnknown action '{action}'. Stop and reassess.\n</{tag}>"})
        return None

    def estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        return len(str(messages)) // 4

    def micro_compact(self, messages: List[Dict[str, Any]]) -> None:
        tool_results = []
        for msg_idx, msg in enumerate(messages):
            if msg["role"] == "user" and isinstance(msg.get("content"), list):
                for part_idx, part in enumerate(msg["content"]):
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        tool_results.append((msg_idx, part_idx, part))
        if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
            return
        tool_name_map = {}
        for msg in messages:
            if msg["role"] != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if getattr(block, "type", None) == "tool_use":
                    tool_name_map[block.id] = block.name
        for _, _, result in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
            if isinstance(result.get("content"), str) and len(result["content"]) > 100:
                tool_id = result.get("tool_use_id", "")
                tool_name = tool_name_map.get(tool_id, "unknown")
                result["content"] = f"[Previous: used {tool_name}]"

    def auto_compact(self, messages: List[Dict[str, Any]], focus: str = "") -> List[Dict[str, Any]]:
        TRANSCRIPT_DIR.mkdir(exist_ok=True)
        transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
        with open(transcript_path, "w", encoding="utf-8") as handle:
            for msg in messages:
                handle.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

        self._require_client()
        conversation_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
        prompt = (
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details."
        )
        if focus:
            prompt += f" Focus especially on: {focus}."
        response = self.client.messages.create(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt + "\n\n" + conversation_text}],
            max_tokens=2000,
        )
        summary = self._assistant_text(response.content)
        return [
            {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
            {"role": "assistant", "content": "Understood. I have the context from the summary. Continuing."},
        ]

    def compact_tool(self, focus: str = "") -> Dict[str, Any]:
        return {
            "ok": True,
            "tool": "compact",
            "content": "Manual compression requested.",
            "focus": focus,
        }

    def run_subagent_tool(self, prompt: str, description: str = "") -> Dict[str, Any]:
        try:
            subagent = CustomAIAgent(model_name=self.model_name, enable_task_tool=False)
            result = subagent.run_prompt(prompt)
            summary = result.get("assistant_text", "") or result.get("message", "") or "(no summary)"
            return {
                "ok": True,
                "tool": "task",
                "description": description,
                "content": summary,
                "subagent_result": result,
            }
        except Exception as exc:
            return {"ok": False, "tool": "task", "error": str(exc), "description": description}

    def agent_loop(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        s02-style agent loop:
        the loop stays the same, only tools and dispatch expand.
        """
        self._require_client()

        while True:
            request = self.poll_intervention()
            if request:
                intervention_result = self.handle_intervention(request, messages)
                if intervention_result is not None:
                    return intervention_result

            self.micro_compact(messages)
            if self.estimate_tokens(messages) > COMPACT_THRESHOLD:
                messages[:] = self.auto_compact(messages)

            notifications = self.background_manager.drain_notifications()
            if notifications and messages:
                notif_text = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifications)
                messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
                messages.append({"role": "assistant", "content": "Noted background results."})

            response = self.client.messages.create(
                model=self.model_name,
                system=self.system_prompt,
                messages=messages,
                tools=self.tool_schemas,
                max_tokens=8000,
            )
            messages.append({"role": "assistant", "content": response.content})
            self._record_action(f"assistant stop_reason: {response.stop_reason}")

            if response.stop_reason != "tool_use":
                return {
                    "ok": True,
                    "stop_reason": response.stop_reason,
                    "assistant_text": self._assistant_text(response.content),
                    "message_count": len(messages),
                }

            results = []
            used_todo = False
            manual_compact = False
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                self._log_tool_call(block.name, block.input)
                tool_result = self.call_tool(block.name, **block.input)
                if block.name == "todo":
                    used_todo = True
                if block.name == "compact":
                    manual_compact = True
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": self._tool_result_to_content(tool_result),
                    }
                )
            self.rounds_since_todo = 0 if used_todo else self.rounds_since_todo + 1
            if self.rounds_since_todo >= 3:
                results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})
            messages.append({"role": "user", "content": results})
            if manual_compact:
                focus = ""
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use" and block.name == "compact":
                        focus = block.input.get("focus", "")
                        break
                messages[:] = self.auto_compact(messages, focus=focus)

    def process_request(self, prompt: str) -> Dict[str, Any]:
        self.conversation_history.append({"role": "user", "content": prompt})
        self._record_action(f"prompt: {prompt}")
        return {
            "ok": True,
            "message": "Scaffold ready. Tool layer and s01-s08-style loop features are available.",
            "suggested_first_tools": ["describe_tools", "todo", "list_files", "search"],
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "model": self.model_name,
            "agent_name": self.agent_name,
            "execution_context": self.execution_context,
            "tool_count": len(self.tool_schemas),
            "history_items": len(self.conversation_history),
            "tool_results_count": len(self.tool_results),
            "client_ready": self.client is not None,
            "intervention_file": str(INTERVENTION_FILE),
            "intervention_state": self.intervention_state,
        }

    def run_once(self, prompt: str) -> Dict[str, Any]:
        result = self.process_request(prompt)
        return {
            "agent_name": self.agent_name,
            "prompt": prompt,
            "result": result,
            "status": self.get_status(),
        }

    def run_tool_json(self, payload: str) -> str:
        """
        Convenience entry for a future model loop that emits JSON tool calls.
        Expected shape:
        {"tool": "read", "arguments": {"path": "README.md"}}
        """
        try:
            decoded = json.loads(payload)
            tool = decoded["tool"]
            arguments = decoded.get("arguments", {})
            result = self.call_tool(tool, **arguments)
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2)

    def run_prompt(self, prompt: str) -> Dict[str, Any]:
        """
        Run one full prompt through the model + tool loop.
        """
        messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
        self._record_action(f"run_prompt: {prompt}")
        result = self.agent_loop(messages)
        self.conversation_history.extend(messages)
        return result

    def interactive_mode(self) -> None:
        """
        Simple REPL for the s02-style aiagent loop.
        """
        history: List[Dict[str, Any]] = []
        while True:
            try:
                query = input("\033[36maiagent >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "quit", "exit", ""):
                break

            history.append({"role": "user", "content": query})
            result = self.agent_loop(history)
            text = result.get("assistant_text", "")
            if text:
                print(text)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
