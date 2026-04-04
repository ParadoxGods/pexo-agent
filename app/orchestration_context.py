from __future__ import annotations

from sqlalchemy.orm import Session

from .cache import cached_value, invalidate_context_caches
from .database import SessionLocal
from .models import AgentProfile, DynamicTool, Profile, Memory, Artifact
from .search_index import search_memory_ids, search_artifact_ids


def _build_context_payload(db: Session, query: str | None = None) -> dict:
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

    # Recursive Self-Evolution: Fetch recent lessons learned
    lessons = (
        db.query(Memory)
        .filter(Memory.task_context == "lesson_learned")
        .order_by(Memory.created_at.desc())
        .limit(5)
        .all()
    )
    lessons_learned_text = "\n".join([f"- {m.content}" for m in lessons]) if lessons else "No previous lessons learned yet."

    # Predictive Context Retrieval (Automatic RAG)
    relevant_context_text = "No additional context found for this prompt."
    if query:
        mem_ids = search_memory_ids(query, limit=3)
        art_ids = search_artifact_ids(query, limit=3)
        
        rel_memories = db.query(Memory).filter(Memory.id.in_(mem_ids)).all() if mem_ids else []
        rel_artifacts = db.query(Artifact).filter(Artifact.id.in_(art_ids)).all() if art_ids else []
        
        context_fragments = []
        for m in rel_memories:
            context_fragments.append(f"[Past Memory] {m.content[:300]}")
        for a in rel_artifacts:
            snippet = a.extracted_text[:500] if a.extracted_text else "Binary/Non-text artifact"
            context_fragments.append(f"[Artifact: {a.name}] {snippet}")
        
        if context_fragments:
            relevant_context_text = "\n\n".join(context_fragments)

    return {
        "profile_text": profile_text,
        "agent_registry": agent_registry,
        "core_agent_text": core_agent_text,
        "custom_agent_text": custom_agent_text,
        "tool_text": tool_text,
        "lessons_learned_text": lessons_learned_text,
        "relevant_context_text": relevant_context_text,
    }


def build_session_context_snapshot(db: Session | None = None, query: str | None = None) -> dict:
    if db is not None:
        return _build_context_payload(db, query=query)

    def loader():
        local_db = SessionLocal()
        try:
            return _build_context_payload(local_db, query=query)
        finally:
            local_db.close()

    # Cache is keyed by query to ensure different prompts get different RAG context
    cache_key = f"session_context_seed_{hash(query)}" if query else "session_context_seed_default"
    return cached_value(cache_key, "default_user", 5.0, loader)


def invalidate_session_context_snapshot() -> None:
    invalidate_context_caches()
