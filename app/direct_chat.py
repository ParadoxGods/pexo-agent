from __future__ import annotations

from datetime import datetime
import os
import re
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
from .models import AgentProfile, Artifact, ChatMessage, ChatSession, Memory, Profile
from .paths import PROJECT_ROOT

PREFERRED_CHAT_BACKENDS = ("codex", "gemini", "claude")
CONVERSATION_HINTS = (
    "hello",
    "hi",
    "hey",
    "thanks",
    "thank you",
    "testing",
    "test",
    "this is a test",
    "are you there",
    "are you online",
    "who are you",
    "what can you do",
    "help",
)
TASK_HINTS = (
    "build",
    "create",
    "design",
    "implement",
    "fix",
    "refactor",
    "review",
    "audit",
    "analyze",
    "plan",
    "write",
    "edit",
    "update",
    "change",
    "debug",
    "optimize",
    "scaffold",
    "generate",
    "develop",
    "start a new",
    "new website",
    "landing page",
    "dashboard",
    "repo",
    "repository",
    "codebase",
)
BRAIN_LOOKUP_HINTS = (
    "memory",
    "remember",
    "recall",
    "artifact",
    "artifacts",
    "stored",
    "store",
    "session",
    "sessions",
    "agent",
    "agents",
    "profile",
    "runtime",
    "telemetry",
    "readme",
    "what do you know",
    "what do we know",
    "what is stored",
    "what's stored",
)


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


def _normalize_chat_text(message: str) -> str:
    return " ".join((message or "").strip().lower().split())


def _contains_hint(text: str, hint: str) -> bool:
    normalized_hint = hint.strip().lower()
    if not normalized_hint:
        return False
    if " " in normalized_hint:
        return normalized_hint in text
    return re.search(rf"\b{re.escape(normalized_hint)}\b", text) is not None


def _looks_like_conversation(text: str) -> bool:
    if not text:
        return True
    if any(_contains_hint(text, hint) for hint in CONVERSATION_HINTS):
        return True
    words = text.split()
    if len(words) <= 10 and not any(_contains_hint(text, hint) for hint in TASK_HINTS + BRAIN_LOOKUP_HINTS):
        return True
    if text.endswith("?") and not any(_contains_hint(text, hint) for hint in TASK_HINTS):
        return True
    return False


def _looks_like_brain_lookup(text: str) -> bool:
    if not text:
        return False
    return any(_contains_hint(text, hint) for hint in BRAIN_LOOKUP_HINTS)


def _looks_like_task(text: str) -> bool:
    if not text:
        return False
    return any(_contains_hint(text, hint) for hint in TASK_HINTS)


def _infer_chat_mode(chat_session: ChatSession, latest_user_message: str) -> str:
    text = _normalize_chat_text(latest_user_message)
    previous_mode = str((chat_session.details or {}).get("mode") or "").strip().lower()

    if _looks_like_brain_lookup(text):
        return "brain_lookup"
    if _looks_like_task(text):
        return "task"
    if previous_mode == "task" and text and not _looks_like_conversation(text):
        return "task"
    return "conversation"


def _profile_summary(db: Session) -> str:
    profile = db.query(Profile).filter(Profile.name == "default_user").first()
    if not profile:
        return "No user profile is configured."
    personality = " ".join((profile.personality_prompt or "").split()).strip()
    scripting = ""
    if isinstance(profile.scripting_preferences, dict):
        scripting = str(profile.scripting_preferences.get("scripting_preferences") or "").strip()
    parts = [part for part in (personality, scripting) if part]
    summary = " | ".join(parts) if parts else "Profile is configured."
    return f"Profile {profile.name}: {summary}"


def _agent_summary(db: Session, limit: int = 8) -> str:
    agents = (
        db.query(AgentProfile)
        .order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc())
        .limit(max(1, min(limit, 12)))
        .all()
    )
    if not agents:
        return "No agents are registered."
    rendered = []
    for agent in agents:
        role = (agent.role or "").strip()
        rendered.append(f"{agent.name} ({role})" if role else agent.name)
    return "Agents: " + ", ".join(rendered)


