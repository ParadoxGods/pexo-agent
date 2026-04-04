from typing import Any

from fastapi import HTTPException

from mcp.server.fastmcp import FastMCP

from .database import SessionLocal, init_db
from .models import AgentProfile, AgentState, Artifact, Profile
from .routers.admin import (
    build_client_surface,
    build_telemetry_payload,
    get_admin_snapshot as build_admin_snapshot,
    serialize_agent,
    serialize_agent_state,
    serialize_profile,
)
from .routers.agents import AgentCreate, create_agent
from .routers.artifacts import (
    ArtifactPathRequest,
    ArtifactTextRequest,
    delete_artifact,
    get_artifact,
    list_artifacts,
    register_artifact_path,
    register_artifact_text,
    serialize_artifact,
)
from .routers.backup import run_backup_for_profile
from .routers.evolve import EvolutionRequest, evolve_agent
from .routers.memory import (
    MemoryMaintenanceRequest,
    MemorySearchRequest,
    MemoryStoreRequest,
    MemoryUpdateRequest,
    delete_memory,
    get_memory,
    list_recent_memories,
    run_memory_maintenance,
    search_memory,
    serialize_memory,
    store_memory,
    update_memory,
)
from .routers.orchestrator import (
    ExecuteRequest,
    PromptRequest,
    SimpleContinueRequest,
    TaskResult,
    continue_simple_task,
    execute_plan,
    get_next_task,
    get_simple_task_status,
    intake_prompt,
    start_simple_task,
    submit_task_result,
)
from .routers.profile import (
    ProfileAnswers,
    QuickSetupRequest,
    build_profile_from_preset,
    derive_profile_answers,
    get_onboarding_questions,
    get_profile_presets,
    upsert_profile,
)
from .routers.runtime import get_runtime_status as build_runtime_status_response, promote_runtime_profile
from .routers.tools import (
    ToolExecutionRequest,
    ToolRegistrationRequest,
    ToolUpdateRequest,
    delete_tool,
    execute_tool,
    get_tool,
    list_tools,
    register_tool,
    update_tool,
)

mcp = FastMCP("Pexo")


def _normalize_http_error(exc: HTTPException) -> ValueError:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return ValueError(detail)


def _with_db(operation):
    db = SessionLocal()
    try:
        return operation(db)
    except HTTPException as exc:
        raise _normalize_http_error(exc) from exc
    finally:
        db.close()


def _require_agent(db, agent_id: int | None = None, agent_name: str | None = None) -> AgentProfile:
    if agent_id is None and not agent_name:
        raise ValueError("Provide either agent_id or agent_name.")

    query = db.query(AgentProfile)
    agent = None
    if agent_id is not None:
        agent = query.filter(AgentProfile.id == agent_id).first()
    elif agent_name:
        agent = query.filter(AgentProfile.name == agent_name).first()

    if not agent:
        raise ValueError("Agent not found.")
    return agent


def _merge_profile_answers(
    db,
    profile_name: str,
    personality_answers: dict[str, str] | None = None,
    scripting_answers: dict[str, str] | None = None,
    backup_path: str | None = None,
    clear_backup_path: bool = False,
) -> ProfileAnswers:
    profile = db.query(Profile).filter(Profile.name == profile_name).first()
    merged = derive_profile_answers(profile)
    merged_personality = dict(merged["personality_answers"])
    merged_scripting = dict(merged["scripting_answers"])

    if personality_answers:
        merged_personality.update(personality_answers)
    if scripting_answers:
        merged_scripting.update(scripting_answers)

    merged_backup_path = ""
    if not clear_backup_path:
        if backup_path is not None:
            merged_backup_path = backup_path
        elif profile and profile.backup_path:
            merged_backup_path = profile.backup_path

    return ProfileAnswers(
        name=profile_name,
        personality_answers=merged_personality,
        scripting_answers=merged_scripting,
        backup_path=merged_backup_path,
        clear_backup_path=clear_backup_path,
    )


def _list_recent_sessions(db, limit: int = 10) -> list[dict]:
    telemetry = build_telemetry_payload(db)
    safe_limit = max(1, min(limit, 100))
    return telemetry["recent_sessions"][:safe_limit]


def _get_session_activity(db, session_id: str, limit: int = 50) -> list[dict]:
    safe_limit = max(1, min(limit, 200))
    states = (
        db.query(AgentState)
        .filter(AgentState.session_id == session_id)
        .order_by(AgentState.created_at.desc(), AgentState.id.desc())
        .limit(safe_limit)
        .all()
    )
    return [serialize_agent_state(state) for state in states]


def _require_artifact(db, artifact_id: int) -> Artifact:
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if artifact is None:
        raise ValueError("Artifact not found.")
    return artifact


