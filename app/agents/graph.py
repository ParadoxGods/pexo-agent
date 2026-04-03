from typing import TypedDict, List, Dict, Any, Optional
from langgraph.graph import StateGraph, END
from ..database import SessionLocal
from ..models import Profile, AgentProfile, DynamicTool

class PexoState(TypedDict):
    session_id: str
    user_prompt: str
    clarification_answer: str
    tasks: List[Dict[str, Any]]
    completed_tasks: List[Dict[str, Any]]
    current_agent: str
    current_instruction: str
    waiting_for_ai: bool
    final_response: str
    user_profile: str
    available_agents: str
    available_tools: str

def _get_context():
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.name == "default_user").first()
        agents = db.query(AgentProfile).order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc()).all()
        tools = db.query(DynamicTool).all()
        
        prof_text = "No profile set."
        if profile:
            prof_text = f"Personality: {profile.personality_prompt}\nScripting: {profile.scripting_preferences}"

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

        core_agent_text = ", ".join(
            [f"{agent.name} (Role: {agent.role})" for agent in core_agents]
        ) or "No core agents registered."
        custom_agent_text = ", ".join(
            [f"{agent.name} (Role: {agent.role})" for agent in custom_agents]
        ) or "No custom agents registered."
            
        tool_text = ", ".join([f"{t.name}: {t.description}" for t in tools])
        if not tool_text:
            tool_text = "No dynamic tools generated yet. The swarm must rely on native capabilities or create new tools via /tools/register."
            
        return {
            "profile_text": prof_text,
            "agent_registry": agent_registry,
            "core_agent_text": core_agent_text,
            "custom_agent_text": custom_agent_text,
            "tool_text": tool_text,
        }
    finally:
        db.close()


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
    return ", ".join(capabilities) if capabilities else "No explicit capabilities registered."

def supervisor_node(state: PexoState):
    if not state.get("tasks"):
        context = _get_context()
        supervisor = _resolve_agent_context(context["agent_registry"], "Supervisor", "Supervisor")
        
        instruction = (
            f"{supervisor['system_prompt']}\n\n"
            f"--- ACTIVE AGENT PROFILE ---\n"
            f"Name: {supervisor['name']}\n"
            f"Role: {supervisor['role']}\n"
            f"Capabilities: {_format_capabilities(supervisor)}\n\n"
            f"--- PROMPT & CLARIFICATION ---\n"
            f"Prompt: {state['user_prompt']}\n"
            f"User Clarification: {state['clarification_answer']}\n\n"
            f"--- STRICT USER CONSTRAINTS ---\n"
            f"{context['profile_text']}\n\n"
            f"--- AVAILABLE CORE AGENTS ---\n"
            f"{context['core_agent_text']}\n\n"
            f"--- AVAILABLE CUSTOM AGENTS ---\n"
            f"{context['custom_agent_text']}\n\n"
            f"--- AVAILABLE SWARM TOOLS (THE GENESIS ENGINE) ---\n"
            f"{context['tool_text']}\n\n"
            f"--- NON-NEGOTIABLE OUTPUT CONTRACT ---\n"
            f"Return a raw JSON array of tasks. Each task MUST have: 'id', 'description', and an 'assigned_agent' (chosen from the available list above based on the task's needs). If a task requires a capability you lack, assign a task to write and register a new tool to Pexo's Genesis Engine."
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
    if len(completed) < len(tasks):
        next_task = tasks[len(completed)]
        assigned = next_task.get('assigned_agent', 'Developer')
        context = _get_context()
        assigned_agent = _resolve_agent_context(context["agent_registry"], assigned, "Developer")
        
        instruction = (
            f"{assigned_agent['system_prompt']}\n\n"
            f"--- ACTIVE AGENT PROFILE ---\n"
            f"Name: {assigned_agent['name']}\n"
            f"Role: {assigned_agent['role']}\n"
            f"Capabilities: {_format_capabilities(assigned_agent)}\n\n"
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

def manager_node(state: PexoState):
    if state.get("current_agent") != "Code Organization Manager":
        context = _get_context()
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
    
    if state.get("current_agent") == "Supervisor":
        return "developer"
        
    # If the current agent is anything OTHER than Supervisor or the final Manager, 
    # it means a worker (Developer or Custom Agent) just finished.
    if state.get("current_agent") not in ["Supervisor", "Code Organization Manager"]:
        tasks = state.get("tasks", [])
        completed = state.get("completed_tasks", [])
        if len(completed) < len(tasks):
            return "developer" # Loop back to assign the next task
        return "manager" # All tasks done, go to final review
        
    if state.get("current_agent") == "Code Organization Manager":
        return END
        
    return END

workflow = StateGraph(PexoState)
workflow.add_node("supervisor", supervisor_node)
workflow.add_node("developer", developer_node)
workflow.add_node("manager", manager_node)

workflow.set_entry_point("supervisor")
workflow.add_conditional_edges("supervisor", router, {END: END, "developer": "developer"})
workflow.add_conditional_edges("developer", router, {END: END, "developer": "developer", "manager": "manager"})
workflow.add_conditional_edges("manager", router, {END: END})

pexo_app = workflow.compile()
