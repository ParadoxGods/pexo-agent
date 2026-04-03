from typing import Any

from fastapi import HTTPException

from mcp.server.fastmcp import FastMCP

from .database import SessionLocal, init_db
from .models import AgentProfile, AgentState, Artifact, Profile
from .routers.admin import (
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
    store_memory,
    update_memory,
)
from .routers.orchestrator import ExecuteRequest, PromptRequest, TaskResult, execute_plan, get_next_task, intake_prompt, submit_task_result
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
def pexo_execute_plan(session_id: str, clarification_answer: str) -> dict:
    """Applies the clarification answer and starts graph execution."""
    return _with_db(lambda db: execute_plan(ExecuteRequest(session_id=session_id, clarification_answer=clarification_answer), db))


@mcp.tool()
def pexo_get_next_task(session_id: str) -> dict:
    """Returns the next pending orchestration instruction or session completion state."""
    return _with_db(lambda db: get_next_task(session_id=session_id, db=db))


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
