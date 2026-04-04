from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..cache import cached_value, invalidate_many
from ..client_connect import connect_clients
from ..database import get_db
from ..models import AgentProfile, AgentState, Artifact, DynamicTool, Memory, Profile
from ..runtime import build_runtime_status
from .artifacts import serialize_artifact
from .memory import serialize_memory
from .profile import derive_profile_answers

router = APIRouter()

STATUS_LABELS = {
    "clarification_pending": "Needs Clarification",
    "graph_started": "Planning Started",
    "running": "Running",
    "completed": "Completed",
    "session_complete": "Complete",
    "processing": "Processing",
    "pending_action": "Waiting On Agent",
    "error": "Error",
}

STATUS_TONES = {
    "clarification_pending": "warn",
    "graph_started": "warn",
    "running": "warn",
    "completed": "success",
    "session_complete": "success",
    "error": "danger",
}


def _truncate(value: str | None, limit: int = 96) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _status_label(status: str | None) -> str:
    normalized = (status or "").strip().lower()
    return STATUS_LABELS.get(normalized, normalized.replace("_", " ").title() or "Unknown")


def _status_tone(status: str | None) -> str:
    return STATUS_TONES.get((status or "").strip().lower(), "")


def _agent_label(agent_name: str | None) -> str:
    if not agent_name:
        return "Unknown"
    if agent_name == "orchestrator":
        return "Orchestrator"
    return agent_name


def _session_title_from_state(state: AgentState) -> tuple[int, str | None]:
    data = state.data or {}
    for priority, candidate in (
        (3, data.get("user_prompt")),
        (2, data.get("task_description")),
        (1, data.get("current_instruction")),
    ):
        title = _truncate(candidate, 88)
        if title:
            return priority, title
    return 0, None


def _session_summary_from_state(state: AgentState) -> str | None:
    data = state.data or {}
    if state.status == "clarification_pending":
        return "Waiting for one clarification before work begins."
    if state.status == "graph_started":
        next_agent = data.get("next_agent")
        if next_agent:
            return f"Task graph started. Next role: {_agent_label(str(next_agent))}."
        return "Task graph started."
    if state.status == "session_complete":
        return _truncate(data.get("final_response"), 110) or "Session completed."
    for candidate in (
        data.get("task_description"),
        data.get("output_preview"),
        data.get("clarification_answer"),
        data.get("result_type"),
    ):
        summary = _truncate(candidate, 110)
        if summary:
            return summary
    return None


def build_client_surface(scope: str = "user") -> dict:
    return cached_value(
        "client_surface",
        scope,
        10.0,
        lambda: connect_clients(target="all", scope=scope, dry_run=True, verify_existing=False),
    )


def serialize_profile(profile: Profile | None) -> dict | None:
    if profile is None:
        return None
    return {
        "id": profile.id,
        "name": profile.name,
        "personality_prompt": profile.personality_prompt,
        "scripting_preferences": profile.scripting_preferences or {},
        "backup_path": profile.backup_path,
    }


def serialize_agent(agent: AgentProfile) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "role": agent.role,
        "system_prompt": agent.system_prompt,
        "capabilities": list(agent.capabilities or []),
        "is_core": bool(agent.is_core),
    }


def serialize_agent_state(state: AgentState) -> dict:
    data = state.data or {}
    output_preview = data.get("output_preview")
    if output_preview is None and "output" in data:
        raw_output = data["output"]
        output_preview = str(raw_output)
        if len(output_preview) > 220:
            output_preview = f"{output_preview[:220].rstrip()}..."
    return {
        "id": state.id,
        "session_id": state.session_id,
        "agent_name": state.agent_name,
        "status": state.status,
        "context_size_tokens": state.context_size_tokens,
        "created_at": state.created_at.isoformat() if state.created_at else None,
        "task_id": data.get("task_id"),
        "task_description": data.get("task_description"),
        "output_preview": output_preview,
        "result_type": data.get("result_type"),
        "task_count": data.get("task_count"),
    }