def _memory_summary(db: Session, query: str, limit: int = 3) -> str:
    needle = _normalize_chat_text(query)
    query_obj = db.query(Memory).filter(Memory.is_archived.is_(False))
    memories = []
    if needle and not any(phrase in needle for phrase in ("what do you know", "what do we know", "what is stored", "what's stored")):
        term = f"%{needle}%"
        memories = (
            query_obj.filter((Memory.content.ilike(term)) | (Memory.task_context.ilike(term)))
            .order_by(Memory.updated_at.desc().nullslast(), Memory.created_at.desc(), Memory.id.desc())
            .limit(max(1, min(limit, 6)))
            .all()
        )
    if not memories:
        memories = (
            query_obj.order_by(Memory.updated_at.desc().nullslast(), Memory.created_at.desc(), Memory.id.desc())
            .limit(max(1, min(limit, 6)))
            .all()
        )
    if not memories:
        return "Matching memories: none."
    lines = []
    for memory in memories:
        compact = " ".join((memory.content or "").split()).strip()
        snippet = compact if len(compact) <= 180 else f"{compact[:180].rstrip()}..."
        lines.append(f"- [{memory.task_context or 'general'}] {snippet}")
    return "Matching memories:\n" + "\n".join(lines)


def _artifact_summary(db: Session, query: str, limit: int = 3) -> str:
    needle = _normalize_chat_text(query)
    query_obj = db.query(Artifact)
    artifacts = []
    if needle and not any(phrase in needle for phrase in ("what do you know", "what do we know", "what is stored", "what's stored")):
        term = f"%{needle}%"
        artifacts = (
            query_obj.filter(
                (Artifact.name.ilike(term))
                | (Artifact.source_uri.ilike(term))
                | (Artifact.extracted_text.ilike(term))
                | (Artifact.task_context.ilike(term))
            )
            .order_by(Artifact.updated_at.desc().nullslast(), Artifact.created_at.desc(), Artifact.id.desc())
            .limit(max(1, min(limit, 6)))
            .all()
        )
    if not artifacts:
        artifacts = (
            query_obj.order_by(Artifact.updated_at.desc().nullslast(), Artifact.created_at.desc(), Artifact.id.desc())
            .limit(max(1, min(limit, 6)))
            .all()
        )
    if not artifacts:
        return "Matching artifacts: none."
    lines = []
    for artifact in artifacts:
        preview = " ".join((artifact.extracted_text or "").split()).strip()
        snippet = preview if len(preview) <= 140 else f"{preview[:140].rstrip()}..."
        descriptor = artifact.name or artifact.source_uri or f"artifact:{artifact.id}"
        if snippet:
            lines.append(f"- {descriptor}: {snippet}")
        else:
            lines.append(f"- {descriptor}")
    return "Matching artifacts:\n" + "\n".join(lines)


def _build_brain_lookup_context(db: Session, query: str) -> str:
    sections = [
        _profile_summary(db),
        _agent_summary(db),
        _memory_summary(db, query),
        _artifact_summary(db, query),
    ]
    return "\n\n".join(section for section in sections if section)


def _format_local_date() -> str:
    now = datetime.now().astimezone()
    day = now.day
    return f"{now.strftime('%A')}, {now.strftime('%B')} {day}, {now.year}"


def _build_local_conversation_reply(user_message: str) -> str | None:
    text = _normalize_chat_text(user_message)
    if not text:
        return "Pexo is online and ready."

    if any(_contains_hint(text, hint) for hint in ("what is your name", "what's your name", "your name")):
        return "My name is Pexo."

    if any(
        phrase in text
        for phrase in (
            "what day is today",
            "what is todays day",
            "what is today's day",
            "what is today",
            "what's today",
            "todays date",
            "today's date",
            "what is todays date",
            "what is today's date",
        )
    ):
        return f"Today is {_format_local_date()}."

    if any(
        phrase in text
        for phrase in (
            "what time is it",
            "what's the time",
            "what is the time",
            "current time",
        )
    ):
        return f"It is {datetime.now().astimezone().strftime('%I:%M %p').lstrip('0')}."

    if any(_contains_hint(text, hint) for hint in ("thank you", "thanks")):
        return "You're welcome. Pexo is ready for the next step."

    if any(_contains_hint(text, hint) for hint in ("what can you do", "help")):
        return (
            "Pexo is online. I can keep local memory, search stored artifacts, manage agents, "
            "and coordinate real work when a task actually needs it."
        )

    if _contains_hint(text, "who are you"):
        return "I'm Pexo, your local-first control plane for memory, artifacts, agents, and task flow."

    if any(_contains_hint(text, hint) for hint in ("are you there", "are you online", "testing", "test", "hello", "hi", "hey")):
        return "Pexo is online and ready. Ask for a task, stored context, or an agent change when you're ready."

    return None


