from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AgentProfile, AgentState, DynamicTool, Memory, Profile
from .memory import serialize_memory
from .profile import derive_profile_answers

router = APIRouter()


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


def build_telemetry_payload(db: Session) -> dict:
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
            session = {
                "session_id": state.session_id,
                "last_agent": state.agent_name,
                "last_status": state.status,
                "last_activity_at": state.created_at.isoformat() if state.created_at else None,
                "total_actions": 0,
                "completed_actions": 0,
                "task_ids": set(),
            }
            session_index[state.session_id] = session
            recent_sessions.append(session)

        session["total_actions"] += 1
        if state.status == "completed" and state.agent_name != "orchestrator":
            session["completed_actions"] += 1
        task_id = (state.data or {}).get("task_id")
        if task_id:
            session["task_ids"].add(str(task_id))

    normalized_sessions = []
    for session in recent_sessions[:10]:
        normalized_sessions.append(
            {
                **session,
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


@router.get("/snapshot")
def get_admin_snapshot(memory_limit: int = 12, db: Session = Depends(get_db)):
    safe_limit = max(1, min(memory_limit, 100))
    memory_recency = func.coalesce(Memory.updated_at, Memory.created_at)

    profile = db.query(Profile).filter(Profile.name == "default_user").first()
    agents = db.query(AgentProfile).order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc()).all()
    tools = db.query(DynamicTool).order_by(DynamicTool.name.asc()).all()
    recent_memories = db.query(Memory).order_by(memory_recency.desc(), Memory.id.desc()).limit(safe_limit).all()

    return {
        "configured": profile is not None,
        "profile": serialize_profile(profile),
        "profile_answers": derive_profile_answers(profile),
        "agents": [serialize_agent(agent) for agent in agents],
        "tools": [{"name": tool.name, "description": tool.description} for tool in tools],
        "recent_memories": [serialize_memory(memory) for memory in recent_memories],
        "stats": {
            "agent_count": db.query(func.count(AgentProfile.id)).scalar() or 0,
            "tool_count": db.query(func.count(DynamicTool.id)).scalar() or 0,
            "memory_count": db.query(func.count(Memory.id)).scalar() or 0,
            "archived_memory_count": db.query(func.count(Memory.id)).filter(Memory.is_archived.is_(True)).scalar() or 0,
            "pinned_memory_count": db.query(func.count(Memory.id)).filter(Memory.is_pinned.is_(True)).scalar() or 0,
            "compacted_memory_count": db.query(func.count(Memory.id)).filter(Memory.is_compacted.is_(True)).scalar() or 0,
        },
        "telemetry": build_telemetry_payload(db),
    }
