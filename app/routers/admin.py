from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AgentProfile, DynamicTool, Memory, Profile
from .memory import serialize_memory

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


@router.get("/snapshot")
def get_admin_snapshot(memory_limit: int = 12, db: Session = Depends(get_db)):
    safe_limit = max(1, min(memory_limit, 100))

    profile = db.query(Profile).filter(Profile.name == "default_user").first()
    agents = db.query(AgentProfile).order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc()).all()
    tools = db.query(DynamicTool).order_by(DynamicTool.name.asc()).all()
    recent_memories = db.query(Memory).order_by(Memory.created_at.desc()).limit(safe_limit).all()

    return {
        "configured": profile is not None,
        "profile": serialize_profile(profile),
        "agents": [serialize_agent(agent) for agent in agents],
        "tools": [{"name": tool.name, "description": tool.description} for tool in tools],
        "recent_memories": [serialize_memory(memory) for memory in recent_memories],
        "stats": {
            "agent_count": db.query(func.count(AgentProfile.id)).scalar() or 0,
            "tool_count": db.query(func.count(DynamicTool.id)).scalar() or 0,
            "memory_count": db.query(func.count(Memory.id)).scalar() or 0,
        },
    }