def _build_local_lookup_reply(db: Session, user_message: str) -> str:
    text = _normalize_chat_text(user_message)
    broad_lookup = any(
        _contains_hint(text, phrase) for phrase in ("what do you know", "what do we know", "what is stored", "what's stored")
    )

    sections: list[str] = []
    if broad_lookup or any(_contains_hint(text, hint) for hint in ("profile", "runtime")):
        sections.append(_profile_summary(db))
    if broad_lookup or any(_contains_hint(text, hint) for hint in ("agent", "agents")):
        sections.append(_agent_summary(db))
    if broad_lookup or any(_contains_hint(text, hint) for hint in ("memory", "remember", "recall")):
        sections.append(_memory_summary(db, user_message))
    if broad_lookup or any(_contains_hint(text, hint) for hint in ("artifact", "artifacts", "readme", "stored")):
        sections.append(_artifact_summary(db, user_message))

    if not sections:
        sections.extend(
            [
                _memory_summary(db, user_message),
                _artifact_summary(db, user_message),
            ]
        )

    return "Here's what Pexo has locally:\n\n" + "\n\n".join(section for section in sections if section)


def _looks_like_generic_backend_filler(text: str) -> bool:
    normalized = _normalize_chat_text(text)
    if not normalized:
        return True
    generic_phrases = (
        "ill act as the user-facing pexo assistant",
        "ill operate as the user-facing pexo assistant",
        "i'll act as the user-facing pexo assistant",
        "i'll operate as the user-facing pexo assistant",
        "send the task, question, or workflow you want handled",
        "what do you want to do next",
        "what do you want to do",
    )
    return any(phrase in normalized for phrase in generic_phrases)


def _normalize_backend_reply(db: Session, *, mode: str, user_message: str, assistant_text: str) -> str:
    cleaned = (assistant_text or "").strip()
    if cleaned and not _looks_like_generic_backend_filler(cleaned):
        return cleaned

    local_reply = _maybe_build_local_reply(db, mode=mode, user_message=user_message)
    if local_reply:
        return local_reply

    if mode == "task":
        return "Pexo is ready to handle the task. Tell me the outcome you want, and I'll continue from there."
    return "Pexo is online and ready."


def _maybe_build_local_reply(db: Session, *, mode: str, user_message: str) -> str | None:
    if mode == "conversation":
        return _build_local_conversation_reply(user_message)
    if mode == "brain_lookup":
        return _build_local_lookup_reply(db, user_message)
    return None


def _default_workspace_path(explicit_path: str | None = None) -> str:
    if explicit_path:
        return str(Path(explicit_path).expanduser().resolve(strict=False))
    try:
        candidate = Path.cwd().resolve(strict=False)
    except OSError:
        candidate = PROJECT_ROOT
    windows_dir = Path(os.environ.get("WINDIR") or r"C:\Windows").resolve(strict=False)
    if candidate == windows_dir or windows_dir in candidate.parents:
        return str(Path.home().resolve(strict=False))
    return str(candidate)


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

def _build_conversation_prompt(
    *,
    backend_name: str,
    chat_session: ChatSession,
    latest_user_message: str,
    history_excerpt: str,
) -> str:
    return (
        "You are the user-facing Pexo assistant.\n"
        "This turn is conversation mode.\n"
        "Do not start or continue a structured Pexo task for this turn.\n"
        "Do not invoke Supervisor-style orchestration for greetings, smoke tests, casual chat, or short direct questions.\n"
        "If the user is simply testing the chat, answer plainly that Pexo is online and ready.\n"
        "If the user asks a short direct question, answer that question directly.\n"
        "Ask a clarification question only if the request cannot be answered responsibly without one.\n"
        "Keep the reply short, plain, and natural.\n\n"
        "Do not respond with meta filler like 'I'll act as the user-facing Pexo assistant' or 'Send the task you want handled'.\n\n"
        f"Direct chat backend: {backend_name}\n"
        f"Direct chat session: {chat_session.id}\n"
        f"Workspace path: {chat_session.workspace_path or str(PROJECT_ROOT)}\n\n"
        f"Recent direct chat transcript:\n{history_excerpt}\n\n"
        f"Latest user message:\n{latest_user_message}\n"
    )


