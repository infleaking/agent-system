#!/usr/bin/env python3
"""
Send a one-off mailbox message to the active agent in the latest session,
or to a specific agent/session when provided.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runtime import Mailbox, SessionStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a temporary mailbox message to an aiagent session")
    parser.add_argument(
        "--body",
        required=True,
        help="Message body to inject",
    )
    parser.add_argument(
        "--action",
        default="pause_and_inject",
        help="Message action, for example: pause_and_inject, pause_only, stop",
    )
    parser.add_argument(
        "--kind",
        default="intervention",
        help="Message kind, defaults to intervention",
    )
    parser.add_argument(
        "--sender",
        default="human",
        help="Sender label recorded in the envelope",
    )
    parser.add_argument(
        "--reason",
        default="manual mailbox injection",
        help="Reason recorded in the envelope",
    )
    parser.add_argument(
        "--agent-id",
        help="Recipient agent id. Defaults to the active agent in the latest session.",
    )
    parser.add_argument(
        "--session-id",
        help="Session id to target. Defaults to the latest session.",
    )
    return parser.parse_args()


def resolve_session(project_root: Path, session_id: str | None) -> tuple[SessionStore, dict]:
    if session_id:
        store = SessionStore(project_root, session_id=session_id)
        manifest = store.read_manifest()
        if manifest.get("ok") is False:
            raise RuntimeError(manifest.get("error", "failed to resolve session"))
        return store, manifest

    sessions_root = project_root / ".aiagent-sessions"
    index_path = sessions_root / "latest.json"
    if not index_path.exists():
        raise RuntimeError(f"latest session index not found: {index_path}")
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    manifest_path = Path(payload["manifest_path"])
    if not manifest_path.exists():
        raise RuntimeError(f"latest session manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    store = SessionStore(project_root, session_id=manifest["session_id"])
    return store, manifest


def main() -> None:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    session_store, manifest = resolve_session(project_root, args.session_id)
    recipient = args.agent_id or manifest.get("active_agent_id") or manifest.get("root_agent_id")
    if not recipient:
        raise RuntimeError("could not determine recipient agent id")

    mailbox = Mailbox(project_root, session_store.session_root)
    result = mailbox.send(
        sender=args.sender,
        recipient=recipient,
        kind=args.kind,
        action=args.action,
        body=args.body,
        reason=args.reason,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "session_id": session_store.session_id,
                "session_root": str(session_store.session_root),
                "recipient": recipient,
                "message": result["message"],
                "path": result["path"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