def _build_telemetry_payload(db: Session) -> dict:
    total_sessions = db.query(func.count(func.distinct(AgentState.session_id))).scalar() or 0
    total_actions = db.query(func.count(AgentState.id)).scalar() or 0
    total_tokens = db.query(func.sum(AgentState.context_size_tokens)).scalar() or 0
    last_day_cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    actions_last_day = (
        db.query(func.count(AgentState.id))
        .filter(AgentState.created_at >= last_day_cutoff)
        .scalar()
        or 0
    )
    status_rows = db.query(AgentState.status, func.count(AgentState.id)).group_by(AgentState.status).all()
    status_breakdown = {status: count for status, count in status_rows}

    recent_states = db.query(AgentState).order_by(AgentState.created_at.desc(), AgentState.id.desc()).limit(60).all()
    recent_activity = [serialize_agent_state(state) for state in recent_states[:20]]

    session_index: dict[str, dict] = {}
    recent_sessions: list[dict] = []
    for state in recent_states:
        session = session_index.get(state.session_id)
        if session is None:
            title_priority, title = _session_title_from_state(state)
            session = {
                "session_id": state.session_id,
                "short_id": state.session_id[:8],
                "title": title,
                "title_priority": title_priority,
                "summary": _session_summary_from_state(state),
                "last_agent": state.agent_name,
                "last_agent_label": _agent_label(state.agent_name),
                "last_status": state.status,
                "status_label": _status_label(state.status),
                "status_tone": _status_tone(state.status),
                "last_activity_at": state.created_at.isoformat() if state.created_at else None,
                "started_at": state.created_at.isoformat() if state.created_at else None,
                "total_actions": 0,
                "completed_actions": 0,
                "task_ids": set(),
            }
            session_index[state.session_id] = session
            recent_sessions.append(session)

        session["started_at"] = state.created_at.isoformat() if state.created_at else session["started_at"]
        session["total_actions"] += 1
        if state.status == "completed" and state.agent_name != "orchestrator":
            session["completed_actions"] += 1
        task_id = (state.data or {}).get("task_id")
        if task_id:
            session["task_ids"].add(str(task_id))
        title_priority, title = _session_title_from_state(state)
        if title and title_priority > session.get("title_priority", 0):
            session["title"] = title
            session["title_priority"] = title_priority
        if not session.get("summary"):
            session["summary"] = _session_summary_from_state(state)

    normalized_sessions = []
    for session in recent_sessions[:10]:
        session_payload = {key: value for key, value in session.items() if key != "title_priority"}
        normalized_sessions.append(
            {
                **session_payload,
                "title": session.get("title") or f"Session {session['short_id']}",
                "task_count": len(session["task_ids"]),
                "task_ids": sorted(session["task_ids"]),
            }
        )

    return {
        "summary": {
            "session_count": total_sessions,
            "action_count": total_actions,
            "actions_last_day": actions_last_day,
            "avg_actions_per_session": round(total_actions / total_sessions, 2) if total_sessions else 0,
            "estimated_tokens_observed": int(total_tokens),
            "status_breakdown": status_breakdown,
        },
        "recent_sessions": normalized_sessions,
        "recent_activity": recent_activity,
    }


def build_telemetry_payload(db: Session) -> dict:
    telemetry_stamp = db.query(
        func.count(AgentState.id),
        func.max(AgentState.id),
    ).one()
    cache_key = (
        int(telemetry_stamp[0] or 0),
        int(telemetry_stamp[1] or 0),
    )
    return cached_value("telemetry", cache_key, 2.0, lambda: _build_telemetry_payload(db))


@router.get("/snapshot")
def get_admin_snapshot(memory_limit: int = 12, db: Session = Depends(get_db)):
    safe_limit = max(1, min(memory_limit, 100))

    def loader():
        memory_recency = func.coalesce(Memory.updated_at, Memory.created_at)
        artifact_recency = func.coalesce(Artifact.updated_at, Artifact.created_at)

        profile = db.query(Profile).filter(Profile.name == "default_user").first()
        agents = db.query(AgentProfile).order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc()).all()
        tools = db.query(DynamicTool).order_by(DynamicTool.name.asc()).all()
        recent_memories = db.query(Memory).order_by(memory_recency.desc(), Memory.id.desc()).limit(safe_limit).all()
        recent_artifacts = db.query(Artifact).order_by(artifact_recency.desc(), Artifact.id.desc()).limit(safe_limit).all()

        return {
            "configured": profile is not None,
            "profile": serialize_profile(profile),
            "profile_answers": derive_profile_answers(profile),
            "agents": [serialize_agent(agent) for agent in agents],
            "tools": [{"name": tool.name, "description": tool.description} for tool in tools],
            "recent_memories": [serialize_memory(memory) for memory in recent_memories],
            "recent_artifacts": [serialize_artifact(artifact) for artifact in recent_artifacts],
            "stats": {
                "agent_count": db.query(func.count(AgentProfile.id)).scalar() or 0,
                "tool_count": db.query(func.count(DynamicTool.id)).scalar() or 0,
                "memory_count": db.query(func.count(Memory.id)).scalar() or 0,
                "artifact_count": db.query(func.count(Artifact.id)).scalar() or 0,
                "archived_memory_count": db.query(func.count(Memory.id)).filter(Memory.is_archived.is_(True)).scalar() or 0,
                "pinned_memory_count": db.query(func.count(Memory.id)).filter(Memory.is_pinned.is_(True)).scalar() or 0,
                "compacted_memory_count": db.query(func.count(Memory.id)).filter(Memory.is_compacted.is_(True)).scalar() or 0,
            },
            "runtime": build_runtime_status(db),
            "clients": build_client_surface(),
            "telemetry": build_telemetry_payload(db),
        }

    return cached_value("admin_snapshot", safe_limit, 2.0, loader)


@router.post("/connect/{target}")
def connect_ai_clients(target: str, scope: str = "user"):
    try:
        result = connect_clients(target=target, scope=scope, dry_run=False)
        invalidate_many("client_surface", "admin_snapshot")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
