from typing import TypedDict, List, Dict, Any, Optional

try:
    from langgraph.graph import StateGraph, END
except ImportError:  # pragma: no cover - exercised by lightweight runtime paths
    StateGraph = None
    END = "__end__"

from ..orchestration_context import build_session_context_snapshot
from ..database import SessionLocal
from ..models import Memory, Artifact

class PexoState(TypedDict):
    session_id: str
    user_prompt: str
    clarification_answer: str
    tasks: List[Dict[str, Any]]
    completed_tasks: List[Dict[str, Any]]
    reviewed_tasks: List[Dict[str, Any]]
    active_tasks: List[str]
    current_agent: str
    current_instruction: str
    waiting_for_ai: bool
    final_response: str
    user_profile: str
    available_agents: str
    available_tools: str
    context_snapshot: Dict[str, Any]


def _get_context(state: PexoState | None = None):
    if state and state.get("context_snapshot"):
        return state["context_snapshot"]
    return build_session_context_snapshot()


def _resolve_agent_context(agent_registry: Dict[str, Dict[str, Any]], agent_name: str, fallback_name: str) -> Dict[str, Any]:
    return agent_registry.get(agent_name) or agent_registry.get(fallback_name) or {
        "name": fallback_name,
        "role": fallback_name,
        "system_prompt": f"You are {fallback_name}. Finish the assigned work with minimal overhead and clear verification.",
        "capabilities": [],
        "is_core": True,
    }


def _format_capabilities(agent: Dict[str, Any]) -> str:
    capabilities = agent.get("capabilities") or []
    if isinstance(capabilities, dict) and "list" in capabilities:
        base = ", ".join(capabilities["list"])
        schemas = json.dumps(capabilities.get("schemas", {}), indent=2)
        return f"{base}\nDetailed Schemas:\n{schemas}"
    return ", ".join(capabilities) if capabilities else "No explicit capabilities registered."

def _get_lessons_learned() -> str:
    from ..database import SessionLocal
    from ..models import Memory
    db = SessionLocal()
    try:
        lessons = (
            db.query(Memory)
            .filter(Memory.task_context == "lesson_learned")
            .order_by(Memory.created_at.desc())
            .limit(5)
            .all()
        )
        return "\n".join([f"- {m.content}" for m in lessons]) if lessons else "No previous lessons learned yet."
    finally:
        db.close()


def supervisor_node(state: PexoState):
    if not state.get("tasks"):
        context = _get_context(state)
        supervisor = _resolve_agent_context(context["agent_registry"], "Supervisor", "Supervisor")
        lessons_text = _get_lessons_learned()
        
        instruction = (
            f"{supervisor['system_prompt']}\n\n"
            f"--- ACTIVE AGENT PROFILE ---\n"
            f"Name: {supervisor['name']}\n"
            f"Role: {supervisor['role']}\n"
            f"Capabilities: {_format_capabilities(supervisor)}\n\n"
            f"--- PROMPT & CLARIFICATION ---\n"
            f"Prompt: {state['user_prompt']}\n"
            f"User Clarification: {state['clarification_answer']}\n\n"
            f"--- RELEVANT PROJECT CONTEXT (PREDICTIVE RAG) ---\n"
            f"{context.get('relevant_context_text', 'No additional context found.')}\n\n"
            f"--- LESSONS FROM PREVIOUS FAILURES (RECURSIVE LEARNING) ---\n"
            f"{lessons_text}\n\n"
            f"--- STRICT USER CONSTRAINTS ---\n"
            f"{context['profile_text']}\n\n"
            f"--- AVAILABLE CORE AGENTS ---\n"
            f"{context['core_agent_text']}\n\n"
            f"--- AVAILABLE CUSTOM AGENTS ---\n"
            f"{context['custom_agent_text']}\n\n"
            f"--- AVAILABLE SWARM TOOLS (THE GENESIS ENGINE) ---\n"
            f"{context['tool_text']}\n\n"
            f"--- NON-NEGOTIABLE OUTPUT CONTRACT ---\n"
            f"If the user's request is too vague, ambiguous, or missing critical constraints required to plan the work, return a JSON object: {{\"clarification_required\": \"Your specific question here.\"}}.\n"
            f"Otherwise, return a raw JSON array of tasks. Each task MUST have: 'id', 'description', 'assigned_agent', and 'requires' (an array of task IDs that must be completed first).\n"
            f"If a task requires a capability you lack, assign a task to write and register a new tool to Pexo's Genesis Engine."
        )
        
        return {
            "current_agent": "Supervisor",
            "current_instruction": instruction,
            "waiting_for_ai": True,
            "user_profile": context["profile_text"],
            "available_agents": f"Core: {context['core_agent_text']}\nCustom: {context['custom_agent_text']}",
            "available_tools": context["tool_text"],
        }
    return {"waiting_for_ai": False}

