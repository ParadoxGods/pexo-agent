from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from .cache import invalidate_many
from .client_connect import SUPPORTED_CLIENTS, build_client_connection_plan, connect_clients
from .database import ensure_db_ready
from .models import ChatMessage, ChatSession
from .paths import PROJECT_ROOT
from .routers.orchestrator import (
    PromptRequest,
    SimpleContinueRequest,
    continue_simple_task,
    get_simple_task_status,
    start_simple_task,
)

PREFERRED_CHAT_BACKENDS = ("codex", "gemini", "claude")
DIRECT_CHAT_TURN_LIMIT = 8


def serialize_chat_session(session: ChatSession, *, message_count: int | None = None) -> dict:
    payload = {
        "id": session.id,
        "title": session.title,
        "backend": session.backend,
        "workspace_path": session.workspace_path,
        "pexo_session_id": session.pexo_session_id,
        "status": session.status,
        "details": session.details or {},
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }
    if message_count is not None:
        payload["message_count"] = int(message_count)
    return payload


def serialize_chat_message(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "chat_session_id": message.chat_session_id,
        "role": message.role,
        "content": message.content,
        "details": message.details or {},
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def _session_title(message: str) -> str:
    compact = " ".join((message or "").split()).strip()
    if not compact:
        return "New Chat"
    if len(compact) <= 72:
        return compact
    return f"{compact[:72].rstrip()}..."


def _default_workspace_path(explicit_path: str | None = None) -> str:
    if explicit_path:
        return str(Path(explicit_path).expanduser().resolve(strict=False))
    try:
        return str(Path.cwd().resolve(strict=False))
    except OSError:
        return str(PROJECT_ROOT)


def list_chat_backends(scope: str = "user") -> dict:
    results = []
    for client in SUPPORTED_CLIENTS:
        plan = build_client_connection_plan(client, scope=scope)
        results.append(
            {
                "name": client,
                "available": bool(plan["available"]),
                "binary": plan["binary"],
                "target_command": plan["target"]["display"],
                "manual_command": plan["manual_command"],
            }
        )
    default_backend = next((entry["name"] for entry in results if entry["available"]), None)
    return {
        "default_backend": default_backend,
        "results": results,
    }


def _resolve_backend_name(preferred: str | None = None) -> str:
    normalized = (preferred or "auto").strip().lower()
    if normalized and normalized != "auto":
        plan = build_client_connection_plan(normalized, scope="user")
        if not plan["available"]:
            raise RuntimeError(f"{normalized} is not installed or not visible in PATH.")
        return normalized

    for candidate in PREFERRED_CHAT_BACKENDS:
        plan = build_client_connection_plan(candidate, scope="user")
        if plan["available"]:
            return candidate
    raise RuntimeError("No supported direct-chat backend is installed. Install Codex, Gemini, or Claude and connect Pexo first.")


def _ensure_backend_connected(backend_name: str) -> None:
    report = connect_clients(target=backend_name, scope="user", dry_run=False)
    if report["status"] == "failed":
        raise RuntimeError(f"Unable to connect {backend_name} to the Pexo MCP server.")


def _wrap_command(invoker: str, args: list[str]) -> list[str]:
    if os.name == "nt" and invoker.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", invoker, *args]
    return [invoker, *args]


def _history_excerpt(db: Session, chat_session_id: str, limit: int = 10) -> str:
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_session_id == chat_session_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
        .all()
    )
    if not messages:
        return "No prior chat turns."
    ordered = list(reversed(messages))
    return "\n".join(f"{message.role.upper()}: {message.content}" for message in ordered)


def _build_worker_prompt(
    *,
    backend_name: str,
    chat_session: ChatSession,
    role: str | None,
    instruction: str,
    latest_user_message: str,
    history_excerpt: str,
) -> str:
    return (
        "You are completing one internal Pexo worker turn.\n"
        "Do not talk to the user directly.\n"
        "Return only the work product needed by the instruction.\n"
        "If raw JSON is required, return raw JSON only with no markdown fences.\n"
        "Pexo already owns the session state. Do not start a new Pexo task, do not continue the current task with Pexo control tools, and do not restate orchestration internals.\n"
        "You may use the connected Pexo MCP server for memory lookup, artifact lookup, tool execution, or agent management if that materially improves the result.\n\n"
        f"Direct chat backend: {backend_name}\n"
        f"Direct chat session: {chat_session.id}\n"
        f"Pexo task session: {chat_session.pexo_session_id or 'none'}\n"
        f"Workspace path: {chat_session.workspace_path or str(PROJECT_ROOT)}\n"
        f"Assigned role: {role or 'Worker'}\n\n"
        f"Recent direct chat transcript:\n{history_excerpt}\n\n"
        f"Latest user message:\n{latest_user_message}\n\n"
        f"Internal instruction:\n{instruction}\n"
    )


