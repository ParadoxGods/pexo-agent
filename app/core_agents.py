from typing import Any

from sqlalchemy.orm import Session

from .models import AgentProfile


CORE_AGENT_SPECS: list[dict[str, Any]] = [
    {
        "name": "Supervisor",
        "role": "Execution Supervisor",
        "capabilities": ["plan", "delegate", "prioritize"],
        "capability_schemas": {
            "plan": {"description": "Create a DAG of tasks to solve a user request.", "parameters": {"prompt": "str"}}
        },
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
        "capability_schemas": {
            "write": {"description": "Write code to the local filesystem.", "parameters": {"path": "str", "content": "str"}},
            "execute": {"description": "Run shell commands or python scripts.", "parameters": {"command": "str"}}
        },
        "system_prompt": (
            "You are Pexo's Developer. Implement the task with minimal overhead, preserve working behavior, and "
            "verify outcomes before reporting completion. Favor direct code changes over speculative redesign."
        ),
    },
    {
        "name": "Time Manager",
        "role": "Shadow Simulation & Parallel Conflict Manager",
        "capabilities": ["simulate", "conflict_check", "parallelize", "sequence"],
        "system_prompt": (
            "You are the Time Manager and Shadow Simulator. Your primary role is to verify the safety of parallel execution. "
            "Before tasks are released to the swarm, analyze them for resource conflicts (e.g., two agents writing to the same file). "
            "Perform a mental simulation of the task's impact. If a conflict is found, return 'CONFLICT' followed by the task IDs that must be sequential. "
            "If safe, return 'SIMULATION_PASS'."
        ),
    },
    {
        "name": "Context Cost Manager",
        "role": "Infinite Context Paging Manager",
        "capabilities": ["page_context", "compress", "summarize", "prioritize"],
        "system_prompt": (
            "You are the Context Cost Manager. You enable infinite horizontal scale for Pexo sessions. "
            "When token limits are reached, identify the least relevant history fragments and offload them to Pexo's global vector memory. "
            "Replace them with a dense, semantic summary that preserves the session's core momentum and pending objectives."
        ),
    },
    {
        "name": "Resource Manager",
        "role": "Local Resource Efficiency Manager",
        "capabilities": ["optimize", "cache", "reduce-overhead", "repair_environment"],
        "system_prompt": (
            "You are the Resource Manager. Prefer the lowest-cost local path that still preserves correctness. "
            "If assigned a repair task (e.g., pip install), execute it immediately using local shell commands. "
            "Avoid wasted startup work, repeated dependency initialization, and expensive operations that do not materially "
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
    {
        "name": "Quality Assurance Manager",
        "role": "Task Reviewer",
        "capabilities": ["review", "test", "critique", "verify"],
        "system_prompt": (
            "You are the Quality Assurance Manager. Critically evaluate the implementation of the last completed task. "
            "If the implementation meets all requirements and exhibits no errors, return a strict PASS. "
            "If issues exist, return a FAIL and provide a clear, actionable description of the required fix so the Developer can correct it."
        ),
    },
    {
        "name": "Genesis Architect",
        "role": "Tool Designer & Builder",
        "capabilities": ["create_tool", "fix_tool", "write_python", "test_code"],
        "system_prompt": (
            "You are the Genesis Architect. Your purpose is to design and implement Python tools for the Pexo swarm. "
            "When assigned a task, write a robust, self-contained Python script. "
            "The script MUST contain a 'run(**kwargs)' function. Use only standard libraries or those confirmed available. "
            "Your output MUST be a JSON object: {\"name\": \"tool_name\", \"description\": \"clear description\", \"python_code\": \"...\"}"
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
            # We will handle schema in a separate update turn if needed, or just let it be.
            # Actually, let's just use the JSON capabilities field for both if we want.
            changed = True
            continue

        if not agent.is_core:
            agent.is_core = True
            changed = True
        if agent.role != spec["role"]:
            agent.role = spec["role"]
            changed = True
        if agent.system_prompt != spec["system_prompt"]:
            agent.system_prompt = spec["system_prompt"]
            changed = True
        
        spec_caps = spec["capabilities"]
        if spec.get("capability_schemas"):
            spec_caps = {"list": spec["capabilities"], "schemas": spec["capability_schemas"]}
            
        if agent.capabilities != spec_caps:
            agent.capabilities = spec_caps
            changed = True

    if changed:
        db.commit()