def developer_node(state: PexoState):
    tasks = state.get("tasks", [])
    completed = state.get("completed_tasks", [])
    active_ids = set(state.get("active_tasks", []))
    completed_ids = {t.get("task", {}).get("id") for t in completed}
    
    # Find the next task that is not completed, not active, and whose dependencies are met
    next_task = None
    for task in tasks:
        task_id = task.get("id")
        if task_id in completed_ids or task_id in active_ids:
            continue
        
        requires = task.get("requires") or []
        if all(req_id in completed_ids for req_id in requires):
            next_task = task
            break
            
    if next_task:
        assigned = next_task.get('assigned_agent', 'Developer')
        context = _get_context(state)
        assigned_agent = _resolve_agent_context(context["agent_registry"], assigned, "Developer")
        lessons_text = _get_lessons_learned()
        
        instruction = (
            f"{assigned_agent['system_prompt']}\n\n"
            f"--- ACTIVE AGENT PROFILE ---\n"
            f"Name: {assigned_agent['name']}\n"
            f"Role: {assigned_agent['role']}\n"
            f"Capabilities: {_format_capabilities(assigned_agent)}\n\n"
            f"--- LESSONS FROM PREVIOUS FAILURES (RECURSIVE LEARNING) ---\n"
            f"{lessons_text}\n\n"
            f"--- TASK TO EXECUTE ---\n"
            f"Task ID: {next_task.get('id', 'unknown')}\n"
            f"Description: {next_task.get('description', 'No description provided')}\n\n"
            f"--- ENFORCED USER PROFILE ---\n"
            f"{state.get('user_profile', 'None')}\n\n"
            f"--- NON-NEGOTIABLE OUTPUT CONTRACT ---\n"
            f"Execute this task immediately on the local system. Once done, return your code, findings, or proof of execution."
        )
        
        return {
            "current_agent": assigned_agent["name"],
            "current_instruction": instruction,
            "waiting_for_ai": True
        }
    return {"waiting_for_ai": False}

def reviewer_node(state: PexoState):
    completed = state.get("completed_tasks", [])
    reviewed = state.get("reviewed_tasks", [])
    if len(reviewed) < len(completed):
        last_completed = completed[len(reviewed)]
        task_info = last_completed.get("task", {})
        worker_result = last_completed.get("result", "")
        context = _get_context(state)
        qa = _resolve_agent_context(context["agent_registry"], "Quality Assurance Manager", "Quality Assurance Manager")
        
        instruction = (
            f"{qa['system_prompt']}\n\n"
            f"--- ACTIVE AGENT PROFILE ---\n"
            f"Name: {qa['name']}\n"
            f"Role: {qa['role']}\n"
            f"Capabilities: {_format_capabilities(qa)}\n\n"
            f"--- TASK TO REVIEW ---\n"
            f"Task ID: {task_info.get('id', 'unknown')}\n"
            f"Description: {task_info.get('description', 'No description provided')}\n\n"
            f"--- WORKER RESULT ---\n"
            f"{worker_result}\n\n"
            f"--- NON-NEGOTIABLE OUTPUT CONTRACT ---\n"
            f"Critically review the result. If it perfectly satisfies the task description, output 'PASS'. "
            f"If it does not, output 'FAIL' followed by a description of what must be fixed. "
            f"CRITICAL: If you FAIL, you MUST also provide a 'LESSON LEARNED:' block for the developer to prevent this mistake in the future."
        )
        return {
            "current_agent": qa["name"],
            "current_instruction": instruction,
            "waiting_for_ai": True
        }
    return {"waiting_for_ai": False}

