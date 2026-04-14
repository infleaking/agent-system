#!/usr/bin/env python3
"""
Minimal aiagent scaffold with structured local tools for LLM-style tool calling.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .tools import ToolRegistry
from .tools.skill_tools import SkillLoader, build_skill_tool_schemas
from .tools.task_tools import TaskManager, build_task_tool_schemas
from .tools.todo_tools import TodoManager, build_todo_tool_schemas
from .runtime import AgentRegistry, Mailbox, RuntimeStateStore, SessionStore, new_id

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


def _serve_child_agent(
    model_name: str,
    agent_id: str,
    session_id: str,
    role: str,
    parent_agent_id: str | None,
    root_agent_id: str | None,
    owner_agent_id: str | None,
    task_brief: Dict[str, Any],
) -> None:
    agent = CustomAIAgent(
        model_name=model_name,
        enable_task_tool=True,
        agent_id=agent_id,
        role=role,
        parent_agent_id=parent_agent_id,
        root_agent_id=root_agent_id,
        owner_agent_id=owner_agent_id,
        task_brief=task_brief,
        session_id=session_id,
    )
    agent.serve_forever()

class CustomAIAgent:
    """Small agent shell with a structured tool layer."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        enable_task_tool: bool = True,
        *,
        agent_id: str | None = None,
        role: str = "general",
        parent_agent_id: str | None = None,
        root_agent_id: str | None = None,
        owner_agent_id: str | None = None,
        task_brief: Dict[str, Any] | None = None,
        session_id: str | None = None,
    ):
        self.model_name = model_name
        self.session_store = SessionStore(PROJECT_ROOT, session_id=session_id)
        self.session_id = self.session_store.session_id
        self.session_root = self.session_store.session_root
        self.agent_id = agent_id or new_id("agent")
        self.agent_name = self.agent_id
        self.role = role
        self.parent_agent_id = parent_agent_id
        self.root_agent_id = root_agent_id or self.agent_id
        self.owner_agent_id = owner_agent_id or parent_agent_id or self.agent_id
        self.task_brief = task_brief or {}
        self.enable_task_tool = enable_task_tool
        self.conversation_history: List[Dict[str, Any]] = []
        self.tool_results: List[Dict[str, Any]] = []
        self.pending_tasks: Dict[str, Dict[str, Any]] = {}
        self.child_agents: Dict[str, "CustomAIAgent"] = {}
        self.child_processes: Dict[str, multiprocessing.Process] = {}
        self.waiting_since: str | None = None
        self.idle_timeout_seconds = 300
        self.last_loop_invoked_model = False
        self.parent_requesting_exit = False
        self.execution_context: Dict[str, Any] = {
            "agent_name": self.agent_name,
            "agent_id": self.agent_id,
            "role": self.role,
            "parent_agent_id": self.parent_agent_id,
            "root_agent_id": self.root_agent_id,
            "owner_agent_id": self.owner_agent_id,
            "session_id": self.session_id,
            "session_root": str(self.session_root),
            "environment": "powershell",
            "project_root": str(PROJECT_ROOT),
            "working_directory": str(PROJECT_ROOT),
            "runner_status": "idle",
            "recent_actions": [],
            "last_updated": self._now(),
        }
        self.todo_manager = TodoManager()
        self.skill_loader = SkillLoader(SKILLS_DIR)
        self.task_manager = TaskManager(TASKS_DIR)
        self.runtime_state = RuntimeStateStore(PROJECT_ROOT, self.session_root)
        self.registry = AgentRegistry(PROJECT_ROOT, self.session_root)
        self.mailbox = Mailbox(PROJECT_ROOT, self.session_root)
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
        self._write_session_manifest(status="idle", event="agent_initialized")
        self._register_self(status="idle")
        self.write_runtime_state(status="idle", event="agent_initialized")

    def __del__(self):
        try:
            self._shutdown_children()
        except Exception:
            pass

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

    def _register_self(self, status: str) -> None:
        record = {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "role": self.role,
            "status": status,
            "parent_agent_id": self.parent_agent_id,
            "root_agent_id": self.root_agent_id,
            "owner_agent_id": self.owner_agent_id,
            "children": self.registry.get(self.agent_id).get("children", []) if self.registry.get(self.agent_id) else [],
            "task_brief": self.task_brief,
            "last_updated": self._now(),
        }
        self.registry.upsert(record)
        if self.parent_agent_id:
            self.registry.add_child(self.parent_agent_id, self.agent_id)

    def _write_session_manifest(self, status: str, event: str) -> None:
        latest_state_path = self.runtime_state.path_for(self.agent_id)
        self.session_store.write_manifest(
            {
                "ok": True,
                "status": status,
                "event": event,
                "project_root": str(PROJECT_ROOT),
                "root_agent_id": self.root_agent_id,
                "active_agent_id": self.agent_id,
                "latest_runtime_state_path": str(latest_state_path),
            }
        )

    def _build_system_prompt(self) -> str:
        task_line = "- Use the task tool to delegate exploration or longer-running subtasks." if self.enable_task_tool else ""
        task_brief_block = json.dumps(self.task_brief, ensure_ascii=False, indent=2) if self.task_brief else "(none)"
        return f"""You are aiagent, a coding agent at {PROJECT_ROOT}.

Identity:
- agent_id: {self.agent_id}
- role: {self.role}
- parent_agent_id: {self.parent_agent_id or "(none)"}
- root_agent_id: {self.root_agent_id}

Task brief:
{task_brief_block}

Rules:
- Operate only inside the current repository.
- Prefer list_files/search/read before modifying code.
- Prefer apply_patch over full-file rewrites whenever practical.
- Use todo to plan multi-step tasks and keep it updated.
- Use load_skill before tackling unfamiliar topics.
- Use task for longer-running work that should be delegated.
- Recursive child agents are allowed, but keep delegation bounded and summarize context instead of forwarding full history.
- Keep tool calls small, explicit, and auditable.
- Mailbox is the primary coordination channel between agents and the UI.
- Treat mailbox messages as explicit work items with lifecycle: unread -> in_progress -> done.
- Do not assume mailbox messages are handled synchronously; replies and follow-up control arrive through mailbox.
- When you receive a user_request, your primary goal is to answer that request directly and specifically.
- Do not continue unrelated exploration when replying to a user_request unless it is strictly necessary to answer well.
- When you delegate with task, expect results through mailbox as task_progress or task_result rather than synchronous returns.
- When your budget is nearly exhausted, prioritize sending a useful progress update and recommending extra budget if needed.
- Prefer progress reports and clear handoffs over silent termination or drifting into unrelated work.
{task_line}

Skills available:
{self.skill_loader.descriptions()}
"""

    def _build_registry(self) -> ToolRegistry:
        extra_handlers = {
            "todo": self.todo_manager.update,
            "todo_update": self.todo_manager.update_alias,
            "load_skill": self.skill_loader.load_skill,
            "task_create": self.task_manager.create,
            "task_update": self.task_manager.update,
            "task_list": self.task_manager.list_all,
            "task_get": self.task_manager.get,
            "compact": self.compact_tool,
        }
        extra_schemas = (
            build_todo_tool_schemas()
            + build_skill_tool_schemas()
            + build_task_tool_schemas()
            + [self._compact_tool_schema()]
        )
        if self.enable_task_tool:
            extra_handlers["task"] = self.run_subagent_tool
            extra_schemas.append(self._subagent_tool_schema())
        extra_notes = [
            "Use todo or todo_update for multi-step work; updates must be incremental and full-list rewrites are not allowed.",
            "Use load_skill to fetch full skill instructions on demand.",
            "Use task_create/task_update/task_get/task_list for persistent tasks in .tasks.",
            "Use task to delegate longer-running or parallelizable work instead of launching background commands.",
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
                    "api_call_budget": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum model calls the subagent may use before reporting back. Defaults to 10.",
                    },
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
        self.write_runtime_state(status="running", event="action_recorded")

    def write_runtime_state(self, status: str = "idle", event: str = "", messages: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        self._register_self(status=status)
        self._write_session_manifest(status=status, event=event)
        payload = {
            "ok": True,
            "agent_name": self.agent_name,
            "agent_id": self.agent_id,
            "role": self.role,
            "parent_agent_id": self.parent_agent_id,
            "root_agent_id": self.root_agent_id,
            "owner_agent_id": self.owner_agent_id,
            "session_id": self.session_id,
            "session_root": str(self.session_root),
            "model": self.model_name,
            "status": status,
            "event": event,
            "project_root": str(PROJECT_ROOT),
            "execution_context": self.execution_context,
            "history_items": len(self.conversation_history),
            "tool_results_count": len(self.tool_results),
            "client_ready": self.client is not None,
            "intervention_state": self.intervention_state,
            "pending_tasks": self.pending_tasks,
            "child_processes": {
                agent_id: {
                    "pid": process.pid,
                    "alive": process.is_alive(),
                    "exitcode": process.exitcode,
                }
                for agent_id, process in self.child_processes.items()
            },
            "waiting_since": self.waiting_since,
            "last_loop_invoked_model": self.last_loop_invoked_model,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "todo": {
                "items": self.todo_manager.items,
                "rendered": self.todo_manager.render(),
            },
            "pending_mail": self.mailbox.pending(recipient=self.agent_id),
            "message_count": len(messages or []),
        }
        return self.runtime_state.write(self.agent_id, payload)

    def _set_runner_status(self, status: str) -> None:
        self.execution_context["runner_status"] = status
        self.execution_context["last_updated"] = self._now()

    def _enter_waiting_mail(self) -> None:
        if self.waiting_since is None:
            self.waiting_since = self._now()
        self._set_runner_status("waiting_mail")
        self.write_runtime_state(status="waiting_mail", event="waiting_for_mail")

    def _leave_waiting_mail(self) -> None:
        self.waiting_since = None
        self.parent_requesting_exit = False
        self._set_runner_status("running")

    def _idle_seconds(self) -> float:
        if self.waiting_since is None:
            return 0.0
        try:
            started = datetime.fromisoformat(self.waiting_since)
        except ValueError:
            return 0.0
        return max(0.0, (datetime.now() - started).total_seconds())

    def _request_idle_exit(self) -> Dict[str, Any] | None:
        if self.parent_requesting_exit:
            return None
        self.parent_requesting_exit = True
        self._set_runner_status("waiting_parent")
        if self.parent_agent_id:
            result = self.send_message(
                recipient=self.parent_agent_id,
                kind="lifecycle_request",
                action="idle_timeout_exit_request",
                body="Idle timeout exceeded while waiting for new mailbox work.",
                reason="idle timeout",
                metadata={
                    "agent_id": self.agent_id,
                    "idle_started_at": self.waiting_since,
                    "idle_duration_seconds": self._idle_seconds(),
                    "pending_task_count": len(self.pending_tasks),
                },
            )
            self.write_runtime_state(status="waiting_parent", event="idle_timeout_exit_requested")
            return result
        self.write_runtime_state(status="terminated", event="idle_timeout_self_terminated")
        return {
            "ok": True,
            "tool": "lifecycle_request",
            "status": "terminated",
            "reason": "idle timeout with no parent agent",
        }

    def _shutdown_children(self, join_timeout_seconds: float = 0.5) -> None:
        for child_agent_id, process in list(self.child_processes.items()):
            try:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=max(0.1, float(join_timeout_seconds)))
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=max(0.1, float(join_timeout_seconds)))
            except Exception:
                pass
            self.child_processes.pop(child_agent_id, None)
            self.child_agents.pop(child_agent_id, None)

    def shutdown(self, reason: str = "agent shutdown") -> Dict[str, Any]:
        self._set_runner_status("terminating")
        self._shutdown_children()
        self.write_runtime_state(status="terminated", event="agent_shutdown")
        return {
            "ok": True,
            "status": "terminated",
            "assistant_text": "",
            "message_count": 0,
        }

    def serve_forever(self, poll_interval_seconds: float = 0.5) -> Dict[str, Any]:
        self._enter_waiting_mail()
        while True:
            request = self.poll_intervention()
            if request:
                self._leave_waiting_mail()
                result = self.handle_message(request, [])
                if request.get("id") and request.get("recipient") == self.agent_id:
                    self.mailbox.complete(request["id"], recipient=self.agent_id, claimed_by=self.agent_id)
                if result and result.get("status") in {"terminated", "cancelled", "completed", "failed", "stopped"}:
                    self.write_runtime_state(status=result.get("status", "terminated"), event="serve_forever_returned")
                    return result
                self._enter_waiting_mail()
                continue

            if self._idle_seconds() >= self.idle_timeout_seconds:
                result = self._request_idle_exit()
                if result and result.get("status") == "terminated":
                    return result
            time.sleep(max(0.1, float(poll_interval_seconds)))

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
        self.write_runtime_state(status="running", event="intervention_submitted")
        return {
            "ok": True,
            "intervention_file": str(INTERVENTION_FILE),
            "request": payload,
        }

    def send_message(
        self,
        *,
        sender: str | None = None,
        recipient: str,
        kind: str,
        action: str,
        body: str = "",
        reason: str = "",
        metadata: Dict[str, Any] | None = None,
        thread_id: str | None = None,
        reply_to: str | None = None,
    ) -> Dict[str, Any]:
        result = self.mailbox.send(
            sender=sender or self.agent_id,
            recipient=recipient,
            kind=kind,
            action=action,
            body=body,
            reason=reason,
            metadata=metadata,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        self.write_runtime_state(status="running", event="message_queued")
        return result

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
        mailbox_items = self.mailbox.list_unread(self.agent_id)
        if mailbox_items:
            chosen = mailbox_items[0]
            claimed = self.mailbox.claim(chosen["message"]["id"], recipient=self.agent_id, claimed_by=self.agent_id)
            if claimed.get("ok"):
                return claimed["message"]
        if not INTERVENTION_FILE.exists():
            return None
        try:
            request = json.loads(INTERVENTION_FILE.read_text(encoding="utf-8"))
        finally:
            INTERVENTION_FILE.unlink(missing_ok=True)
        request["kind"] = request.get("kind", "intervention")
        request["sender"] = request.get("source", "human")
        request["recipient"] = self.agent_id
        return request

    def handle_intervention(self, request: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Any] | None:
        source = request.get("source") or request.get("sender", "human")
        action = request.get("action", "pause_and_inject")
        prompt = request.get("prompt") or request.get("body", "")
        prompt = prompt.strip()
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
        self.write_runtime_state(status="running", event="intervention_applied", messages=messages)

        if action == "pause_and_inject":
            body = prompt or "Pause current workflow and reassess before continuing."
            messages.append({"role": "user", "content": f"<{tag} reason=\"{reason}\">\n{body}\n</{tag}>"})
            messages.append({"role": "assistant", "content": "Intervention received. Reassessing approach."})
            return None
        if action == "pause_only":
            self.write_runtime_state(status="paused", event="intervention_pause_only", messages=messages)
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
            self.write_runtime_state(status="stopped", event="intervention_stop", messages=messages)
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

    def create_task_brief(self, prompt: str, description: str = "") -> Dict[str, Any]:
        summary = self.conversation_history[-4:] if self.conversation_history else []
        return {
            "prompt": prompt,
            "description": description,
            "creator_agent_id": self.agent_id,
            "creator_role": self.role,
            "session_id": self.session_id,
            "constraints": [
                "Operate only inside the current repository.",
                "Prefer summary and artifact references over full history forwarding.",
            ],
            "recent_summary": json.dumps(summary, ensure_ascii=False, default=str)[:4000],
        }

    def _build_task_summary(self, task_brief: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        summary = result.get("assistant_text", "") or result.get("message", "") or "(no summary)"
        budget_exhausted = bool(result.get("budget_exhausted"))
        status = result.get("status", "completed")
        continue_recommended = budget_exhausted
        recommended_next_action = "continue" if budget_exhausted else "complete"
        recommended_additional_api_budget = 1 if budget_exhausted else 0
        return {
            "ok": bool(result.get("ok", True)),
            "agent_id": self.agent_id,
            "role": self.role,
            "task_brief": task_brief,
            "result": result,
            "summary": summary,
            "status": status,
            "continue_recommended": continue_recommended,
            "recommended_next_action": recommended_next_action,
            "recommended_additional_api_budget": recommended_additional_api_budget,
            "budget_exhausted": budget_exhausted,
            "api_calls_used": result.get("api_calls_used", 0),
        }

    def _budget_system_prompt(self, max_api_calls: int | None, api_calls_used: int) -> str:
        if max_api_calls is None:
            return self.system_prompt
        total = max(1, int(max_api_calls))
        remaining = max(0, total - int(api_calls_used))
        lines = [
            "API budget:",
            f"- total_api_calls: {total}",
            f"- remaining_api_calls: {remaining}",
        ]
        if remaining <= 2:
            lines.extend([
                "- Budget is nearly exhausted.",
                "- Stop expanding scope and avoid starting new exploration unless it is strictly necessary.",
                "- Your priority is to prepare a progress report for the parent agent before budget is exhausted.",
                "- Summarize completed work, key findings, and remaining work.",
                "- If the task is incomplete, explicitly recommend requesting additional API budget before continuing.",
                "- Prefer a useful progress update over consuming the final budget on low-priority tool calls.",
            ])
        if remaining <= 1:
            lines.extend([
                "- This is likely your final call before reporting.",
                "- Use this turn to produce a progress-oriented response rather than broad task execution.",
                "- If more work is needed, request additional budget and explain why.",
            ])
        return f"{self.system_prompt}\n\n" + "\n".join(lines)

    def _build_user_reply_prompt(self, prompt: str, message: Dict[str, Any]) -> str:
        return (
            "You are replying to a mailbox user request.\n"
            "Your primary goal is to answer the user's latest question directly and specifically.\n"
            "Do not continue unrelated repository exploration or previously pending work unless it is strictly necessary to answer.\n"
            "If tools are unnecessary, reply immediately in plain language.\n"
            "If tools are necessary, use only the minimum needed and then answer the question.\n"
            f"Request id: {message.get('id', '')}\n"
            f"Thread id: {message.get('thread_id', '')}\n\n"
            f"User request:\n{prompt}"
        )

    def handle_summary(self, message: Dict[str, Any]) -> Dict[str, Any]:
        request_id = message.get("reply_to") or message.get("metadata", {}).get("request_id", "")
        payload = message.get("metadata", {}).get("task_result", {})
        pending = self.pending_tasks.get(request_id)
        if pending is None:
            return {
                "ok": False,
                "status": "failed",
                "assistant_text": f"Unknown task summary reply: {request_id or '(missing)'}",
                "message_count": 0,
            }

        pending["status"] = payload.get("status", "completed")
        pending["last_summary"] = payload.get("summary", message.get("body", ""))
        pending["continue_recommended"] = payload.get("continue_recommended", False)
        pending["recommended_next_action"] = payload.get("recommended_next_action", "complete")
        pending["recommended_additional_api_budget"] = payload.get("recommended_additional_api_budget", 0)
        pending["updated_at"] = self._now()

        if message.get("kind") == "task_result":
            self.pending_tasks.pop(request_id, None)
        else:
            self.pending_tasks[request_id] = pending

        self.write_runtime_state(status="running", event="task_summary_received")
        return {
            "ok": True,
            "status": "running",
            "assistant_text": pending["last_summary"],
            "message_count": 0,
            "task_result": payload,
            "request_id": request_id,
            "continue_recommended": pending["continue_recommended"],
            "recommended_next_action": pending["recommended_next_action"],
            "recommended_additional_api_budget": pending["recommended_additional_api_budget"],
        }

    def handle_message(self, message: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Any] | None:
        kind = message.get("kind", "intervention")
        if kind == "intervention":
            normalized = {
                "source": message.get("sender", "human"),
                "action": message.get("action", "pause_and_inject"),
                "prompt": message.get("body", ""),
                "reason": message.get("reason", ""),
                "metadata": message.get("metadata", {}),
            }
            return self.handle_intervention(normalized, messages)
        if kind == "user_request":
            prompt = (message.get("body", "") or message.get("metadata", {}).get("prompt", "")).strip()
            if not prompt:
                result = {
                    "ok": False,
                    "status": "failed",
                    "assistant_text": "",
                    "error": "user_request requires a prompt body",
                    "message_count": len(messages),
                }
            else:
                try:
                    result = self.run_prompt(self._build_user_reply_prompt(prompt, message))
                except Exception as exc:
                    result = {
                        "ok": False,
                        "status": "failed",
                        "assistant_text": "",
                        "error": str(exc),
                        "message_count": len(messages),
                    }
            reply_text = result.get("assistant_text", "") or result.get("error", "") or json.dumps(result, ensure_ascii=False)
            self.send_message(
                recipient=message.get("sender", ""),
                kind="agent_reply",
                action="reply",
                body=reply_text,
                metadata={
                    "source_agent_id": self.agent_id,
                    "result": result,
                    "request_id": message.get("id"),
                },
                thread_id=message.get("thread_id"),
                reply_to=message.get("id"),
            )
            return result
        if kind == "task_request":
            task_brief = message.get("metadata", {}).get("task_brief", {})
            self.task_brief = task_brief
            prompt = task_brief.get("prompt", "").strip()
            try:
                if not prompt:
                    result = {"ok": False, "status": "failed", "error": "task_brief.prompt is required"}
                else:
                    max_api_calls = max(1, int(task_brief.get("api_call_budget", 10)))
                    result = self.run_prompt(prompt, max_api_calls=max_api_calls)
            except Exception as exc:
                result = {
                    "ok": False,
                    "status": "failed",
                    "error": str(exc),
                    "assistant_text": f"Subagent execution failed: {exc}",
                }
            summary = self._build_task_summary(task_brief, result)
            result_kind = "task_progress" if summary.get("continue_recommended") else "task_result"
            self.send_message(
                recipient=message.get("sender", ""),
                kind=result_kind,
                action="completed" if summary.get("ok") else "failed",
                body=summary.get("summary", result.get("error", "")),
                metadata={
                    "task_brief": task_brief,
                    "task_result": summary,
                    "source_agent_id": self.agent_id,
                    "request_id": message.get("id"),
                },
                thread_id=message.get("thread_id"),
                reply_to=message.get("id"),
            )
            return {
                "ok": True,
                "status": summary.get("status", "completed"),
                "assistant_text": summary.get("summary", ""),
                "message_count": len(messages),
                "task_result": summary,
            }
        if kind == "task_control":
            action = message.get("action", "continue")
            if action != "continue":
                result = {
                    "ok": True,
                    "status": "terminated" if action == "terminate" else "cancelled" if action == "cancel" else "paused",
                    "assistant_text": f"Task {action} by parent control.",
                    "api_calls_used": 0,
                    "budget_exhausted": False,
                }
                summary = self._build_task_summary(self.task_brief, result)
                summary["continue_recommended"] = False
                summary["recommended_next_action"] = "complete"
                self.send_message(
                    recipient=message.get("sender", ""),
                    kind="task_result",
                    action="completed",
                    body=summary["summary"],
                    metadata={
                        "task_brief": self.task_brief,
                        "task_result": summary,
                        "source_agent_id": self.agent_id,
                        "request_id": message.get("reply_to") or message.get("id"),
                    },
                    thread_id=message.get("thread_id"),
                    reply_to=message.get("id"),
                )
                return {
                    "ok": True,
                    "status": summary["status"],
                    "assistant_text": summary["summary"],
                    "message_count": len(messages),
                    "task_result": summary,
                }

            control_meta = message.get("metadata", {})
            updated_brief = dict(self.task_brief)
            updated_brief["api_call_budget"] = max(1, int(control_meta.get("api_call_budget", updated_brief.get("api_call_budget", 10))))
            if control_meta.get("prompt"):
                updated_brief["prompt"] = str(control_meta["prompt"])
            if control_meta.get("description"):
                updated_brief["description"] = str(control_meta["description"])
            self.task_brief = updated_brief
            prompt = updated_brief.get("prompt", "").strip()
            try:
                result = self.run_prompt(prompt, max_api_calls=max(1, int(updated_brief.get("api_call_budget", 10))))
            except Exception as exc:
                result = {
                    "ok": False,
                    "status": "failed",
                    "error": str(exc),
                    "assistant_text": f"Subagent execution failed: {exc}",
                }
            summary = self._build_task_summary(updated_brief, result)
            result_kind = "task_progress" if summary.get("continue_recommended") else "task_result"
            self.send_message(
                recipient=message.get("sender", ""),
                kind=result_kind,
                action="completed" if summary.get("ok") else "failed",
                body=summary["summary"],
                metadata={
                    "task_brief": updated_brief,
                    "task_result": summary,
                    "source_agent_id": self.agent_id,
                    "request_id": message.get("reply_to") or message.get("id"),
                },
                thread_id=message.get("thread_id"),
                reply_to=message.get("id"),
            )
            return {
                "ok": True,
                "status": summary["status"],
                "assistant_text": summary["summary"],
                "message_count": len(messages),
                "task_result": summary,
            }
        if kind in {"task_progress", "task_result"}:
            return self.handle_summary(message)
        if kind == "lifecycle_request":
            action = message.get("action", "")
            if action == "idle_timeout_exit_request":
                return {
                    "ok": True,
                    "status": "running",
                    "assistant_text": message.get("body", ""),
                    "message_count": len(messages),
                    "lifecycle_request": message,
                }
            if action == "terminate_agent":
                return self.shutdown(reason=message.get("body", "") or message.get("reason", "") or "parent requested shutdown")
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
        self.write_runtime_state(status="running", event="auto_compacted", messages=messages)
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

    def create_child_agent(self, task_brief: Dict[str, Any], role: str = "delegate") -> "CustomAIAgent":
        return CustomAIAgent(
            model_name=self.model_name,
            enable_task_tool=True,
            agent_id=None,
            role=role,
            parent_agent_id=self.agent_id,
            root_agent_id=self.root_agent_id,
            owner_agent_id=self.agent_id,
            task_brief=task_brief,
            session_id=self.session_id,
        )

    def _spawn_child_process(self, subagent: "CustomAIAgent") -> multiprocessing.Process:
        process = multiprocessing.Process(
            target=_serve_child_agent,
            args=(
                self.model_name,
                subagent.agent_id,
                self.session_id,
                subagent.role,
                self.agent_id,
                self.root_agent_id,
                self.agent_id,
                subagent.task_brief,
            ),
            daemon=False,
        )
        process.start()
        self.child_processes[subagent.agent_id] = process
        return process

    def run_subagent_tool(self, prompt: str, description: str = "", api_call_budget: int = 10) -> Dict[str, Any]:
        try:
            task_brief = self.create_task_brief(prompt=prompt, description=description)
            task_brief["api_call_budget"] = max(1, int(api_call_budget))
            subagent = self.create_child_agent(task_brief=task_brief)
            self.child_agents[subagent.agent_id] = subagent
            envelope = self.send_message(
                recipient=subagent.agent_id,
                kind="task_request",
                action="execute_task_brief",
                body=description or prompt[:200],
                metadata={"task_brief": task_brief},
            )
            process = self._spawn_child_process(subagent)
            request_id = envelope["message"]["id"]
            self.pending_tasks[request_id] = {
                "request_id": request_id,
                "child_agent_id": subagent.agent_id,
                "status": "dispatched",
                "spawn_status": "started",
                "process_id": process.pid,
                "description": description,
                "task_brief": task_brief,
                "created_at": self._now(),
                "updated_at": self._now(),
                "last_summary": "",
                "continue_recommended": False,
                "recommended_next_action": "wait",
                "recommended_additional_api_budget": 0,
            }
            self.write_runtime_state(status="running", event="task_dispatched")
            return {
                "ok": True,
                "tool": "task",
                "description": description,
                "agent_id": subagent.agent_id,
                "request_id": request_id,
                "thread_id": envelope["message"]["thread_id"],
                "task_brief": task_brief,
                "status": "dispatched",
                "spawn_status": "started",
                "process_id": process.pid,
                "content": "Subagent process started and will report back via mailbox.",
            }
        except Exception as exc:
            return {"ok": False, "tool": "task", "error": str(exc), "description": description}

    def agent_loop(self, messages: List[Dict[str, Any]], max_api_calls: int | None = None) -> Dict[str, Any]:
        """
        s02-style agent loop:
        the loop stays the same, only tools and dispatch expand.
        """
        self._require_client()

        api_calls_used = 0
        while True:
            self.write_runtime_state(status="running", event="loop_tick", messages=messages)
            request = self.poll_intervention()
            if request:
                message_result = self.handle_message(request, messages)
                if request.get("id") and request.get("recipient") == self.agent_id:
                    self.mailbox.complete(request["id"], recipient=self.agent_id, claimed_by=self.agent_id)
                if message_result is not None:
                    if request.get("kind") == "task_request":
                        self.write_runtime_state(status="idle", event="task_request_completed", messages=messages)
                    else:
                        self.write_runtime_state(status=message_result.get("status", "running"), event="loop_returning", messages=messages)
                    return message_result

            self.micro_compact(messages)
            
            if self.estimate_tokens(messages) > COMPACT_THRESHOLD:
                messages[:] = self.auto_compact(messages)

            if max_api_calls is not None and api_calls_used >= max_api_calls:
                self.last_loop_invoked_model = api_calls_used > 0
                self.write_runtime_state(status="waiting_parent", event="api_budget_exhausted", messages=messages)
                return {
                    "ok": True,
                    "status": "waiting_parent",
                    "assistant_text": "",
                    "message_count": len(messages),
                    "budget_exhausted": True,
                    "api_calls_used": api_calls_used,
                    "invoked_model": api_calls_used > 0,
                    "has_pending_local_work": False,
                    "should_wait_mail": True,
                }

            response = self.client.messages.create(
                model=self.model_name,
                system=self._budget_system_prompt(max_api_calls, api_calls_used),
                messages=messages,
                tools=self.tool_schemas,
                max_tokens=8000,
            )
            api_calls_used += 1
            self.last_loop_invoked_model = True
            messages.append({"role": "assistant", "content": response.content})
            self._record_action(f"assistant stop_reason: {response.stop_reason}")
            self.write_runtime_state(status="running", event="assistant_responded", messages=messages)

            if response.stop_reason != "tool_use":
                self.write_runtime_state(status="idle", event="assistant_completed", messages=messages)
                return {
                    "ok": True,
                    "stop_reason": response.stop_reason,
                    "assistant_text": self._assistant_text(response.content),
                    "message_count": len(messages),
                    "budget_exhausted": False,
                    "api_calls_used": api_calls_used,
                    "invoked_model": True,
                    "has_pending_local_work": False,
                    "should_wait_mail": False,
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
                results.append({"type": "text", "text": "<reminder>Update your todos incrementally. Do not rewrite the full list.</reminder>"})
            messages.append({"role": "user", "content": results})
            self.write_runtime_state(status="running", event="tool_results_appended", messages=messages)
            if manual_compact:
                focus = ""
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use" and block.name == "compact":
                        focus = block.input.get("focus", "")
                        break
                messages[:] = self.auto_compact(messages, focus=focus)
            self.last_loop_invoked_model = True

    def process_request(self, prompt: str) -> Dict[str, Any]:
        self.conversation_history.append({"role": "user", "content": prompt})
        self._record_action(f"prompt: {prompt}")
        self.write_runtime_state(status="running", event="request_processed")
        return {
            "ok": True,
            "message": "Scaffold ready. Tool layer and s01-s08-style loop features are available.",
            "suggested_first_tools": ["describe_tools", "todo", "list_files", "search"],
        }

    def get_status(self) -> Dict[str, Any]:
        record = self.registry.get(self.agent_id) or {}
        return {
            "model": self.model_name,
            "agent_name": self.agent_name,
            "agent_id": self.agent_id,
            "role": self.role,
            "parent_agent_id": self.parent_agent_id,
            "root_agent_id": self.root_agent_id,
            "owner_agent_id": self.owner_agent_id,
            "session_id": self.session_id,
            "session_root": str(self.session_root),
            "execution_context": self.execution_context,
            "tool_count": len(self.tool_schemas),
            "history_items": len(self.conversation_history),
            "tool_results_count": len(self.tool_results),
            "client_ready": self.client is not None,
            "intervention_file": str(INTERVENTION_FILE),
            "runtime_state_file": str(self.runtime_state.path_for(self.agent_id)),
            "mailbox_dir": str(self.mailbox.root),
            "registry_file": str(self.registry.path),
            "session_manifest_file": str(self.session_store.manifest_path),
            "children": record.get("children", []),
            "pending_tasks": self.pending_tasks,
            "waiting_since": self.waiting_since,
            "last_loop_invoked_model": self.last_loop_invoked_model,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "runner_status": self.execution_context.get("runner_status", "idle"),
            "intervention_state": self.intervention_state,
            "child_processes": {
                agent_id: {
                    "pid": process.pid,
                    "alive": process.is_alive(),
                    "exitcode": process.exitcode,
                }
                for agent_id, process in self.child_processes.items()
            },
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

    def run_prompt(self, prompt: str, max_api_calls: int | None = None) -> Dict[str, Any]:
        """
        Run one full prompt through the model + tool loop.
        """
        messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
        self._record_action(f"run_prompt: {prompt}")
        result = self.agent_loop(messages, max_api_calls=max_api_calls)
        self.conversation_history.extend(messages)
        self.write_runtime_state(status=result.get("status", "idle") if isinstance(result, dict) else "idle", event="prompt_finished", messages=messages)
        return result

    def interactive_mode(self) -> None:
        """
        Simple REPL for the s02-style aiagent loop.
        """
        history: List[Dict[str, Any]] = []
        self.write_runtime_state(status="interactive_waiting", event="interactive_started", messages=history)
        while True:
            try:
                query = input("\033[36maiagent >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                break
            if query.strip().lower() in ("q", "quit", "exit", ""):
                break

            history.append({"role": "user", "content": query})
            self.write_runtime_state(status="interactive_running", event="interactive_query_received", messages=history)
            result = self.agent_loop(history)
            text = result.get("assistant_text", "")
            if text:
                print(text)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            self.write_runtime_state(status="interactive_waiting", event="interactive_query_completed", messages=history)
