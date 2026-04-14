from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def now_iso() -> str:
    return datetime.now().isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class SessionStore:
    def __init__(self, project_root: Path, session_id: str | None = None):
        self.project_root = project_root
        self.sessions_root = project_root / ".aiagent-sessions"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.sessions_root / "latest.json"
        self.session_id = session_id or self._new_session_id()
        self.session_root = self.sessions_root / self.session_id
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.session_root / "session.json"

    def _new_session_id(self) -> str:
        stamp = datetime.now().strftime("session_%Y%m%d_%H%M%S")
        return f"{stamp}_{uuid.uuid4().hex[:8]}"

    def write_manifest(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        manifest = dict(payload)
        manifest["session_id"] = self.session_id
        manifest["session_root"] = str(self.session_root)
        manifest["updated_at"] = now_iso()
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.index_path.write_text(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "session_root": str(self.session_root),
                    "manifest_path": str(self.manifest_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return manifest

    def read_manifest(self) -> Dict[str, Any]:
        if not self.manifest_path.exists():
            return {"ok": False, "error": "session manifest not found", "path": str(self.manifest_path)}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def read_latest_manifest(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return {"ok": False, "error": "latest session index not found", "path": str(self.index_path)}
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        manifest_path = Path(payload["manifest_path"])
        if not manifest_path.exists():
            return {"ok": False, "error": "latest session manifest missing", "path": str(manifest_path)}
        return json.loads(manifest_path.read_text(encoding="utf-8"))


class RuntimeStateStore:
    def __init__(self, project_root: Path, session_root: Path):
        self.project_root = project_root
        self.session_root = session_root
        self.root = session_root / "runtime"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = session_root / "runtime_latest.json"

    def path_for(self, agent_id: str) -> Path:
        return self.root / f"{agent_id}.json"

    def write(self, agent_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(payload)
        enriched["updated_at"] = now_iso()
        enriched["pid"] = os.getpid()
        target = self.path_for(agent_id)
        target.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
        self.index_path.write_text(json.dumps({"agent_id": agent_id, "path": str(target)}, ensure_ascii=False, indent=2), encoding="utf-8")
        return enriched

    def read(self, agent_id: str | None = None) -> Dict[str, Any]:
        target = self.path_for(agent_id) if agent_id else self.index_path
        if not target.exists():
            return {"ok": False, "error": "runtime state not found", "path": str(target)}
        payload = json.loads(target.read_text(encoding="utf-8"))
        if agent_id is None and "path" in payload:
            resolved = Path(payload["path"])
            if not resolved.exists():
                return {"ok": False, "error": "indexed runtime state missing", "path": str(resolved)}
            return json.loads(resolved.read_text(encoding="utf-8"))
        return payload

    def list_states(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            if path.name == "latest.json":
                continue
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                items.append({"ok": False, "error": "invalid json", "path": str(path)})
        return items


class AgentRegistry:
    def __init__(self, project_root: Path, session_root: Path):
        self.project_root = project_root
        self.session_root = session_root
        self.path = session_root / "registry.json"

    def _load_all(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save_all(self, agents: Dict[str, Dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert(self, record: Dict[str, Any]) -> Dict[str, Any]:
        agents = self._load_all()
        record = dict(record)
        record["updated_at"] = now_iso()
        agents[record["agent_id"]] = record
        self._save_all(agents)
        return record

    def get(self, agent_id: str) -> Dict[str, Any] | None:
        return self._load_all().get(agent_id)

    def list_all(self) -> List[Dict[str, Any]]:
        return list(self._load_all().values())

    def add_child(self, parent_agent_id: str, child_agent_id: str) -> None:
        agents = self._load_all()
        parent = agents.get(parent_agent_id)
        if parent is None:
            return
        children = parent.get("children", [])
        if child_agent_id not in children:
            children.append(child_agent_id)
        parent["children"] = children
        parent["updated_at"] = now_iso()
        agents[parent_agent_id] = parent
        self._save_all(agents)

    def descendants_of(self, agent_id: str) -> List[str]:
        agents = self._load_all()
        descendants: List[str] = []
        stack = list(agents.get(agent_id, {}).get("children", []))
        while stack:
            current = stack.pop()
            descendants.append(current)
            stack.extend(agents.get(current, {}).get("children", []))
        return descendants

    def terminate_tree(self, agent_id: str, reason: str = "") -> List[str]:
        agents = self._load_all()
        tree = self.descendants_of(agent_id) + [agent_id]
        for current in tree:
            record = agents.get(current)
            if not record:
                continue
            record["status"] = "terminated"
            record["termination_reason"] = reason
            record["updated_at"] = now_iso()
            agents[current] = record
        self._save_all(agents)
        return tree


class Mailbox:
    def __init__(self, project_root: Path, session_root: Path):
        self.project_root = project_root
        self.session_root = session_root
        self.root = session_root / "mailbox"
        self.inbox_dir = self.root / "inbox"
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

    def _title_for(self, kind: str, action: str, body: str, sender: str) -> str:
        text = (body or "").strip().replace("\n", " ")
        if kind == "user_request":
            return text[:60] or f"User request from {sender}"
        if kind == "agent_reply":
            return f"Reply from {sender}"
        if kind == "task_progress":
            return f"Task progress from {sender}"
        if kind == "task_result":
            return f"Task result from {sender}"
        return (text[:60] or f"{kind}:{action} from {sender}").strip()

    def _path_for_message(self, message: Dict[str, Any]) -> Path:
        return self.inbox_dir / f"{message['created_at'].replace(':', '-')}__{message['id']}.json"

    def _read_message(self, path: Path) -> Dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        payload.setdefault("state", "unread")
        payload.setdefault("title", self._title_for(payload.get("kind", ""), payload.get("action", ""), payload.get("body", ""), payload.get("sender", "")))
        payload.setdefault("claimed_by", None)
        payload.setdefault("claimed_at", None)
        payload.setdefault("completed_at", None)
        return payload

    def _write_message(self, path: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def _summary_view(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": payload.get("id"),
            "thread_id": payload.get("thread_id"),
            "reply_to": payload.get("reply_to"),
            "sender": payload.get("sender"),
            "recipient": payload.get("recipient"),
            "kind": payload.get("kind"),
            "action": payload.get("action"),
            "title": payload.get("title"),
            "state": payload.get("state", "unread"),
            "created_at": payload.get("created_at"),
            "claimed_by": payload.get("claimed_by"),
            "claimed_at": payload.get("claimed_at"),
            "completed_at": payload.get("completed_at"),
        }

    def build_message(
        self,
        *,
        sender: str,
        recipient: str,
        kind: str,
        action: str,
        body: str = "",
        reason: str = "",
        metadata: Dict[str, Any] | None = None,
        thread_id: str | None = None,
        reply_to: str | None = None,
    ) -> Dict[str, Any]:
        return {
            "id": new_id("msg"),
            "thread_id": thread_id or new_id("thread"),
            "reply_to": reply_to,
            "sender": sender,
            "recipient": recipient,
            "kind": kind,
            "action": action,
            "body": body,
            "reason": reason,
            "metadata": metadata or {},
            "created_at": now_iso(),
            "title": self._title_for(kind, action, body, sender),
            "state": "unread",
            "claimed_by": None,
            "claimed_at": None,
            "completed_at": None,
        }

    def send(self, **kwargs: Any) -> Dict[str, Any]:
        message = self.build_message(**kwargs)
        path = self._path_for_message(message)
        path.write_text(json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "message": message, "path": str(path)}

    def list_unread(self, recipient: str, include_broadcast: bool = True) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for path in sorted(self.inbox_dir.glob("*.json")):
            payload = self._read_message(path)
            if payload is None:
                continue
            target = payload.get("recipient", "")
            if target != recipient and not (include_broadcast and target == "*"):
                continue
            if payload.get("state") != "unread":
                continue
            messages.append({"path": path, "message": self._summary_view(payload)})
        return messages

    def claim(self, message_id: str, recipient: str, claimed_by: str, include_broadcast: bool = True) -> Dict[str, Any]:
        for path in sorted(self.inbox_dir.glob("*.json")):
            payload = self._read_message(path)
            if payload is None or payload.get("id") != message_id:
                continue
            target = payload.get("recipient", "")
            if target != recipient and not (include_broadcast and target == "*"):
                break
            if payload.get("state") != "unread":
                return {"ok": False, "error": f"message is already {payload.get('state')}", "message": self._summary_view(payload)}
            payload["state"] = "in_progress"
            payload["claimed_by"] = claimed_by
            payload["claimed_at"] = now_iso()
            self._write_message(path, payload)
            return {"ok": True, "path": str(path), "message": payload}
        return {"ok": False, "error": f"message not found: {message_id}"}

    def complete(self, message_id: str, recipient: str, claimed_by: str | None = None, include_broadcast: bool = True) -> Dict[str, Any]:
        for path in sorted(self.inbox_dir.glob("*.json")):
            payload = self._read_message(path)
            if payload is None or payload.get("id") != message_id:
                continue
            target = payload.get("recipient", "")
            if target != recipient and not (include_broadcast and target == "*"):
                break
            if payload.get("state") == "done":
                return {"ok": True, "path": str(path), "message": payload}
            if claimed_by and payload.get("claimed_by") not in {None, claimed_by}:
                return {"ok": False, "error": f"message claimed by {payload.get('claimed_by')}", "message": self._summary_view(payload)}
            payload["state"] = "done"
            payload["completed_at"] = now_iso()
            self._write_message(path, payload)
            return {"ok": True, "path": str(path), "message": payload}
        return {"ok": False, "error": f"message not found: {message_id}"}

    def receive(self, recipient: str, include_broadcast: bool = True) -> List[Dict[str, Any]]:
        return self.list_unread(recipient=recipient, include_broadcast=include_broadcast)

    def acknowledge(self, path: Path) -> None:
        payload = self._read_message(path)
        if payload is None:
            return
        payload["state"] = "done"
        payload["completed_at"] = now_iso()
        self._write_message(path, payload)

    def pending(self, recipient: str | None = None, state: str | None = None) -> List[Dict[str, Any]]:
        items = []
        for path in sorted(self.inbox_dir.glob("*.json")):
            payload = self._read_message(path)
            if payload is None:
                payload = {"id": path.stem, "error": "invalid json"}
            if recipient and payload.get("recipient") not in {recipient, "*"}:
                continue
            if state and payload.get("state", "unread") != state:
                continue
            items.append(payload)
        return items