def _brain_usage_rules() -> list[str]:
    return [
        "If Pexo is connected, prefer pexo as the default one-call surface for ordinary tasks without waiting for the user to mention Pexo explicitly.",
        "Use pexo_exchange, pexo_bootstrap_brain, pexo_start_task, pexo_continue_task, and pexo_get_task_status only when you need lower-level control.",
        "Show user_message to the user whenever it is present.",
        "Keep internal orchestration instructions hidden unless the user explicitly asks for them.",
        "Use pexo_recall_context to find relevant memory and artifacts before asking the user to repeat context.",
        "Use pexo_remember_context and pexo_attach_context to persist useful context for future sessions.",
    ]


def _summarize_profile(profile: dict | None, profile_answers: dict | None) -> dict:
    if not profile:
        return {
            "configured": False,
            "name": "default_user",
            "summary": "No profile is configured yet.",
        }
    personality = (profile.get("personality_prompt") or "").strip()
    scripting = (profile.get("scripting_preferences") or {}).get("scripting_preferences")
    parts = []
    if personality:
        parts.append(personality)
    if scripting:
        parts.append(str(scripting))
    return {
        "configured": True,
        "name": profile.get("name") or "default_user",
        "summary": " | ".join(parts) or "Profile is configured.",
        "answers": profile_answers or {},
    }


def _summarize_clients(surface: dict) -> dict:
    results = surface.get("results") or []
    connected = [result["client"] for result in results if result.get("status") == "connected"]
    available = [result["client"] for result in results if result.get("status") == "available"]
    missing = [result["client"] for result in results if result.get("status") == "missing"]
    return {
        "status": surface.get("status", "unknown"),
        "connected": connected,
        "available": available,
        "missing": missing,
        "mcp_server": surface.get("mcp_server"),
    }


def _summarize_agents(agents: list[AgentProfile], limit: int = 8) -> list[dict]:
    return [
        {
            "name": agent.name,
            "role": agent.role,
            "capabilities": list(agent.capabilities or []),
            "is_core": bool(agent.is_core),
        }
        for agent in agents[:limit]
    ]


def _truncate(value: str | None, limit: int = 220) -> str:
    if not value:
        return ""
    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit].rstrip()}..."


def _compact_memory_result(payload: dict) -> dict:
    return {
        "id": payload.get("id"),
        "session_id": payload.get("session_id"),
        "task_context": payload.get("task_context"),
        "content": _truncate(payload.get("content"), limit=240),
        "is_archived": bool(payload.get("is_archived")),
        "is_pinned": bool(payload.get("is_pinned")),
    }


def _compact_artifact_result(payload: dict) -> dict:
    return {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "session_id": payload.get("session_id"),
        "task_context": payload.get("task_context"),
        "source_type": payload.get("source_type"),
        "source_uri": payload.get("source_uri"),
        "preview": _truncate(payload.get("preview"), limit=240),
        "has_text": bool(payload.get("has_text")),
    }


def _build_exchange_task_view(task_payload: dict | None, notice: str | None = None) -> dict:
    if task_payload is None:
        response = {
            "status": "context_ready",
            "next_action": "reply_to_user",
            "user_message": "Pexo updated the local brain state.",
        }
    else:
        status = task_payload.get("status", "processing")
        next_action = {
            "clarification_required": "ask_user",
            "agent_action_required": "perform_agent_work",
            "complete": "reply_to_user",
            "processing": "wait",
        }.get(status, "reply_to_user")
        response = {
            "status": status,
            "session_id": task_payload.get("session_id"),
            "next_action": next_action,
            "user_message": task_payload.get("user_message") or task_payload.get("response") or "Pexo updated the task state.",
        }
        for key in ("question", "role", "instruction", "agent_instruction", "final_response", "response"):
            if key in task_payload and task_payload.get(key) is not None:
                response[key] = task_payload.get(key)

    if notice:
        response["notice"] = notice
    return response


