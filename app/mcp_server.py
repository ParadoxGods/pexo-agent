import json
import re
from typing import Any

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import func, or_

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
    build_memory_handoff_packet,
    delete_memory,
    get_memory,
    list_recent_memories,
    run_memory_maintenance,
    search_memory,
    serialize_memory,
    store_memory,
    store_memory_record,
    update_memory,
)
from .routers.orchestrator import (
    ClaimRequest,
    ExecuteRequest,
    PromptRequest,
    SimpleContinueRequest,
    TaskResult,
    claim_next_task,
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


def _summarize_handoff_activity(activity: list[dict], limit: int = 6) -> list[dict]:
    summarized = []
    for item in activity[: max(1, min(limit, 20))]:
        summarized.append(
            {
                "agent_name": item.get("agent_name"),
                "status": item.get("status"),
                "task_id": item.get("task_id"),
                "task_description": _truncate(item.get("task_description"), 140),
                "output_preview": _truncate(item.get("output_preview"), 160),
                "created_at": item.get("created_at"),
            }
        )
    return summarized


def _build_handoff_packet(
    db,
    *,
    session_id: str,
    task_context: str | None = None,
    memory_limit: int = 5,
    artifact_limit: int = 5,
) -> dict:
    task_status = get_simple_task_status(session_id=session_id, db=db)
    activity = _get_session_activity(db, session_id=session_id, limit=20)
    memory_packet = build_memory_handoff_packet(
        db,
        session_id=session_id,
        task_context=task_context,
        limit=memory_limit,
    )
    scoped_task_context = task_context if task_context and task_context != "general" else None
    artifact_payload = list_artifacts(
        limit=max(1, min(artifact_limit, 20)),
        query=None,
        session_id=session_id,
        task_context=scoped_task_context,
        db=db,
    )
    artifacts = _compact_artifact_lookup_results(
        artifact_payload,
        query=session_id,
        session_id=session_id,
        task_context=task_context,
    )
    key_memories = [_compact_memory_result(memory) for memory in memory_packet.get("memories", [])]
    next_action = task_status.get("next_action") or "reply_to_user"
    return {
        "status": "success",
        "session_id": session_id,
        "task_context": task_context,
        "task": {
            "status": task_status.get("status"),
            "role": task_status.get("role"),
            "user_message": task_status.get("user_message"),
            "question": task_status.get("question"),
            "instruction": task_status.get("instruction"),
            "agent_instruction": task_status.get("agent_instruction"),
            "final_response": task_status.get("final_response"),
            "next_action": next_action,
        },
        "handoff_summary": {
            "recent_activity": _summarize_handoff_activity(activity),
            "key_memories": key_memories,
            "artifacts": artifacts,
        },
        "user_message": (
            "Pexo built a handoff packet for the next client."
            if activity or key_memories or artifacts
            else "Pexo found little prior session state, but built a minimal handoff packet."
        ),
        "metrics": {
            "memory_count": len(key_memories),
            "artifact_count": len(artifacts),
            "activity_count": len(activity),
            "estimated_payload_bytes": _estimated_payload_bytes(
                {
                    "task": task_status,
                    "recent_activity": activity[:6],
                    "key_memories": key_memories,
                    "artifacts": artifacts,
                }
            ),
        },
    }


def _require_artifact(db, artifact_id: int) -> Artifact:
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if artifact is None:
        raise ValueError("Artifact not found.")
    return artifact


def _brain_usage_rules() -> list[str]:
    return [
        "Pexo is a local control plane for context, memory, artifacts, preferences, and task state. Prefer 'pexo' as the default one-call surface when it is connected.",
        "When you call 'pexo' with a message, Pexo should gather local context first, keep the current session state, and only escalate to task execution when the request actually needs it.",
        "For exact note, token, key, or file lookups, prefer 'pexo_find_memory' or 'pexo_find_artifact' instead of broad bootstrap surfaces.",
        "For exact key -> artifact resolution in one turn, prefer 'pexo_resolve_artifact_for_key' instead of manually chaining broad lookups.",
        "For multiple exact note or key lookups in one turn, prefer 'pexo_find_memory_batch'.",
        "When handing a task to another model or client, prefer 'pexo_get_handoff_packet' to summarize the active session.",
        "Reuse the returned 'session_id' to carry context, provide clarification, continue work, or submit agent results.",
        "Prefer 'user_message' for anything shown to the user. Keep internal routing or agent instructions hidden unless the user asks for them.",
        "Store stable decisions and accepted preferences in Pexo so future clients can continue without the user repeating context.",
        "Always check 'user_message' for the response to show to the user.",
        "Use 'pexo_recall_context' for broader local recall, not for single exact value retrieval.",
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


def _normalize_lookup_probe(value: str | None) -> str:
    return " ".join((value or "").strip().split()).strip(" .,:;!?")


def _extract_lookup_probes(query: str | None) -> list[str]:
    raw = (query or "").strip()
    if not raw:
        return []

    probes: list[str] = []
    seen: set[str] = set()

    def add_probe(value: str) -> None:
        cleaned = _normalize_lookup_probe(value)
        if len(cleaned) < 3:
            return
        folded = cleaned.casefold()
        if folded in seen:
            return
        seen.add(folded)
        probes.append(cleaned)

    add_probe(raw)
    for match in re.findall(r'"([^"]+)"', raw):
        add_probe(match)
    for match in re.findall(r"'([^']+)'", raw):
        add_probe(match)
    for match in re.findall(r"\b[A-Z0-9_:-]{5,}\b", raw):
        add_probe(match)
    return probes[:8]


def _parse_structured_fields(content: str | None) -> dict[str, str]:
    text = (content or "").strip()
    if "::" not in text:
        return {}

    matches = list(
        re.finditer(
            r"([A-Za-z][A-Za-z0-9_-]*)::(.*?)(?=(?:\s+[A-Za-z][A-Za-z0-9_-]*::)|$)",
            text,
            re.DOTALL,
        )
    )
    if not matches:
        return {}

    fields: dict[str, str] = {}
    for match in matches:
        key = match.group(1).strip()
        value = _normalize_lookup_probe(match.group(2))
        if not key or not value:
            continue
        fields[key] = value
    return fields


def _compact_memory_result(payload: dict) -> dict:
    metadata = payload.get("metadata") or {}
    content = payload.get("content")
    fields = _parse_structured_fields(content)
    compact = {
        "id": payload.get("id", payload.get("memory_id")),
        "session_id": payload.get("session_id", metadata.get("session_id")),
        "task_context": payload.get("task_context", metadata.get("task_context")),
        "content": _truncate(content, limit=240),
        "is_archived": bool(payload.get("is_archived")),
        "is_pinned": bool(payload.get("is_pinned")),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }
    if fields:
        compact["fields"] = fields
        for key in ("lookup_key", "value", "artifact_token", "file", "path"):
            if key in fields:
                compact[key] = fields[key]
    return compact


def _compact_artifact_result(payload: dict) -> dict:
    compact = {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "lookup_token": payload.get("lookup_token"),
        "canonical_name": payload.get("canonical_name"),
        "session_id": payload.get("session_id"),
        "task_context": payload.get("task_context"),
        "source_type": payload.get("source_type"),
        "source_uri": payload.get("source_uri"),
        "preview": _truncate(payload.get("preview"), limit=240),
        "has_text": bool(payload.get("has_text")),
    }
    fields = {}
    if payload.get("lookup_token"):
        fields["lookup_token"] = payload.get("lookup_token")
    if payload.get("canonical_name"):
        fields["canonical_name"] = payload.get("canonical_name")
    if fields:
        compact["fields"] = fields
    return compact


def _exact_artifact_results(
    db,
    *,
    query: str,
    limit: int,
    session_id: str | None = None,
    task_context: str | None = None,
) -> list[dict]:
    probes = _extract_lookup_probes(query)
    if not probes:
        return []

    scoped_task_context = task_context if task_context and task_context != "general" else None

    def fetch(probe_session_id: str | None, probe_task_context: str | None) -> list[dict]:
        lower_probe_values = [probe.casefold() for probe in probes]
        artifact_query = db.query(Artifact)
        if probe_session_id:
            artifact_query = artifact_query.filter(Artifact.session_id == probe_session_id)
        if probe_task_context:
            artifact_query = artifact_query.filter(Artifact.task_context == probe_task_context)
        artifact_query = artifact_query.filter(
            or_(
                func.lower(func.coalesce(Artifact.lookup_token, "")).in_(lower_probe_values),
                func.lower(func.coalesce(Artifact.canonical_name, "")).in_(lower_probe_values),
                func.lower(func.coalesce(Artifact.name, "")).in_(lower_probe_values),
            )
        )
        recency_order = func.coalesce(Artifact.updated_at, Artifact.created_at)
        artifacts = artifact_query.order_by(recency_order.desc(), Artifact.id.desc()).limit(max(1, min(limit, 20))).all()
        return [serialize_artifact(artifact) for artifact in artifacts]

    exact = fetch(session_id, scoped_task_context)
    if exact or not (session_id or scoped_task_context):
        return exact
    return fetch(None, None)


def _select_scoped_results(
    results: list[dict],
    *,
    session_id: str | None = None,
    task_context: str | None = None,
) -> list[dict]:
    if not results:
        return []

    scoped_task_context = task_context if task_context and task_context != "general" else None
    if not session_id and not scoped_task_context:
        return results

    scoped = [
        item
        for item in results
        if (not session_id or item.get("session_id") == session_id)
        and (not scoped_task_context or item.get("task_context") == scoped_task_context)
    ]
    return scoped or results


def _dedupe_results(results: list[dict], *, fingerprint_keys: tuple[str, ...] = ("content",)) -> list[dict]:
    deduped: list[dict] = []
    seen: dict[tuple[str, ...], dict] = {}
    for item in results:
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        fingerprint_parts: list[str] = []
        for key in fingerprint_keys:
            if key == "fields":
                normalized_fields = "|".join(
                    f"{field_key}={fields[field_key]}"
                    for field_key in sorted(fields)
                )
                fingerprint_parts.append(normalized_fields)
            else:
                fingerprint_parts.append(_normalize_lookup_probe(str(item.get(key) or "")))
        fingerprint = tuple(fingerprint_parts)
        if not any(part for part in fingerprint):
            fingerprint = (_normalize_lookup_probe(str(item.get("id") or "")),)

        existing = seen.get(fingerprint)
        if existing is None:
            entry = dict(item)
            entry["duplicate_count"] = 0
            seen[fingerprint] = entry
            deduped.append(entry)
            continue

        existing["duplicate_count"] = int(existing.get("duplicate_count", 0)) + 1
    return deduped


def _score_result_against_query(result: dict, query: str) -> int:
    probes = _extract_lookup_probes(query)
    if not probes:
        return 0

    fields = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    haystacks = [
        _normalize_lookup_probe(result.get("content")),
        _normalize_lookup_probe(result.get("name")),
        _normalize_lookup_probe(result.get("source_uri")),
        _normalize_lookup_probe(result.get("preview")),
        _normalize_lookup_probe(result.get("session_id")),
        _normalize_lookup_probe(result.get("task_context")),
        _normalize_lookup_probe(result.get("lookup_token")),
        _normalize_lookup_probe(result.get("canonical_name")),
    ]
    haystacks.extend(_normalize_lookup_probe(str(value)) for value in fields.values())
    score = 0
    for probe in probes:
        folded_probe = probe.casefold()
        if not folded_probe:
            continue
        for haystack in haystacks:
            folded_haystack = haystack.casefold()
            if not folded_haystack:
                continue
            if folded_haystack == folded_probe:
                score += 50
            elif folded_probe in folded_haystack:
                score += 15
    if result.get("is_pinned"):
        score += 2
    return score


def _rank_results(results: list[dict], *, query: str) -> list[dict]:
    return sorted(
        results,
        key=lambda item: (
            _score_result_against_query(item, query),
            item.get("updated_at") or item.get("created_at") or "",
            item.get("id") or 0,
        ),
        reverse=True,
    )


def _estimated_payload_bytes(payload: Any) -> int:
    return len(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _build_retrieval_metrics(
    query: str,
    results: list[dict],
    *,
    session_id: str | None = None,
    task_context: str | None = None,
) -> dict:
    scoped_task_context = task_context if task_context and task_context != "general" else None
    duplicate_count = sum(int(item.get("duplicate_count", 0)) for item in results)
    exact_hit_count = sum(1 for item in results if _score_result_against_query(item, query) >= 50)
    scoped_hit_count = sum(
        1
        for item in results
        if (not session_id or item.get("session_id") == session_id)
        and (not scoped_task_context or item.get("task_context") == scoped_task_context)
    )
    return {
        "query_probe_count": len(_extract_lookup_probes(query)),
        "result_count": len(results),
        "duplicate_count": duplicate_count,
        "exact_hit_count": exact_hit_count,
        "scoped_hit_count": scoped_hit_count,
        "used_scope": bool(session_id or scoped_task_context),
        "estimated_payload_bytes": _estimated_payload_bytes(results),
    }


def _compact_memory_lookup_results(
    payload: dict,
    *,
    query: str,
    session_id: str | None = None,
    task_context: str | None = None,
) -> list[dict]:
    compact = [_compact_memory_result(item) for item in payload.get("results", [])]
    compact = _select_scoped_results(compact, session_id=session_id, task_context=task_context)
    compact = _dedupe_results(compact, fingerprint_keys=("fields", "content"))
    return _rank_results(compact, query=query)


def _compact_artifact_lookup_results(
    payload: dict,
    *,
    query: str,
    session_id: str | None = None,
    task_context: str | None = None,
) -> list[dict]:
    compact = [_compact_artifact_result(item) for item in payload.get("artifacts", [])]
    compact = _select_scoped_results(compact, session_id=session_id, task_context=task_context)
    compact = _dedupe_results(compact, fingerprint_keys=("name", "source_uri", "preview"))
    return _rank_results(compact, query=query)


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


def _infer_lookup_targets(query: str) -> tuple[bool, bool]:
    normalized = _normalize_exchange_message(query)
    if not normalized:
        return True, True

    memory_markers = ("memory", "memories", "note", "notes", "preference", "preferences", "session", "sessions")
    artifact_markers = ("artifact", "artifacts", "file", "files", "readme", "path", "paths", "document", "documents")

    wants_memory = any(marker in normalized for marker in memory_markers)
    wants_artifacts = any(marker in normalized for marker in artifact_markers)
    if wants_memory and not wants_artifacts:
        return True, False
    if wants_artifacts and not wants_memory:
        return False, True
    return True, True


def _build_compact_lookup_payload(
    db,
    *,
    query: str,
    memory_results: int,
    artifact_results: int,
    session_id: str | None = None,
    task_context: str | None = None,
    auto_promote_vector: bool = False,
) -> dict:
    include_memory, include_artifacts = _infer_lookup_targets(query)
    memory_payload = (
        search_memory(
            MemorySearchRequest(
                query=query,
                n_results=max(1, min(memory_results, 10)),
                session_id=session_id,
                task_context=task_context,
                auto_promote_vector=auto_promote_vector,
            ),
            db,
        )
        if include_memory
        else {"results": []}
    )
    if include_memory and not memory_payload.get("results") and (session_id or (task_context and task_context != "general")):
        memory_payload = search_memory(
            MemorySearchRequest(
                query=query,
                n_results=max(1, min(memory_results, 10)),
                auto_promote_vector=auto_promote_vector,
            ),
            db,
        )
    artifact_payload = {"artifacts": []}
    if include_artifacts:
        scoped_task_context = task_context if task_context and task_context != "general" else None
        artifact_payload = list_artifacts(
            limit=max(1, min(artifact_results, 10)),
            query=query,
            session_id=session_id,
            task_context=scoped_task_context,
            db=db,
        )
        if not artifact_payload.get("artifacts") and (session_id or scoped_task_context):
            artifact_payload = list_artifacts(
                limit=max(1, min(artifact_results, 10)),
                query=query,
                session_id=None,
                task_context=None,
                db=db,
            )
    memory_results_payload = _compact_memory_lookup_results(
        memory_payload,
        query=query,
        session_id=session_id,
        task_context=task_context,
    )
    artifact_results_payload = _compact_artifact_lookup_results(
        artifact_payload,
        query=query,
        session_id=session_id,
        task_context=task_context,
    )
    return {
        "memory": {
            "query": query,
            "results": memory_results_payload,
            "metrics": _build_retrieval_metrics(
                query,
                memory_results_payload,
                session_id=session_id,
                task_context=task_context,
            ),
        },
        "artifacts": {
            "query": query,
            "results": artifact_results_payload,
            "metrics": _build_retrieval_metrics(
                query,
                artifact_results_payload,
                session_id=session_id,
                task_context=task_context,
            ),
        },
    }


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
    compact: bool = False,
    memory_results: int = 4,
    artifact_results: int = 4,
    auto_promote_vector: bool = False,
) -> dict:
    effective_query = query
    effective_remember = remember
    if effective_remember is None:
        effective_remember = _extract_inline_memory_message(message)
    if effective_query is None and _looks_like_lookup_only_message(message):
        effective_query = (message or "").strip()

    if attach_text and not attach_name:
        attach_text_name = "context.txt"
    else:
        attach_text_name = attach_name

    if (
        message is None
        and session_id is None
        and effective_query is None
        and effective_remember is None
        and attach_path is None
        and attach_text is None
        and not include_brain
    ):
        raise ValueError(
            "Provide a message to start a task, a session_id to continue one, or context to store/recall."
        )

    if session_id is None and agent_result is not None:
        raise ValueError("agent_result requires an existing session_id.")

    writes: dict[str, Any] = {}
    if effective_remember:
        stored_memory = store_memory_record(
            MemoryStoreRequest(
                session_id=session_id or "brain_session",
                content=effective_remember,
                task_context=task_context,
                auto_promote_vector=auto_promote_vector,
            ),
            db=db,
            background_tasks=BackgroundTasks(),
        )
        memory_id = stored_memory.get("memory_id")
        writes["memory"] = _compact_memory_result(get_memory(memory_id, db)) if memory_id else None

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
    has_task_intent = (
        message is not None
        and effective_query is None
        and not _looks_like_storage_only_message(
            message,
            remember=effective_remember,
            attach_path=attach_path,
            attach_text=attach_text,
            query=effective_query,
        )
    )
    if session_id and (has_task_intent or agent_result is not None):
        try:
            current_status = get_simple_task_status(session_id=session_id, db=db)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            current_status = None
        if message is not None and agent_result is not None:
            notice = "Pexo ignored the user message because agent_result was provided for this session."
            task_payload = continue_simple_task(
                SimpleContinueRequest(session_id=session_id, result_data=agent_result),
                db,
            )
        elif message is not None:
            if current_status is None:
                task_payload = start_simple_task(
                    PromptRequest(user_id=user_id, prompt=message, session_id=session_id),
                    db,
                )
            elif current_status.get("status") == "clarification_required":
                task_payload = continue_simple_task(
                    SimpleContinueRequest(session_id=session_id, clarification_answer=message),
                    db,
                )
            else:
                task_payload = current_status
                notice = "Pexo did not use the message because this session is not waiting for user clarification."
        elif agent_result is not None:
            if current_status is None:
                raise ValueError("agent_result requires an existing session waiting for agent work.")
            if current_status.get("status") == "agent_action_required":
                task_payload = continue_simple_task(
                    SimpleContinueRequest(session_id=session_id, result_data=agent_result),
                    db,
                )
            else:
                task_payload = current_status
                notice = "Pexo did not use agent_result because this session is not waiting for agent work."
    elif has_task_intent:
        task_payload = start_simple_task(
            PromptRequest(user_id=user_id, prompt=message, session_id=None),
            db,
        )

    recall_query = (effective_query or effective_remember or "").strip()
    brain = None
    lookup = None
    if include_brain:
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
    elif recall_query and compact:
        lookup = _build_compact_lookup_payload(
            db,
            query=recall_query,
            memory_results=memory_results,
            artifact_results=artifact_results,
            session_id=session_id,
            task_context=task_context,
            auto_promote_vector=auto_promote_vector,
        )
    elif recall_query and not include_brain and not has_task_intent:
        lookup = _build_compact_lookup_payload(
            db,
            query=recall_query,
            memory_results=memory_results,
            artifact_results=artifact_results,
            session_id=session_id,
            task_context=task_context,
            auto_promote_vector=auto_promote_vector,
        )
    elif effective_query is not None or (message is not None and session_id is None):
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
    if lookup is not None:
        response.update(lookup)
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
        if _should_bootstrap_start_task(prompt)
        else None
    )

    memory_items = memory_payload.get("results") if recall_query else memory_payload.get("memories", [])
    return {
        "mode": "brain",
        "user_message": "Pexo is ready. Use the returned context and simple task flow.",
        "operating_contract": _brain_usage_rules(),
        "profile": _summarize_profile(profile_payload, profile_answers),
        "runtime": {
            "active_profile": runtime.get("active_profile"),
            "memory_backend": runtime.get("memory_backend"),
            "semantic_memory_ready": runtime.get("semantic_memory_ready"),
            "install_mode": runtime.get("install_mode"),
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


def _coerce_task_result_payload(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _normalize_exchange_message(text: str | None) -> str:
    return " ".join((text or "").strip().lower().split())


def _extract_inline_memory_message(text: str | None) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None

    quoted_match = re.search(
        r"""(?is)\b(?:store|remember|save)\b.*?\b(?:memory|note|context)\b.*?["'](?P<content>.+?)["']\s*[.!?]?\s*$""",
        raw,
    )
    if quoted_match:
        return quoted_match.group("content").strip()

    colon_match = re.search(
        r"""(?is)^\s*(?:store|remember|save)\b.*?\b(?:memory|note|context)\b\s*:?\s*(?P<content>.+?)\s*$""",
        raw,
    )
    if colon_match:
        candidate = colon_match.group("content").strip().strip("\"'")
        candidate = re.sub(r"""(?is)^in\s+pexo\s*:?\s*""", "", candidate).strip()
        is_exact_store = re.search(r"""(?is)\bexact\s+(?:memory|note|context)\b""", raw) is not None
        if is_exact_store or len(candidate.split()) >= 3:
            return candidate
    return None


def _looks_like_lookup_only_message(message: str | None, *, query: str | None = None) -> bool:
    if query is not None:
        return True

    normalized = _normalize_exchange_message(message)
    if not normalized:
        return False

    lookup_prefixes = (
        "find ",
        "search ",
        "look up ",
        "recall ",
        "tell me ",
        "show me ",
        "read ",
        "list ",
        "what do you know",
        "do we have ",
    )
    lookup_markers = (
        "memory",
        "memories",
        "artifact",
        "artifacts",
        "context",
        "readme",
        "note",
        "notes",
        "profile",
        "preferences",
        "session",
        "sessions",
    )
    return any(normalized.startswith(prefix) for prefix in lookup_prefixes) and any(
        marker in normalized for marker in lookup_markers
    )


def _looks_like_storage_only_message(
    message: str | None,
    *,
    remember: str | None = None,
    attach_path: str | None = None,
    attach_text: str | None = None,
    query: str | None = None,
) -> bool:
    normalized = _normalize_exchange_message(message)
    if not normalized:
        return False
    if query is not None:
        return True
    if remember is None and attach_path is None and attach_text is None and _extract_inline_memory_message(message) is None:
        return False

    storage_prefixes = ("store ", "remember ", "save ", "attach ", "add ")
    storage_markers = ("memory", "context", "artifact", "file", "note", "text", "readme", "path")
    if any(normalized.startswith(prefix) for prefix in storage_prefixes) and any(marker in normalized for marker in storage_markers):
        return True
    if "exact memory" in normalized or "exact note" in normalized:
        return True
    if "store this" in normalized or "attach this" in normalized:
        return True
    return False


def _should_bootstrap_start_task(prompt: str | None) -> bool:
    normalized = _normalize_exchange_message(prompt)
    if not normalized:
        return False

    non_task_prefixes = (
        "summarize ",
        "tell me ",
        "show me ",
        "what is ",
        "what's ",
        "list ",
        "read ",
        "find ",
        "search ",
        "look up ",
        "recall ",
    )
    if any(normalized.startswith(prefix) for prefix in non_task_prefixes):
        return False
    if _looks_like_lookup_only_message(prompt):
        return False
    if _looks_like_storage_only_message(prompt):
        return False

    task_verbs = (
        "build ",
        "fix ",
        "implement ",
        "review ",
        "design ",
        "create ",
        "write ",
        "generate ",
        "refactor ",
        "debug ",
        "plan ",
    )
    return any(normalized.startswith(verb) for verb in task_verbs)


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
        "2. For exact stored note or file lookups, prefer `pexo_find_memory`, `pexo_find_artifact`, or `pexo_find_memory_batch` instead of `pexo_bootstrap_brain`.\n"
        "3. Reuse the returned `session_id` with `pexo` for clarification answers or agent results.\n"
        "4. Use `user_message` for user-facing replies.\n"
        "5. Keep `agent_instruction` internal unless the user explicitly asks for orchestration details.\n"
        "6. Use `pexo_recall_context` before asking the user to repeat context when you need broader local recall.\n"
        "7. Use `pexo_get_handoff_packet` when another client needs to continue a live session.\n"
        "8. Persist useful notes with `pexo_remember_context` and files with `pexo_attach_context`, or fold them into `pexo`.\n\n"
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
                "If the user wants one exact stored note, token, or file, prefer pexo_find_memory or pexo_find_artifact instead of broad bootstrap. "
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
    """Primary one-call control-plane surface. Use it to route a task through Pexo while keeping memory, artifacts, preferences, and session state together."""

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
            compact=True,
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
    """Unified control-plane surface. Start or continue work, recall local context, and persist memories or artifacts in one call."""
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
            compact=False,
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
    """Broad state/bootstrap surface. Use this for orientation or task kickoff, not for single exact note/file retrieval."""
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
    session_id: str | None = None,
    task_context: str | None = None,
    auto_promote_vector: bool = False,
) -> dict:
    """Broader local recall surface. Use this when you need memory plus artifact context, not when a single exact lookup will do."""

    def operation(db):
        lookup = _build_compact_lookup_payload(
            db,
            query=query,
            memory_results=memory_results,
            artifact_results=artifact_results,
            session_id=session_id,
            task_context=task_context,
            auto_promote_vector=auto_promote_vector,
        )
        return {
            "user_message": f"Pexo found context for '{query}'.",
            "query": query,
            "memory": lookup["memory"],
            "artifacts": lookup["artifacts"],
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
        stored = store_memory_record(
            MemoryStoreRequest(
                session_id=session_id,
                content=content,
                task_context=task_context,
                auto_promote_vector=auto_promote_vector,
            ),
            db=db,
            background_tasks=BackgroundTasks(),
        )
        memory_id = stored.get("memory_id")
        memory_payload = get_memory(memory_id, db) if memory_id else None
        return {
            "status": "success",
            "user_message": "Pexo stored the context for future tasks.",
            "memory": _compact_memory_result(memory_payload) if memory_payload else None,
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
def pexo_find_memory(
    query: str,
    limit: int = 3,
    session_id: str | None = None,
    task_context: str | None = None,
    auto_promote_vector: bool = False,
) -> dict:
    """Exact/narrow memory lookup surface. Prefer this over bootstrap for specific stored notes, tokens, keys, or values. Pass both session_id and task_context whenever you know them; omitting scope widens the search and can increase payload size."""

    def operation(db):
        payload = search_memory(
            MemorySearchRequest(
                query=query,
                n_results=max(1, min(limit, 10)),
                session_id=session_id,
                task_context=task_context,
                auto_promote_vector=auto_promote_vector,
            ),
            db,
        )
        results = _compact_memory_lookup_results(
            payload,
            query=query,
            session_id=session_id,
            task_context=task_context,
        )
        if not results and (session_id or (task_context and task_context != "general")):
            payload = search_memory(
                MemorySearchRequest(
                    query=query,
                    n_results=max(1, min(limit, 10)),
                    auto_promote_vector=auto_promote_vector,
                ),
                db,
            )
            results = _compact_memory_lookup_results(payload, query=query)
        return {
            "status": "success",
            "user_message": f"Pexo searched memory for '{query}'.",
            "query": query,
            "results": results,
            "best_match": results[0] if results else None,
            "metrics": _build_retrieval_metrics(
                query,
                results,
                session_id=session_id,
                task_context=task_context,
            ),
        }

    return _with_db(operation)


@mcp.tool()
def pexo_find_memory_batch(
    queries: list[str],
    limit_per_query: int = 1,
    session_id: str | None = None,
    task_context: str | None = None,
    auto_promote_vector: bool = False,
) -> dict:
    """Batch exact memory lookup surface. Use this when the user wants multiple exact stored values in one turn. Pass both session_id and task_context whenever you know them; omitting scope widens the search and can increase payload size."""

    def operation(db):
        request_queries = [query.strip() for query in queries if (query or "").strip()]
        if not request_queries:
            raise ValueError("Provide at least one non-empty query.")
        items = []
        for query in request_queries[:20]:
            payload = search_memory(
                MemorySearchRequest(
                    query=query,
                    n_results=max(1, min(limit_per_query, 10)),
                    session_id=session_id,
                    task_context=task_context,
                    auto_promote_vector=auto_promote_vector,
                ),
                db,
            )
            results = _compact_memory_lookup_results(
                payload,
                query=query,
                session_id=session_id,
                task_context=task_context,
            )
            if not results and (session_id or (task_context and task_context != "general")):
                payload = search_memory(
                    MemorySearchRequest(
                        query=query,
                        n_results=max(1, min(limit_per_query, 10)),
                        auto_promote_vector=auto_promote_vector,
                    ),
                    db,
                )
                results = _compact_memory_lookup_results(payload, query=query)
            items.append(
                {
                    "query": query,
                    "results": results,
                    "best_match": results[0] if results else None,
                    "metrics": _build_retrieval_metrics(
                        query,
                        results,
                        session_id=session_id,
                        task_context=task_context,
                    ),
                }
            )
        return {
            "status": "success",
            "user_message": f"Pexo searched memory for {len(items)} exact lookup request(s).",
            "queries": request_queries[:20],
            "items": items,
        }

    return _with_db(operation)


@mcp.tool()
def pexo_store_memory(
    content: str,
    task_context: str,
    session_id: str = "mcp_session",
    auto_promote_vector: bool = False,
) -> dict:
    """Stores a memory record and triggers lifecycle maintenance."""
    return _with_db(
        lambda db: store_memory_record(
            MemoryStoreRequest(
                session_id=session_id,
                content=content,
                task_context=task_context,
                auto_promote_vector=auto_promote_vector,
            ),
            db=db,
            background_tasks=BackgroundTasks(),
        )
    )


@mcp.tool()
def pexo_list_recent_memories(limit: int = 12, include_archived: bool = True) -> dict:
    """Lists recent memories, ordered by last update/creation time."""
    return _with_db(
        lambda db: {
            "memories": [
                _compact_memory_result(memory)
                for memory in list_recent_memories(limit=limit, include_archived=include_archived, db=db).get("memories", [])
            ]
        }
    )


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
def pexo_get_handoff_packet(
    session_id: str,
    task_context: str | None = None,
    memory_limit: int = 5,
    artifact_limit: int = 5,
) -> dict:
    """Builds a compact packet for handing an active session to another model or client."""
    return _with_db(
        lambda db: _build_handoff_packet(
            db,
            session_id=session_id,
            task_context=task_context,
            memory_limit=memory_limit,
            artifact_limit=artifact_limit,
        )
    )


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
    message: str | None = None,
) -> dict:
    """Continues the preferred simplified task flow. If you are unsure whether Pexo wants clarification or an agent result, send plain `message` and Pexo will route it based on the current session state."""

    def operation(db):
        if message is not None:
            if clarification_answer is not None or result_data is not None:
                raise ValueError("Provide either message or explicit continuation fields, not both.")
            status_payload = get_simple_task_status(session_id=session_id, db=db)
            status = status_payload.get("status")
            if status == "clarification_required":
                resolved_clarification = message
                resolved_result = None
            elif status == "agent_action_required":
                resolved_clarification = None
                resolved_result = _coerce_task_result_payload(message)
            else:
                return status_payload
        else:
            resolved_clarification = clarification_answer
            resolved_result = _coerce_task_result_payload(result_data)

        return continue_simple_task(
            SimpleContinueRequest(
                session_id=session_id,
                clarification_answer=resolved_clarification,
                result_data=resolved_result,
            ),
            db,
        )

    return _with_db(operation)


@mcp.tool()
def pexo_get_next_task(session_id: str) -> dict:
    """Returns the next pending orchestration instruction or session completion state."""
    return _with_db(lambda db: get_next_task(session_id=session_id, db=db))


@mcp.tool()
def pexo_claim_next_task(session_id: str, task_id: str | None = None) -> dict:
    """Explicitly claims the current pending task so active-task tracking stays off the read-only poll path."""
    return _with_db(lambda db: claim_next_task(ClaimRequest(session_id=session_id, task_id=task_id), db=db))


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
def pexo_find_artifact(
    query: str,
    limit: int = 5,
    session_id: str | None = None,
    task_context: str | None = None,
) -> dict:
    """Exact/narrow artifact lookup surface. Prefer this over bootstrap for specific files, tokens, paths, or stored artifacts. Pass both session_id and task_context whenever you know them; omitting scope widens the search and can increase payload size."""

    def operation(db):
        scoped_task_context = task_context if task_context and task_context != "general" else None
        payload = {
            "artifacts": _exact_artifact_results(
                db,
                query=query,
                limit=max(1, min(limit, 20)),
                session_id=session_id,
                task_context=scoped_task_context,
            )
        }
        if not payload.get("artifacts"):
            payload = list_artifacts(
                limit=max(1, min(limit, 20)),
                query=query,
                session_id=session_id,
                task_context=scoped_task_context,
                db=db,
            )
            if not payload.get("artifacts") and (session_id or scoped_task_context):
                payload = list_artifacts(
                    limit=max(1, min(limit, 20)),
                    query=query,
                    session_id=None,
                    task_context=None,
                    db=db,
                )
        results = _compact_artifact_lookup_results(
            payload,
            query=query,
            session_id=session_id,
            task_context=task_context,
        )
        return {
            "status": "success",
            "user_message": f"Pexo searched artifacts for '{query}'.",
            "query": query,
            "results": results,
            "best_match": results[0] if results else None,
            "metrics": _build_retrieval_metrics(
                query,
                results,
                session_id=session_id,
                task_context=task_context,
            ),
        }

    return _with_db(operation)


@mcp.tool()
def pexo_find_artifact_batch(
    queries: list[str],
    limit_per_query: int = 1,
    session_id: str | None = None,
    task_context: str | None = None,
) -> dict:
    """Batch exact artifact lookup surface. Use this when the user wants multiple exact files, tokens, or paths in one turn. Pass both session_id and task_context whenever you know them; omitting scope widens the search and can increase payload size."""

    def operation(db):
        request_queries = [query.strip() for query in queries if (query or "").strip()]
        if not request_queries:
            raise ValueError("Provide at least one non-empty query.")

        items = []
        for query in request_queries[:20]:
            scoped_task_context = task_context if task_context and task_context != "general" else None
            payload = {
                "artifacts": _exact_artifact_results(
                    db,
                    query=query,
                    limit=max(1, min(limit_per_query, 20)),
                    session_id=session_id,
                    task_context=scoped_task_context,
                )
            }
            if not payload.get("artifacts"):
                payload = list_artifacts(
                    limit=max(1, min(limit_per_query, 20)),
                    query=query,
                    session_id=session_id,
                    task_context=scoped_task_context,
                    db=db,
                )
            results = _compact_artifact_lookup_results(
                payload,
                query=query,
                session_id=session_id,
                task_context=task_context,
            )
            if not results and (session_id or scoped_task_context):
                payload = list_artifacts(
                    limit=max(1, min(limit_per_query, 20)),
                    query=query,
                    session_id=None,
                    task_context=None,
                    db=db,
                )
                results = _compact_artifact_lookup_results(payload, query=query)
            items.append(
                {
                    "query": query,
                    "results": results,
                    "best_match": results[0] if results else None,
                    "metrics": _build_retrieval_metrics(
                        query,
                        results,
                        session_id=session_id,
                        task_context=task_context,
                    ),
                }
            )

        return {
            "status": "success",
            "user_message": f"Pexo searched artifacts for {len(items)} exact lookup request(s).",
            "queries": request_queries[:20],
            "items": items,
        }

    return _with_db(operation)


@mcp.tool()
def pexo_resolve_artifact_for_key(
    key: str,
    session_id: str | None = None,
    task_context: str | None = None,
) -> dict:
    """One-call exact key-to-artifact resolver. Use this when a key maps to an artifact token in memory and you want the final artifact basename without manually chaining multiple broad retrieval calls. Pass both session_id and task_context whenever you know them."""

    def operation(db):
        memory_payload = search_memory(
            MemorySearchRequest(
                query=key,
                n_results=5,
                session_id=session_id,
                task_context=task_context,
                auto_promote_vector=False,
            ),
            db,
        )
        memory_results = _compact_memory_lookup_results(
            memory_payload,
            query=key,
            session_id=session_id,
            task_context=task_context,
        )
        if not memory_results and (session_id or (task_context and task_context != "general")):
            memory_payload = search_memory(
                MemorySearchRequest(
                    query=key,
                    n_results=5,
                    auto_promote_vector=False,
                ),
                db,
            )
            memory_results = _compact_memory_lookup_results(memory_payload, query=key)

        memory_match = next((item for item in memory_results if item.get("artifact_token")), None)
        artifact_token = memory_match.get("artifact_token") if memory_match else None
        artifact_results: list[dict] = []
        if artifact_token:
            artifact_payload = {
                "artifacts": _exact_artifact_results(
                    db,
                    query=artifact_token,
                    limit=5,
                    session_id=session_id,
                    task_context=task_context,
                )
            }
            if not artifact_payload.get("artifacts"):
                scoped_task_context = task_context if task_context and task_context != "general" else None
                artifact_payload = list_artifacts(
                    limit=5,
                    query=artifact_token,
                    session_id=session_id,
                    task_context=scoped_task_context,
                    db=db,
                )
                if not artifact_payload.get("artifacts") and (session_id or scoped_task_context):
                    artifact_payload = list_artifacts(
                        limit=5,
                        query=artifact_token,
                        session_id=None,
                        task_context=None,
                        db=db,
                    )
            artifact_results = _compact_artifact_lookup_results(
                artifact_payload,
                query=artifact_token,
                session_id=session_id,
                task_context=task_context,
            )

        best_artifact = artifact_results[0] if artifact_results else None
        return {
            "status": "success",
            "user_message": f"Pexo resolved artifact context for key '{key}'.",
            "key": key,
            "artifact_token": artifact_token,
            "memory_match": memory_match,
            "artifact_match": best_artifact,
            "artifact_results": artifact_results,
            "metrics": {
                "memory_exact_hit_count": _build_retrieval_metrics(
                    key,
                    memory_results,
                    session_id=session_id,
                    task_context=task_context,
                )["exact_hit_count"],
                "artifact_exact_hit_count": _build_retrieval_metrics(
                    artifact_token or "",
                    artifact_results,
                    session_id=session_id,
                    task_context=task_context,
                )["exact_hit_count"] if artifact_token else 0,
                "estimated_payload_bytes": _estimated_payload_bytes(
                    {
                        "memory_match": memory_match,
                        "artifact_token": artifact_token,
                        "artifact_match": best_artifact,
                    }
                ),
            },
        }

    return _with_db(operation)


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