def _coerce_backend_result(text: str) -> Any:
    payload = (text or "").strip()
    if not payload:
        return ""
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


def _run_codex_turn(plan: dict, prompt: str, workspace_path: str, timeout_seconds: int) -> str:
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as handle:
        output_path = Path(handle.name)
    command = _wrap_command(
        plan["invoker"],
        [
            "exec",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--full-auto",
            "-C",
            workspace_path,
            "-o",
            str(output_path),
            prompt,
        ],
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "Codex direct chat turn failed.").strip())
        if output_path.exists():
            result = output_path.read_text(encoding="utf-8", errors="ignore").strip()
            if result:
                return result
        return (completed.stdout or "").strip()
    finally:
        output_path.unlink(missing_ok=True)


def _run_gemini_turn(plan: dict, prompt: str, workspace_path: str, timeout_seconds: int) -> str:
    args = [
        "--prompt",
        prompt,
        "--output-format",
        "text",
        "--yolo",
        "--allowed-mcp-server-names",
        "pexo",
    ]
    if workspace_path:
        args.extend(["--include-directories", workspace_path])
    command = _wrap_command(plan["invoker"], args)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Gemini direct chat turn failed.").strip())
    return (completed.stdout or "").strip()


def _run_claude_turn(plan: dict, prompt: str, timeout_seconds: int) -> str:
    command = _wrap_command(plan["invoker"], ["-p", prompt])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Claude direct chat turn failed.").strip())
    return (completed.stdout or "").strip()


def run_direct_chat_backend(backend_name: str, prompt: str, workspace_path: str, timeout_seconds: int = 300) -> str:
    plan = build_client_connection_plan(backend_name, scope="user")
    if not plan["available"]:
        raise RuntimeError(f"{backend_name} is not installed or not visible in PATH.")
    if backend_name == "codex":
        return _run_codex_turn(plan, prompt, workspace_path, timeout_seconds)
    if backend_name == "gemini":
        return _run_gemini_turn(plan, prompt, workspace_path, timeout_seconds)
    if backend_name == "claude":
        return _run_claude_turn(plan, prompt, timeout_seconds)
    raise RuntimeError(f"Unsupported backend '{backend_name}'.")


def _store_message(db: Session, chat_session_id: str, role: str, content: str, details: dict[str, Any] | None = None) -> ChatMessage:
    message = ChatMessage(
        chat_session_id=chat_session_id,
        role=role,
        content=content,
        details=details or {},
    )
    db.add(message)
    db.flush()
    return message


def create_chat_session(
    db: Session,
    *,
    backend: str = "auto",
    workspace_path: str | None = None,
    title: str | None = None,
) -> dict:
    ensure_db_ready()
    backend_name = _resolve_backend_name(backend)
    _ensure_backend_connected(backend_name)
    session = ChatSession(
        id=str(uuid.uuid4()),
        title=title or "New Chat",
        backend=backend_name,
        workspace_path=_default_workspace_path(workspace_path),
        pexo_session_id=None,
        status="idle",
        details={"connected_backend": backend_name},
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    invalidate_many("chat_sessions", "admin_snapshot")
    return serialize_chat_session(session, message_count=0)


def update_chat_session(
    db: Session,
    *,
    session_id: str,
    title: str | None = None,
    backend: str | None = None,
    workspace_path: str | None = None,
) -> dict:
    ensure_db_ready()
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session is None:
        raise RuntimeError("Chat session not found.")

    if title is not None:
        session.title = title.strip() or session.title
    if backend is not None:
        backend_name = _resolve_backend_name(backend)
        _ensure_backend_connected(backend_name)
        session.backend = backend_name
        details = dict(session.details or {})
        details["connected_backend"] = backend_name
        session.details = details
    if workspace_path is not None:
        session.workspace_path = _default_workspace_path(workspace_path)

    db.commit()
    db.refresh(session)
    invalidate_many("chat_sessions", "admin_snapshot")
    message_count = db.query(func.count(ChatMessage.id)).filter(ChatMessage.chat_session_id == session.id).scalar() or 0
    return serialize_chat_session(session, message_count=message_count)


def delete_chat_session(db: Session, session_id: str) -> dict:
    ensure_db_ready()
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session is None:
        raise RuntimeError("Chat session not found.")
    db.query(ChatMessage).filter(ChatMessage.chat_session_id == session_id).delete()
    db.delete(session)
    db.commit()
    invalidate_many("chat_sessions", "admin_snapshot")
    return {"status": "success", "session_id": session_id}


def get_chat_session_payload(db: Session, session_id: str, *, message_limit: int = 120) -> dict:
    ensure_db_ready()
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session is None:
        raise RuntimeError("Chat session not found.")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_session_id == session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .limit(max(1, min(message_limit, 300)))
        .all()
    )
    message_count = db.query(func.count(ChatMessage.id)).filter(ChatMessage.chat_session_id == session_id).scalar() or 0
    return {
        "session": serialize_chat_session(session, message_count=message_count),
        "messages": [serialize_chat_message(message) for message in messages],
        "available_backends": list_chat_backends(),
    }