def _exchange_operation(
    db,
    *,
    message: str | None = None,
    session_id: str | None = None,
    user_id: str = "default_user",
    query: str | None = None,
    agent_result: Any | None = None,
    remember: str | None = None,
    task_context: str = "general",
    attach_path: str | None = None,
    attach_name: str | None = None,
    attach_text: str | None = None,
    include_brain: bool = False,
    memory_results: int = 4,
    artifact_results: int = 4,
    auto_promote_vector: bool = False,
) -> dict:
    if attach_text and not attach_name:
        attach_text_name = "context.txt"
    else:
        attach_text_name = attach_name

    if message is None and session_id is None and query is None and remember is None and attach_path is None and attach_text is None and not include_brain:
        raise ValueError(
            "Provide a message to start a task, a session_id to continue one, or context to store/recall."
        )

    if session_id is None and agent_result is not None:
        raise ValueError("agent_result requires an existing session_id.")

    writes: dict[str, Any] = {}
    if remember:
        stored_memory = store_memory(
            MemoryStoreRequest(
                session_id=session_id or "brain_session",
                content=remember,
                task_context=task_context,
                auto_promote_vector=auto_promote_vector,
            ),
            db,
        )
        memory_id = stored_memory.get("memory_id")
        writes["memory"] = _compact_memory_result(get_memory(memory_id, db)) if memory_id else None
        if stored_memory.get("runtime") is not None:
            writes["memory_runtime"] = stored_memory.get("runtime")
        if stored_memory.get("promotion_offer") is not None:
            writes["memory_promotion_offer"] = stored_memory.get("promotion_offer")

    artifacts_written = []
    if attach_path:
        stored_artifact = register_artifact_path(
            ArtifactPathRequest(
                path=attach_path,
                session_id=session_id or "brain_session",
                task_context=task_context,
                name=attach_name,
            ),
            db,
        )
        artifact_payload = stored_artifact.get("artifact")
        if artifact_payload:
            artifacts_written.append(_compact_artifact_result(artifact_payload))

    if attach_text:
        stored_text_artifact = register_artifact_text(
            ArtifactTextRequest(
                name=attach_text_name or "context.txt",
                content=attach_text,
                session_id=session_id or "brain_session",
                task_context=task_context,
                source_uri=None,
                content_type="text/plain",
            ),
            db,
        )
        artifact_payload = stored_text_artifact.get("artifact")
        if artifact_payload:
            artifacts_written.append(_compact_artifact_result(artifact_payload))

    if artifacts_written:
        writes["artifacts"] = artifacts_written

    task_payload = None
    notice = None
    if session_id:
        current_status = get_simple_task_status(session_id=session_id, db=db)
        if message is not None and agent_result is not None:
            notice = "Pexo ignored the user message because agent_result was provided for this session."
            task_payload = continue_simple_task(
                SimpleContinueRequest(session_id=session_id, result_data=agent_result),
                db,
            )
        elif message is not None:
            if current_status.get("status") == "clarification_required":
                task_payload = continue_simple_task(
                    SimpleContinueRequest(session_id=session_id, clarification_answer=message),
                    db,
                )
            else:
                task_payload = current_status
                notice = "Pexo did not use the message because this session is not waiting for user clarification."
        elif agent_result is not None:
            if current_status.get("status") == "agent_action_required":
                task_payload = continue_simple_task(
                    SimpleContinueRequest(session_id=session_id, result_data=agent_result),
                    db,
                )
            else:
                task_payload = current_status
                notice = "Pexo did not use agent_result because this session is not waiting for agent work."
        else:
            task_payload = current_status
    elif message is not None:
        task_payload = start_simple_task(
            PromptRequest(user_id=user_id, prompt=message, session_id=None),
            db,
        )

    recall_query = (query or (message if not session_id else None) or remember or "").strip()
    brain = None
    if include_brain or query is not None or (message is not None and session_id is None):
        brain = _brain_bootstrap_payload(
            db,
            prompt=None,
            query=recall_query or None,
            user_id=user_id,
            session_id=session_id,
            memory_results=memory_results,
            artifact_results=artifact_results,
        )
        brain["task"] = None

    exchange = _build_exchange_task_view(task_payload, notice=notice)
    effective_session_id = exchange.get("session_id") or session_id
    response = {
        "mode": "exchange",
        "session_id": effective_session_id,
        **exchange,
    }
    if brain is not None:
        response["brain"] = brain
    if writes:
        response["writes"] = writes
    if recall_query:
        response["query"] = recall_query
    return response


def _brain_bootstrap_payload(
    db,
    *,
    prompt: str | None = None,
    query: str | None = None,
    user_id: str = "default_user",
    session_id: str | None = None,
    memory_results: int = 5,
    artifact_results: int = 5,
) -> dict:
    profile_model = db.query(Profile).filter(Profile.name == user_id).first()
    profile_payload = serialize_profile(profile_model)
    profile_answers = derive_profile_answers(profile_model)
    client_surface = build_client_surface()
    runtime = build_runtime_status_response(db)
    agents = db.query(AgentProfile).order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc()).all()

    recall_query = (query or prompt or "").strip()
    memory_payload = (
        search_memory(
            MemorySearchRequest(query=recall_query, n_results=max(1, min(memory_results, 10))),
            db,
        )
        if recall_query
        else list_recent_memories(limit=max(1, min(memory_results, 10)), include_archived=True, db=db)
    )
    artifact_payload = list_artifacts(
        limit=max(1, min(artifact_results, 10)),
        query=recall_query or None,
        session_id=None,
        task_context=None,
        db=db,
    )
    task_payload = (
        start_simple_task(PromptRequest(user_id=user_id, prompt=prompt, session_id=session_id), db)
        if prompt
        else None
    )

    memory_items = memory_payload.get("results") if recall_query else memory_payload.get("memories", [])
    return {
        "mode": "brain",
        "user_message": "Pexo is ready. Use the returned context and simple task flow.",
        "operating_contract": _brain_usage_rules(),
        "profile": _summarize_profile(profile_payload, profile_answers),
        "runtime": {
            "active_profile": runtime.get("runtime", {}).get("active_profile"),
            "recommended_promotions": runtime.get("runtime", {}).get("recommended_promotions", []),
            "vector_embeddings_available": runtime.get("runtime", {}).get("vector_embeddings_available"),
            "install_mode": runtime.get("runtime", {}).get("install_mode"),
        },
        "clients": _summarize_clients(client_surface),
        "agents": _summarize_agents(agents),
        "memory": {
            "query": recall_query or None,
            "results": [_compact_memory_result(item) for item in memory_items[: max(1, min(memory_results, 10))]],
        },
        "artifacts": {
            "query": recall_query or None,
            "results": [
                _compact_artifact_result(item)
                for item in (artifact_payload.get("artifacts") or [])[: max(1, min(artifact_results, 10))]
            ],
        },
        "task": task_payload,
    }