def _build_lookup_prompt(
    *,
    backend_name: str,
    chat_session: ChatSession,
    latest_user_message: str,
    history_excerpt: str,
    local_context: str,
) -> str:
    return (
        "You are the user-facing Pexo assistant.\n"
        "This turn is brain lookup mode.\n"
        "The user is asking what Pexo already knows, stores, or remembers.\n"
        "Answer from the local Pexo context below.\n"
        "Do not start or continue a structured Pexo task for this turn.\n"
        "Do not ask a clarification question unless the user's request is genuinely ambiguous.\n"
        "If the local context does not contain the answer, say that plainly.\n"
        "Keep the reply concise and practical.\n\n"
        "Do not respond with meta filler like 'I'll act as the user-facing Pexo assistant'.\n\n"
        f"Direct chat backend: {backend_name}\n"
        f"Direct chat session: {chat_session.id}\n"
        f"Workspace path: {chat_session.workspace_path or str(PROJECT_ROOT)}\n\n"
        f"Recent direct chat transcript:\n{history_excerpt}\n\n"
        f"Local Pexo context:\n{local_context}\n\n"
        f"Latest user message:\n{latest_user_message}\n"
    )


def _build_task_prompt(
    *,
    backend_name: str,
    chat_session: ChatSession,
    latest_user_message: str,
    history_excerpt: str,
) -> str:
    return (
        "You are the user-facing Pexo assistant.\n"
        "This turn is task mode.\n"
        "The user is asking Pexo to accomplish real work.\n"
        "Treat the connected Pexo MCP server as your default local brain and control plane.\n"
        "Prefer handling straightforward one-step work directly.\n"
        "Use structured Pexo task flow only when the work is clearly multi-step, needs durable coordination, or truly needs one clarification question.\n"
        "Do not expose raw orchestration internals unless the user explicitly asks for them.\n"
        "Keep the reply plain and outcome-focused.\n\n"
        "Do not respond with meta filler like 'I'll act as the user-facing Pexo assistant'.\n\n"
        f"Direct chat backend: {backend_name}\n"
        f"Direct chat session: {chat_session.id}\n"
        f"Workspace path: {chat_session.workspace_path or str(PROJECT_ROOT)}\n\n"
        f"Recent direct chat transcript:\n{history_excerpt}\n\n"
        f"Latest user message:\n{latest_user_message}\n"
    )


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
    history_excerpt = _history_excerpt(db, session.id)
    mode = _infer_chat_mode(session, user_message)
    if mode == "brain_lookup":
        assistant_prompt = _build_lookup_prompt(
            backend_name=backend_name,
            chat_session=session,
            latest_user_message=user_message,
            history_excerpt=history_excerpt,
            local_context=_build_brain_lookup_context(db, user_message),
        )
    elif mode == "task":
        assistant_prompt = _build_task_prompt(
            backend_name=backend_name,
            chat_session=session,
            latest_user_message=user_message,
            history_excerpt=history_excerpt,
        )
    else:
        assistant_prompt = _build_conversation_prompt(
            backend_name=backend_name,
            chat_session=session,
            latest_user_message=user_message,
            history_excerpt=history_excerpt,
        )
    assistant_text = _maybe_build_local_reply(
        db,
        mode=mode,
        user_message=user_message,
    )
    if assistant_text is None:
        raw_result = run_direct_chat_backend(
            backend_name,
            assistant_prompt,
            session.workspace_path,
            timeout_seconds=timeout_seconds,
        )
        assistant_text = _normalize_backend_reply(
            db,
            mode=mode,
            user_message=user_message,
            assistant_text=raw_result or "",
        )

    session.status = "answered"
    details = dict(session.details or {})
    details["last_user_message"] = user_message
    details["last_assistant_message"] = assistant_text
    details["mode"] = mode
    details["connected_backend"] = backend_name
    session.details = details

    _store_message(
        db,
        session.id,
        "assistant",
        assistant_text,
        details={
            "status": "answered",
            "backend": backend_name,
            "mode": mode,
        },
    )

    db.commit()
    db.refresh(session)
    invalidate_many("chat_sessions", "admin_snapshot", "telemetry")
    return {
        **get_chat_session_payload(db, session.id),
        "reply": {
            "status": "answered",
            "user_message": assistant_text,
            "question": None,
            "role": None,
            "next_action": None,
            "pexo_session_id": session.pexo_session_id,
        },
    }