def genesis_node(state: PexoState):
    tasks = state.get("tasks", [])
    completed = state.get("completed_tasks", [])
    completed_ids = {t.get("task", {}).get("id") for t in completed}
    
    # Find the next task assigned to Genesis Architect
    next_task = None
    for task in tasks:
        if task.get("id") not in completed_ids and task.get("assigned_agent") == "Genesis Architect":
            requires = task.get("requires") or []
            if all(req_id in completed_ids for req_id in requires):
                next_task = task
                break
                
    if next_task:
        context = _get_context(state)
        genesis = _resolve_agent_context(context["agent_registry"], "Genesis Architect", "Genesis Architect")
        
        instruction = (
            f"{genesis['system_prompt']}\n\n"
            f"--- TASK TO EXECUTE ---\n"
            f"Task ID: {next_task.get('id', 'unknown')}\n"
            f"Description: {next_task.get('description', 'No description provided')}\n\n"
            f"--- RELEVANT CONTEXT ---\n"
            f"{context.get('relevant_context_text', 'None')}\n\n"
            f"--- NON-NEGOTIABLE OUTPUT CONTRACT ---\n"
            f"Output ONLY a valid JSON object with 'name', 'description', and 'python_code'. "
            f"Example: {{\"name\": \"my_tool\", \"description\": \"...\", \"python_code\": \"def run(**kwargs):\\n    return 'hello'\"}}"
        )
        
        return {
            "current_agent": "Genesis Architect",
            "current_instruction": instruction,
            "waiting_for_ai": True
        }
    return {"waiting_for_ai": False}

def shadow_node(state: PexoState):
    """Shadow Simulation: Verifies safety and identifies conflicts before execution."""
    tasks = state.get("tasks", [])
    completed = state.get("completed_tasks", [])
    active_ids = set(state.get("active_tasks", []))
    completed_ids = {t.get("task", {}).get("id") for t in completed}
    
    # Find all tasks that are 'ready' but not yet simulated or active
    ready_tasks = []
    for task in tasks:
        if task.get("id") not in completed_ids and task.get("id") not in active_ids:
            requires = task.get("requires") or []
            if all(req_id in completed_ids for req_id in requires):
                ready_tasks.append(task)
    
    if not ready_tasks:
        return {"waiting_for_ai": False}

    context = _get_context(state)
    timer = _resolve_agent_context(context["agent_registry"], "Time Manager", "Time Manager")
    
    instruction = (
        f"{timer['system_prompt']}\n\n"
        f"--- READY TASKS TO SIMULATE ---\n"
        f"{json.dumps(ready_tasks, indent=2)}\n\n"
        f"--- ACTIVE WORKERS ---\n"
        f"{list(active_ids)}\n\n"
        f"--- NON-NEGOTIABLE OUTPUT CONTRACT ---\n"
        f"Identify if any ready tasks conflict with each other or active workers. "
        f"If a conflict exists, return 'CONFLICT: [task-id] requires [task-id]'. "
        f"If all are safe, return 'SIMULATION_PASS'."
    )
    
    return {
        "current_agent": "Time Manager",
        "current_instruction": instruction,
        "waiting_for_ai": True
    }

def manager_node(state: PexoState):
    if state.get("current_agent") != "Code Organization Manager":
        context = _get_context(state)
        manager = _resolve_agent_context(context["agent_registry"], "Code Organization Manager", "Code Organization Manager")
        instruction = (
            f"{manager['system_prompt']}\n\n"
            f"--- ACTIVE AGENT PROFILE ---\n"
            f"Name: {manager['name']}\n"
            f"Role: {manager['role']}\n"
            f"Capabilities: {_format_capabilities(manager)}\n\n"
            f"--- OBJECTIVE ---\n"
            f"Review all completed tasks from the worker agents and ensure they perfectly match the user's constraints:\n"
            f"{state.get('user_profile', 'None')}\n\n"
            f"--- NON-NEGOTIABLE OUTPUT CONTRACT ---\n"
            f"Format the final output for the user. If tests or specific directory structures were required by the profile, verify they exist."
        )
        return {
            "current_agent": "Code Organization Manager",
            "current_instruction": instruction,
            "waiting_for_ai": True
        }
    return {"waiting_for_ai": False, "final_response": "All tasks completed and rigorously reviewed against user profile. Session closed."}