@mcp.resource(
    "pexo://brain-guide",
    name="Pexo Brain Guide",
    description="Default operating contract for AI clients using Pexo as a local brain.",
    mime_type="text/markdown",
)
def pexo_brain_guide_resource() -> str:
    rules = "\n".join([f"- {rule}" for rule in _brain_usage_rules()])
    return (
        "# Pexo Brain Guide\n\n"
        "Use Pexo as the default local brain for tasks, memory, artifacts, and execution flow.\n\n"
        "## Default Flow\n\n"
        "1. If Pexo is connected, call `pexo` first for ordinary tasks even when the user does not explicitly mention Pexo.\n"
        "2. Reuse the returned `session_id` with `pexo` for clarification answers or agent results.\n"
        "3. Use `user_message` for user-facing replies.\n"
        "4. Keep `agent_instruction` internal unless the user explicitly asks for orchestration details.\n"
        "5. Use `pexo_recall_context` before asking the user to repeat context when you need extra control.\n"
        "6. Persist useful notes with `pexo_remember_context` and files with `pexo_attach_context`, or fold them into `pexo`.\n\n"
        "## Rules\n\n"
        f"{rules}\n"
    )


@mcp.prompt(
    name="pexo_default_task_flow",
    description="Instruction template for using Pexo as the default local brain for a plain-language task.",
)
def pexo_default_task_prompt(user_task: str) -> list[dict]:
    return [
        {
            "role": "user",
            "content": (
                "Use Pexo as the default local brain for this task. "
                "If Pexo is connected, call pexo first with the task, keep reusing the returned session_id with pexo "
                "for clarification answers or agent results, and keep user-facing replies short by using user_message whenever it is present. "
                f"Task: {user_task}"
            ),
        }
    ]


@mcp.tool()
def pexo(
    message: str | None = None,
    session_id: str | None = None,
    user_id: str = "default_user",
    query: str | None = None,
    agent_result: Any | None = None,
    remember: str | None = None,
    task_context: str = "general",
    attach_path: str | None = None,
    attach_name: str | None = None,
    attach_text: str | None = None,
    include_brain: bool = False,
    memory_results: int = 4,
    artifact_results: int = 4,
    auto_promote_vector: bool = False,
) -> dict:
    """Default one-call autopilot surface. If Pexo is connected, AI clients should use this automatically for ordinary tasks, clarification answers, agent work, and lightweight memory/artifact persistence."""

    return _with_db(
        lambda db: _exchange_operation(
            db,
            message=message,
            session_id=session_id,
            user_id=user_id,
            query=query,
            agent_result=agent_result,
            remember=remember,
            task_context=task_context,
            attach_path=attach_path,
            attach_name=attach_name,
            attach_text=attach_text,
            include_brain=include_brain,
            memory_results=memory_results,
            artifact_results=artifact_results,
            auto_promote_vector=auto_promote_vector,
        )
    )


@mcp.tool()
def pexo_exchange(
    message: str | None = None,
    session_id: str | None = None,
    user_id: str = "default_user",
    query: str | None = None,
    agent_result: Any | None = None,
    remember: str | None = None,
    task_context: str = "general",
    attach_path: str | None = None,
    attach_name: str | None = None,
    attach_text: str | None = None,
    include_brain: bool = False,
    memory_results: int = 4,
    artifact_results: int = 4,
    auto_promote_vector: bool = False,
) -> dict:
    """Primary one-call surface for AI clients. Start or continue a task, recall context, and optionally persist memory or artifacts without manually sequencing multiple Pexo tools."""
    return _with_db(
        lambda db: _exchange_operation(
            db,
            message=message,
            session_id=session_id,
            user_id=user_id,
            query=query,
            agent_result=agent_result,
            remember=remember,
            task_context=task_context,
            attach_path=attach_path,
            attach_name=attach_name,
            attach_text=attach_text,
            include_brain=include_brain,
            memory_results=memory_results,
            artifact_results=artifact_results,
            auto_promote_vector=auto_promote_vector,
        )
    )


