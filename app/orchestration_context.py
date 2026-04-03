from __future__ import annotations

from sqlalchemy.orm import Session

from .cache import cached_value, invalidate_context_caches
from .database import SessionLocal
from .models import AgentProfile, DynamicTool, Profile


def _build_context_payload(db: Session) -> dict:
    profile = db.query(Profile).filter(Profile.name == "default_user").first()
    agents = db.query(AgentProfile).order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc()).all()
    tools = db.query(DynamicTool).order_by(DynamicTool.name.asc()).all()

    profile_text = "No profile set."
    if profile:
        profile_text = f"Personality: {profile.personality_prompt}\nScripting: {profile.scripting_preferences}"

    agent_registry = {
        agent.name: {
            "name": agent.name,
            "role": agent.role,
            "system_prompt": agent.system_prompt or "",
            "capabilities": list(agent.capabilities or []),
            "is_core": bool(agent.is_core),
        }
        for agent in agents
    }

    core_agents = [agent for agent in agents if agent.is_core]
    custom_agents = [agent for agent in agents if not agent.is_core]

    core_agent_text = ", ".join([f"{agent.name} (Role: {agent.role})" for agent in core_agents]) or "No core agents registered."
    custom_agent_text = ", ".join([f"{agent.name} (Role: {agent.role})" for agent in custom_agents]) or "No custom agents registered."
    tool_text = ", ".join([f"{tool.name}: {tool.description}" for tool in tools])
    if not tool_text:
        tool_text = "No dynamic tools generated yet. The swarm must rely on native capabilities or create new tools via /tools/register."

    return {
        "profile_text": profile_text,
        "agent_registry": agent_registry,
        "core_agent_text": core_agent_text,
        "custom_agent_text": custom_agent_text,
        "tool_text": tool_text,
    }


def build_session_context_snapshot(db: Session | None = None) -> dict:
    if db is not None:
        return _build_context_payload(db)

    def loader():
        local_db = SessionLocal()
        try:
            return _build_context_payload(local_db)
        finally:
            local_db.close()

    return cached_value("session_context_seed", "default_user", 5.0, loader)


def invalidate_session_context_snapshot() -> None:
    invalidate_context_caches()
