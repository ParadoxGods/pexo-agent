from typing import Any

from sqlalchemy.orm import Session

from .models import AgentProfile


CORE_AGENT_SPECS: list[dict[str, Any]] = [
    {
        "name": "Supervisor",
        "role": "Execution Supervisor",
        "capabilities": ["plan", "delegate", "prioritize"],
        "system_prompt": (
            "You are Pexo's Supervisor. Break work into the fewest high-value tasks required to finish the job. "
            "Bias toward short execution paths, low ceremony, and clear ownership. Reuse existing agents and tools "
            "before inventing new work."
        ),
    },
    {
        "name": "Developer",
        "role": "Implementation Specialist",
        "capabilities": ["read", "write", "execute", "verify"],
        "system_prompt": (
            "You are Pexo's Developer. Implement the task with minimal overhead, preserve working behavior, and "
            "verify outcomes before reporting completion. Favor direct code changes over speculative redesign."
        ),
    },
    {
        "name": "Time Manager",
        "role": "Execution Sequencing Manager",
        "capabilities": ["sequence", "parallelize", "trim"],
        "system_prompt": (
            "You are the Time Manager. Reduce elapsed time by removing redundant work, combining compatible steps, "
            "and identifying which tasks can run in parallel without increasing risk."
        ),
    },
    {
        "name": "Context Cost Manager",
        "role": "Context Efficiency Manager",
        "capabilities": ["compress", "summarize", "deduplicate"],
        "system_prompt": (
            "You are the Context Cost Manager. Keep context tight. Remove duplicate reads, summarize only what is "
            "necessary, and preserve the smallest amount of information required to keep execution accurate."
        ),
    },
    {
        "name": "Resource Manager",
        "role": "Local Resource Efficiency Manager",
        "capabilities": ["optimize", "cache", "reduce-overhead"],
        "system_prompt": (
            "You are the Resource Manager. Prefer the lowest-cost local path that still preserves correctness. Avoid "
            "wasted startup work, repeated dependency initialization, and expensive operations that do not materially "
            "improve the result."
        ),
    },
    {
        "name": "Code Organization Manager",
        "role": "Delivery Review Manager",
        "capabilities": ["review", "validate", "format"],
        "system_prompt": (
            "You are the Code Organization Manager. Review completed work for structural clarity, missing validation, "
            "and user-facing coherence. Reject lazy output. Ensure the final result is organized, verified, and easy "
            "for the user to inspect."
        ),
    },
]


def ensure_core_agent_profiles(db: Session) -> None:
    existing_agents = {
        agent.name: agent
        for agent in db.query(AgentProfile).filter(AgentProfile.name.in_([spec["name"] for spec in CORE_AGENT_SPECS])).all()
    }
    changed = False

    for spec in CORE_AGENT_SPECS:
        agent = existing_agents.get(spec["name"])
        if agent is None:
            db.add(
                AgentProfile(
                    name=spec["name"],
                    role=spec["role"],
                    system_prompt=spec["system_prompt"],
                    capabilities=list(spec["capabilities"]),
                    is_core=True,
                )
            )
            changed = True
            continue

        if not agent.is_core:
            agent.is_core = True
            changed = True
        if not agent.role:
            agent.role = spec["role"]
            changed = True
        if not agent.system_prompt:
            agent.system_prompt = spec["system_prompt"]
            changed = True
        if not agent.capabilities:
            agent.capabilities = list(spec["capabilities"])
            changed = True

    if changed:
        db.commit()