@mcp.tool()
def pexo_bootstrap_brain(
    prompt: str | None = None,
    query: str | None = None,
    user_id: str = "default_user",
    session_id: str | None = None,
    memory_results: int = 5,
    artifact_results: int = 5,
) -> dict:
    """Default first call whenever Pexo is available. Returns the current operating contract, profile, client status, relevant context, and optionally starts a simple task."""
    return _with_db(
        lambda db: _brain_bootstrap_payload(
            db,
            prompt=prompt,
            query=query,
            user_id=user_id,
            session_id=session_id,
            memory_results=memory_results,
            artifact_results=artifact_results,
        )
    )


@mcp.tool()
def pexo_recall_context(
    query: str,
    memory_results: int = 5,
    artifact_results: int = 5,
    auto_promote_vector: bool = False,
) -> dict:
    """Simple context recall surface. Searches both memory and artifacts in one call before asking the user to repeat context."""

    def operation(db):
        memory_payload = search_memory(
            MemorySearchRequest(
                query=query,
                n_results=max(1, min(memory_results, 10)),
                auto_promote_vector=auto_promote_vector,
            ),
            db,
        )
        artifact_payload = list_artifacts(
            limit=max(1, min(artifact_results, 10)),
            query=query,
            session_id=None,
            task_context=None,
            db=db,
        )
        return {
            "user_message": f"Pexo found context for '{query}'.",
            "query": query,
            "memory": {
                "results": [_compact_memory_result(item) for item in memory_payload.get("results", [])],
                "runtime": memory_payload.get("runtime"),
                "promotion_offer": memory_payload.get("promotion_offer"),
            },
            "artifacts": {
                "results": [_compact_artifact_result(item) for item in artifact_payload.get("artifacts", [])],
            },
        }

    return _with_db(operation)


@mcp.tool()
def pexo_remember_context(
    content: str,
    task_context: str = "general",
    session_id: str = "brain_session",
    auto_promote_vector: bool = False,
) -> dict:
    """Simple memory write surface. Store a note or decision in Pexo's local brain."""

    def operation(db):
        stored = store_memory(
            MemoryStoreRequest(
                session_id=session_id,
                content=content,
                task_context=task_context,
                auto_promote_vector=auto_promote_vector,
            ),
            db,
        )
        memory_id = stored.get("memory_id")
        memory_payload = get_memory(memory_id, db) if memory_id else None
        return {
            "status": "success",
            "detail": stored.get("status"),
            "user_message": "Pexo stored the context for future tasks.",
            "memory": _compact_memory_result(memory_payload) if memory_payload else None,
            "runtime": stored.get("runtime"),
            "promotion_offer": stored.get("promotion_offer"),
        }

    return _with_db(operation)


@mcp.tool()
def pexo_attach_context(
    path: str,
    session_id: str = "brain_session",
    task_context: str = "general",
    name: str | None = None,
) -> dict:
    """Simple artifact attach surface. Copy a local file into Pexo so later AI sessions can retrieve it."""

    def operation(db):
        stored = register_artifact_path(
            ArtifactPathRequest(
                path=path,
                session_id=session_id,
                task_context=task_context,
                name=name,
            ),
            db,
        )
        artifact_payload = stored.get("artifact")
        return {
            "status": "success",
            "detail": stored.get("status"),
            "user_message": "Pexo attached the file to local context.",
            "artifact": _compact_artifact_result(artifact_payload) if artifact_payload else None,
        }

    return _with_db(operation)


@mcp.tool()
def pexo_attach_text_context(
    name: str,
    content: str,
    session_id: str = "brain_session",
    task_context: str = "general",
    source_uri: str | None = None,
    content_type: str = "text/plain",
) -> dict:
    """Simple text attachment surface. Save generated notes, plans, or summaries as an artifact inside Pexo."""

    def operation(db):
        stored = register_artifact_text(
            ArtifactTextRequest(
                name=name,
                content=content,
                session_id=session_id,
                task_context=task_context,
                source_uri=source_uri,
                content_type=content_type,
            ),
            db,
        )
        artifact_payload = stored.get("artifact")
        return {
            "status": "success",
            "detail": stored.get("status"),
            "user_message": "Pexo saved the text artifact for future retrieval.",
            "artifact": _compact_artifact_result(artifact_payload) if artifact_payload else None,
        }

    return _with_db(operation)


@mcp.tool()
def pexo_read_profile(profile_name: str = "default_user") -> dict:
    """Returns the persisted user profile, derived answer map, and current agent registry."""
    return _with_db(
        lambda db: {
            "profile": serialize_profile(db.query(Profile).filter(Profile.name == profile_name).first()),
            "profile_answers": derive_profile_answers(db.query(Profile).filter(Profile.name == profile_name).first()),
            "agents": [serialize_agent(agent) for agent in db.query(AgentProfile).order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc()).all()],
        }
    )


@mcp.tool()
def pexo_get_profile(profile_name: str = "default_user") -> dict:
    """Returns the stored profile plus the derived answer mapping used by the dashboard editor."""
    return _with_db(
        lambda db: {
            "profile": serialize_profile(db.query(Profile).filter(Profile.name == profile_name).first()),
            "profile_answers": derive_profile_answers(db.query(Profile).filter(Profile.name == profile_name).first()),
        }
    )