def list_chat_sessions(db: Session, limit: int = 20) -> dict:
    ensure_db_ready()
    safe_limit = max(1, min(limit, 100))
    sessions = (
        db.query(ChatSession)
        .order_by(ChatSession.updated_at.desc().nullslast(), ChatSession.created_at.desc(), ChatSession.id.desc())
        .limit(safe_limit)
        .all()
    )
    if not sessions:
        return {
            "sessions": [],
            "available_backends": list_chat_backends(),
        }

    counts = {
        session_id: count
        for session_id, count in (
            db.query(ChatMessage.chat_session_id, func.count(ChatMessage.id))
            .filter(ChatMessage.chat_session_id.in_([session.id for session in sessions]))
            .group_by(ChatMessage.chat_session_id)
            .all()
        )
    }
    return {
        "sessions": [serialize_chat_session(session, message_count=counts.get(session.id, 0)) for session in sessions],
        "available_backends": list_chat_backends(),
    }


def send_chat_message(
    db: Session,
    *,
    session_id: str,
    message: str,
    timeout_seconds: int = 300,
) -> dict:
    ensure_db_ready()
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session is None:
        raise RuntimeError("Chat session not found.")

    backend_name = _resolve_backend_name(session.backend or "auto")
    if not session.workspace_path:
        session.workspace_path = _default_workspace_path()

    details = dict(session.details or {})
    if details.get("connected_backend") != backend_name:
        _ensure_backend_connected(backend_name)
        details["connected_backend"] = backend_name
        session.details = details

    user_message = (message or "").strip()
    if not user_message:
        raise RuntimeError("Chat message cannot be empty.")

    if session.title in {None, "", "New Chat"}:
        session.title = _session_title(user_message)

    _store_message(db, session.id, "user", user_message)

    if session.pexo_session_id:
        current_state = get_simple_task_status(session.pexo_session_id, db)
    else:
        current_state = None

    if not session.pexo_session_id or (current_state and current_state.get("status") == "complete"):
        payload = start_simple_task(
            PromptRequest(user_id="default_user", prompt=user_message, session_id=None),
            db,
        )
        session.pexo_session_id = payload.get("session_id")
    elif current_state and current_state.get("status") == "clarification_required":
        payload = continue_simple_task(
            SimpleContinueRequest(session_id=session.pexo_session_id, clarification_answer=user_message),
            db,
        )
    else:
        payload = current_state or start_simple_task(
            PromptRequest(user_id="default_user", prompt=user_message, session_id=None),
            db,
        )
        if not session.pexo_session_id:
            session.pexo_session_id = payload.get("session_id")

    iterations = 0
    while payload.get("status") == "agent_action_required" and iterations < DIRECT_CHAT_TURN_LIMIT:
        iterations += 1
        prompt = _build_worker_prompt(
            backend_name=backend_name,
            chat_session=session,
            role=payload.get("role"),
            instruction=payload.get("agent_instruction") or payload.get("instruction") or "",
            latest_user_message=user_message,
            history_excerpt=_history_excerpt(db, session.id),
        )
        raw_result = run_direct_chat_backend(
            backend_name,
            prompt,
            session.workspace_path,
            timeout_seconds=timeout_seconds,
        )
        payload = continue_simple_task(
            SimpleContinueRequest(
                session_id=session.pexo_session_id,
                result_data=_coerce_backend_result(raw_result),
            ),
            db,
        )

    assistant_text = payload.get("user_message") or payload.get("response") or "Pexo updated the current task."
    if payload.get("status") == "agent_action_required" and iterations >= DIRECT_CHAT_TURN_LIMIT:
        assistant_text = "Pexo reached the current agent-turn limit for this reply. Send another message to continue."

    session.status = payload.get("status") or "idle"
    details = dict(session.details or {})
    details["last_user_message"] = user_message
    details["last_assistant_message"] = assistant_text
    details["last_role"] = payload.get("role")
    details["next_action"] = payload.get("next_action") or payload.get("status")
    details["connected_backend"] = backend_name
    session.details = details

    _store_message(
        db,
        session.id,
        "assistant",
        assistant_text,
        details={
            "pexo_session_id": session.pexo_session_id,
            "status": payload.get("status"),
            "role": payload.get("role"),
            "next_action": payload.get("next_action"),
            "question": payload.get("question"),
        },
    )

    db.commit()
    db.refresh(session)
    invalidate_many("chat_sessions", "admin_snapshot", "telemetry")
    return {
        **get_chat_session_payload(db, session.id),
        "reply": {
            "status": payload.get("status"),
            "user_message": assistant_text,
            "question": payload.get("question"),
            "role": payload.get("role"),
            "next_action": payload.get("next_action"),
            "pexo_session_id": session.pexo_session_id,
        },
    }