def router(state: PexoState):
    if state.get("waiting_for_ai"):
        return END
    
    current_agent = state.get("current_agent")
    tasks = state.get("tasks", [])
    completed = state.get("completed_tasks", [])

    if current_agent == "Supervisor":
        if not tasks:
            return "supervisor"
        return "shadow"

    if current_agent == "Time Manager":
        # Check for conflicts from Shadow Node
        instruction = state.get("current_instruction", "")
        if "CONFLICT" in instruction:
            # We need to stay in shadow or go back to supervisor to fix the DAG?
            # For now, just stay until AI provides a safe path
            return "shadow"
        
        # Check if next task is Genesis Architect
        next_pending = None
        completed_ids = {t.get("task", {}).get("id") for t in completed}
        for t in tasks:
            if t.get("id") not in completed_ids:
                next_pending = t
                break
        
        if next_pending and next_pending.get("assigned_agent") == "Genesis Architect":
            return "genesis"
        return "developer"

    if current_agent == "Genesis Architect":
        return "developer"

    if current_agent == "Quality Assurance Manager":
        if len(completed) < len(tasks):
            return "shadow"
        return "manager"

    if current_agent not in ["Supervisor", "Code Organization Manager", "Quality Assurance Manager", "Time Manager"]:
        return "reviewer"

    if current_agent == "Code Organization Manager":
        return END

    return END


def _resolve_start_node(state: PexoState) -> str:
    if state.get("waiting_for_ai"):
        return ""
    
    route = router(state)
    if route == END:
        return ""
    
    mapping = {
        "developer": "developer",
        "reviewer": "reviewer",
        "manager": "manager",
        "genesis": "genesis"
    }
    return mapping.get(route, route)


def _advance_state_machine(state: PexoState) -> PexoState:
    current_state = dict(state)
    next_node = _resolve_start_node(current_state)
    
    if not next_node:
        return current_state

    loop_count = 0
    while loop_count < 15:
        loop_count += 1
        node_result = {}
        if next_node == "supervisor":
            node_result = supervisor_node(current_state)
        elif next_node == "developer":
            node_result = developer_node(current_state)
        elif next_node == "reviewer":
            node_result = reviewer_node(current_state)
        elif next_node == "manager":
            node_result = manager_node(current_state)
        elif next_node == "genesis":
            node_result = genesis_node(current_state)
        else:
            return current_state

        current_state.update(node_result)
        route = router(current_state)
        
        if route == END:
            return current_state
        
        if route == next_node and not current_state.get("waiting_for_ai"):
            return current_state
            
        next_node = route
        
    return current_state


class FallbackPexoApp:
    def invoke(self, state: PexoState):
        return _advance_state_machine(state)


if StateGraph is None:
    pexo_app = FallbackPexoApp()
else:
    workflow = StateGraph(PexoState)
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("shadow", shadow_node)
    workflow.add_node("developer", developer_node)
    workflow.add_node("reviewer", reviewer_node)
    workflow.add_node("manager", manager_node)
    workflow.add_node("genesis", genesis_node)

    workflow.set_entry_point("supervisor")
    workflow.add_conditional_edges("supervisor", router, {END: END, "shadow": "shadow"})
    workflow.add_conditional_edges("shadow", router, {END: END, "developer": "developer", "genesis": "genesis"})
    workflow.add_conditional_edges("developer", router, {END: END, "reviewer": "reviewer"})
    workflow.add_conditional_edges("reviewer", router, {END: END, "developer": "developer", "manager": "manager", "genesis": "genesis", "shadow": "shadow"})
    workflow.add_conditional_edges("genesis", router, {END: END, "developer": "developer"})
    workflow.add_conditional_edges("manager", router, {END: END})

    pexo_app = workflow.compile()


def invoke_pexo_graph(state: PexoState):
    try:
        return _advance_state_machine(state)
    except Exception:
        fallback = FallbackPexoApp()
        return fallback.invoke(state)