@mcp.tool()
def pexo_get_profile_questions() -> dict:
    """Returns the full onboarding/profile questionnaire schema."""
    return get_onboarding_questions()


@mcp.tool()
def pexo_list_profile_presets() -> list[dict]:
    """Lists the built-in profile presets for fast headless setup."""
    return get_profile_presets()["presets"]


@mcp.tool()
def pexo_quick_setup_profile(
    preset_name: str,
    profile_name: str = "default_user",
    backup_path: str = "",
    clear_backup_path: bool = False,
) -> dict:
    """Applies a built-in preset to initialize or refresh a profile quickly."""

    def operation(db):
        answers = build_profile_from_preset(preset_name, name=profile_name, backup_path=backup_path)
        answers.clear_backup_path = clear_backup_path
        profile = upsert_profile(answers, db)
        return {
            "status": "success",
            "profile": serialize_profile(profile),
            "profile_answers": derive_profile_answers(profile),
            "preset_name": preset_name,
        }

    return _with_db(operation)


@mcp.tool()
def pexo_update_profile(
    profile_name: str = "default_user",
    personality_answers: dict[str, str] | None = None,
    scripting_answers: dict[str, str] | None = None,
    backup_path: str | None = None,
    clear_backup_path: bool = False,
) -> dict:
    """Partially updates a profile while preserving unspecified answers."""

    def operation(db):
        answers = _merge_profile_answers(
            db,
            profile_name=profile_name,
            personality_answers=personality_answers,
            scripting_answers=scripting_answers,
            backup_path=backup_path,
            clear_backup_path=clear_backup_path,
        )
        profile = upsert_profile(answers, db)
        return {
            "status": "success",
            "profile": serialize_profile(profile),
            "profile_answers": derive_profile_answers(profile),
        }

    return _with_db(operation)


@mcp.tool()
def pexo_list_agents(include_core: bool = True, include_custom: bool = True) -> list[dict]:
    """Lists registered core and custom agents."""

    def operation(db):
        agents = db.query(AgentProfile).order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc()).all()
        filtered = [
            agent
            for agent in agents
            if (include_core and agent.is_core) or (include_custom and not agent.is_core)
        ]
        return [serialize_agent(agent) for agent in filtered]

    return _with_db(operation)


@mcp.tool()
def pexo_get_agent(agent_id: int | None = None, agent_name: str | None = None) -> dict:
    """Returns a single agent by id or by name."""
    return _with_db(lambda db: serialize_agent(_require_agent(db, agent_id=agent_id, agent_name=agent_name)))


@mcp.tool()
def pexo_create_agent(
    name: str,
    role: str,
    system_prompt: str,
    capabilities: list[str] | None = None,
) -> dict:
    """Creates a new custom agent."""
    return _with_db(
        lambda db: serialize_agent(
            create_agent(
                AgentCreate(name=name, role=role, system_prompt=system_prompt, capabilities=capabilities or []),
                db,
            )
        )
    )


@mcp.tool()
def pexo_update_agent(
    agent_id: int | None = None,
    agent_name: str | None = None,
    name: str | None = None,
    role: str | None = None,
    system_prompt: str | None = None,
    capabilities: list[str] | None = None,
) -> dict:
    """Partially updates an agent by id or by name."""

    def operation(db):
        agent = _require_agent(db, agent_id=agent_id, agent_name=agent_name)
        target_name = name or agent.name
        duplicate = db.query(AgentProfile).filter(AgentProfile.name == target_name, AgentProfile.id != agent.id).first()
        if duplicate:
            raise ValueError("Agent name already registered.")

        agent.name = target_name
        if role is not None:
            agent.role = role
        if system_prompt is not None:
            agent.system_prompt = system_prompt
        if capabilities is not None:
            agent.capabilities = capabilities
        db.commit()
        db.refresh(agent)
        return {"status": "success", "agent": serialize_agent(agent)}

    return _with_db(operation)


@mcp.tool()
def pexo_delete_agent(agent_id: int | None = None, agent_name: str | None = None) -> dict:
    """Deletes a custom agent by id or by name."""

    def operation(db):
        agent = _require_agent(db, agent_id=agent_id, agent_name=agent_name)
        if agent.is_core:
            raise ValueError("Cannot delete core agents.")
        deleted_name = agent.name
        db.delete(agent)
        db.commit()
        return {"status": "success", "message": f"Agent '{deleted_name}' deleted successfully."}

    return _with_db(operation)


@mcp.tool()
def pexo_search_memory(query: str, n_results: int = 3, auto_promote_vector: bool = False) -> dict:
    """Searches Pexo's Global Vector Brain for relevant historical context."""
    return _with_db(
        lambda db: search_memory(
            MemorySearchRequest(query=query, n_results=n_results, auto_promote_vector=auto_promote_vector),
            db,
        )
    )


@mcp.tool()
def pexo_store_memory(
    content: str,
    task_context: str,
    session_id: str = "mcp_session",
    auto_promote_vector: bool = False,
) -> dict:
    """Stores a memory record and triggers lifecycle maintenance."""
    return _with_db(
        lambda db: store_memory(
            MemoryStoreRequest(
                session_id=session_id,
                content=content,
                task_context=task_context,
                auto_promote_vector=auto_promote_vector,
            ),
            db,
        )
    )


@mcp.tool()
def pexo_list_recent_memories(limit: int = 12, include_archived: bool = True) -> dict:
    """Lists recent memories, ordered by last update/creation time."""
    return _with_db(lambda db: list_recent_memories(limit=limit, include_archived=include_archived, db=db))


@mcp.tool()
def pexo_get_memory(memory_id: int) -> dict:
    """Returns a single memory record."""
    return _with_db(lambda db: get_memory(memory_id, db))


@mcp.tool()
def pexo_update_memory(
    memory_id: int,
    content: str | None = None,
    task_context: str | None = None,
    is_compacted: bool | None = None,
    is_pinned: bool | None = None,
    is_archived: bool | None = None,
) -> dict:
    """Updates a stored memory record and keeps vector state synchronized."""
    return _with_db(
        lambda db: update_memory(
            memory_id,
            MemoryUpdateRequest(
                content=content,
                task_context=task_context,
                is_compacted=is_compacted,
                is_pinned=is_pinned,
                is_archived=is_archived,
            ),
            db,
        )
    )


@mcp.tool()
def pexo_delete_memory(memory_id: int) -> dict:
    """Deletes a memory record and removes its vector embedding."""
    return _with_db(lambda db: delete_memory(memory_id, db))


@mcp.tool()
def pexo_run_memory_maintenance(task_context: str | None = None) -> dict:
    """Runs compaction and retention maintenance for memory storage."""
    return _with_db(lambda db: run_memory_maintenance(MemoryMaintenanceRequest(task_context=task_context), db))


@mcp.tool()
def pexo_evolve_agent(agent_name: str, lesson_learned: str) -> dict:
    """Persists a new lesson into an agent's base prompt."""
    return _with_db(lambda db: evolve_agent(EvolutionRequest(agent_name=agent_name, lesson_learned=lesson_learned), db))


@mcp.tool()
def pexo_list_tools() -> list[dict]:
    """Lists Genesis tools registered in the local tool registry."""
    return _with_db(lambda db: list_tools(db))


@mcp.tool()
def pexo_get_tool(tool_name: str) -> dict:
    """Returns a single Genesis tool including its source code."""
    return _with_db(lambda db: get_tool(tool_name, db))


@mcp.tool()
def pexo_register_tool(name: str, description: str, python_code: str) -> dict:
    """Registers a new Genesis tool from Python source code."""
    return _with_db(
        lambda db: register_tool(
            ToolRegistrationRequest(name=name, description=description, python_code=python_code),
            db,
        )
    )


@mcp.tool()
def pexo_update_tool(tool_name: str, description: str | None = None, python_code: str | None = None) -> dict:
    """Updates a Genesis tool's metadata or source code."""
    return _with_db(
        lambda db: update_tool(
            tool_name,
            ToolUpdateRequest(description=description, python_code=python_code),
            db,
        )
    )


@mcp.tool()
def pexo_execute_tool(
    tool_name: str,
    kwargs: dict[str, Any] | None = None,
    session_id: str = "tool_execution",
    working_directory: str | None = None,
    allow_outside_project: bool = False,
    timeout_seconds: int = 30,
) -> dict:
    """Executes a Genesis tool with structured keyword arguments in an isolated subprocess."""
    return _with_db(
        lambda db: execute_tool(
            tool_name,
            ToolExecutionRequest(
                kwargs=kwargs or {},
                session_id=session_id,
                working_directory=working_directory,
                allow_outside_project=allow_outside_project,
                timeout_seconds=timeout_seconds,
            ),
            db,
        )
    )


@mcp.tool()
def pexo_delete_tool(tool_name: str) -> dict:
    """Deletes a Genesis tool from the registry and local filesystem."""
    return _with_db(lambda db: delete_tool(tool_name, db))


@mcp.tool()
def pexo_get_admin_snapshot(memory_limit: int = 12) -> dict:
    """Returns the consolidated dashboard snapshot used by the local admin UI."""
    return _with_db(lambda db: build_admin_snapshot(memory_limit=memory_limit, db=db))


@mcp.tool()
def pexo_get_telemetry() -> dict:
    """Returns session and agent activity telemetry."""
    return _with_db(lambda db: build_telemetry_payload(db))


@mcp.tool()
def pexo_list_sessions(limit: int = 10) -> list[dict]:
    """Lists recent execution sessions with aggregate activity metrics."""
    return _with_db(lambda db: _list_recent_sessions(db, limit=limit))


@mcp.tool()
def pexo_get_session_activity(session_id: str, limit: int = 50) -> list[dict]:
    """Returns recent agent-state activity for a single orchestration session."""
    return _with_db(lambda db: _get_session_activity(db, session_id=session_id, limit=limit))


@mcp.tool()
def pexo_intake_prompt(prompt: str, user_id: str = "default_user", session_id: str | None = None) -> dict:
    """Starts the one-ask orchestration loop and returns the clarification question."""
    return _with_db(lambda db: intake_prompt(PromptRequest(user_id=user_id, prompt=prompt, session_id=session_id), db).model_dump())


@mcp.tool()
def pexo_start_task(prompt: str, user_id: str = "default_user", session_id: str | None = None) -> dict:
    """Starts the preferred simplified task flow. Use `user_message` for user-facing output and keep internal orchestration details hidden unless asked."""
    return _with_db(lambda db: start_simple_task(PromptRequest(user_id=user_id, prompt=prompt, session_id=session_id), db))


@mcp.tool()
def pexo_execute_plan(session_id: str, clarification_answer: str) -> dict:
    """Applies the clarification answer and starts graph execution."""
    return _with_db(lambda db: execute_plan(ExecuteRequest(session_id=session_id, clarification_answer=clarification_answer), db))


@mcp.tool()
def pexo_continue_task(
    session_id: str,
    clarification_answer: str | None = None,
    result_data: Any | None = None,
) -> dict:
    """Continues the preferred simplified task flow. Show `user_message` to the user; use `instruction` or `agent_instruction` internally when Pexo requests agent work."""
    return _with_db(
        lambda db: continue_simple_task(
            SimpleContinueRequest(
                session_id=session_id,
                clarification_answer=clarification_answer,
                result_data=result_data,
            ),
            db,
        )
    )


@mcp.tool()
def pexo_get_next_task(session_id: str) -> dict:
    """Returns the next pending orchestration instruction or session completion state."""
    return _with_db(lambda db: get_next_task(session_id=session_id, db=db))


@mcp.tool()
def pexo_get_task_status(session_id: str) -> dict:
    """Returns the current simplified task state for a session. Prefer `user_message` for user-facing replies."""
    return _with_db(lambda db: get_simple_task_status(session_id=session_id, db=db))


@mcp.tool()
def pexo_submit_task_result(session_id: str, result_data: Any) -> dict:
    """Submits a worker result back into the orchestration graph."""
    return _with_db(lambda db: submit_task_result(TaskResult(session_id=session_id, result_data=result_data), db))


@mcp.tool()
def pexo_run_backup() -> dict:
    """Backs up the local database, vector store, and Genesis tools using the configured profile path."""
    return _with_db(lambda db: run_backup_for_profile(db))


@mcp.tool()
def pexo_get_runtime_status() -> dict:
    """Returns the local runtime profile status and any recommended promotions."""
    return _with_db(lambda db: build_runtime_status_response(db))


@mcp.tool()
def pexo_promote_runtime(profile: str) -> dict:
    """Promotes the local runtime dependency profile in-place."""
    return _with_db(lambda db: promote_runtime_profile(profile, db))


@mcp.tool()
def pexo_list_artifacts(
    limit: int = 20,
    query: str | None = None,
    session_id: str | None = None,
    task_context: str | None = None,
) -> dict:
    """Lists stored local artifacts and supports lightweight text search."""
    return _with_db(
        lambda db: list_artifacts(limit=limit, query=query, session_id=session_id, task_context=task_context, db=db)
    )


@mcp.tool()
def pexo_get_artifact(artifact_id: int) -> dict:
    """Returns a single artifact and any extracted text preview."""
    return _with_db(lambda db: get_artifact(artifact_id, db))


@mcp.tool()
def pexo_register_artifact_text(
    name: str,
    content: str,
    session_id: str = "artifact_session",
    task_context: str = "general",
    source_uri: str | None = None,
    content_type: str = "text/plain",
) -> dict:
    """Stores a text artifact inside Pexo's local artifact vault."""
    return _with_db(
        lambda db: register_artifact_text(
            ArtifactTextRequest(
                name=name,
                content=content,
                session_id=session_id,
                task_context=task_context,
                source_uri=source_uri,
                content_type=content_type,
            ),
            db,
        )
    )


@mcp.tool()
def pexo_register_artifact_path(
    path: str,
    session_id: str = "artifact_session",
    task_context: str = "general",
    name: str | None = None,
) -> dict:
    """Copies a local file into Pexo's artifact vault and indexes any text preview."""
    return _with_db(
        lambda db: register_artifact_path(
            ArtifactPathRequest(
                path=path,
                session_id=session_id,
                task_context=task_context,
                name=name,
            ),
            db,
        )
    )


@mcp.tool()
def pexo_delete_artifact(artifact_id: int) -> dict:
    """Deletes an artifact from the local artifact vault."""
    return _with_db(lambda db: delete_artifact(artifact_id, db))


def start_mcp_server():
    """Starts the Pexo MCP server over stdio for native AI integration."""
    init_db()
    mcp.run(transport="stdio")
