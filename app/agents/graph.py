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
        agents = db.query(AgentProfile).all()
        tools = db.query(DynamicTool).all()
        
        prof_text = "No profile set."
        if profile:
            prof_text = f"Personality: {profile.personality_prompt}\nScripting: {profile.scripting_preferences}"
            
        agent_text = ", ".join([f"{a.name} (Role: {a.role})" for a in agents])
        if not agent_text:
            agent_text = "No custom agents registered."
            
        tool_text = ", ".join([f"{t.name}: {t.description}" for t in tools])
        if not tool_text:
            tool_text = "No dynamic tools generated yet. The swarm must rely on native capabilities or create new tools via /tools/register."
            
        return prof_text, agent_text, tool_text
    finally:
        db.close()

def supervisor_node(state: PexoState):
    if not state.get("tasks"):
        prof_text, agent_text, tool_text = _get_context()
        
        instruction = (
            f"You are the SUPERVISOR AGENT. Your job is to break the user's prompt into discrete tasks.\n\n"
            f"--- PROMPT & CLARIFICATION ---\n"
            f"Prompt: {state['user_prompt']}\n"
            f"User Clarification: {state['clarification_answer']}\n\n"
            f"--- STRICT USER CONSTRAINTS ---\n"
            f"{prof_text}\n\n"
            f"--- AVAILABLE WORKER AGENTS ---\n"
            f"Core: Developer, Code Organization Manager\n"
            f"Custom: {agent_text}\n\n"
            f"--- AVAILABLE SWARM TOOLS (THE GENESIS ENGINE) ---\n"
            f"{tool_text}\n\n"
            f"ACTION REQUIRED:\n"
            f"Return a raw JSON array of tasks. Each task MUST have: 'id', 'description', and an 'assigned_agent' (chosen from the available list above based on the task's needs). If a task requires a capability you lack, assign a task to write and register a new tool to Pexo's Genesis Engine."
        )
        
        return {
            "current_agent": "Supervisor",
            "current_instruction": instruction,
            "waiting_for_ai": True,
            "user_profile": prof_text,
            "available_agents": agent_text,
            "available_tools": tool_text
        }
    return {"waiting_for_ai": False}

def developer_node(state: PexoState):
    tasks = state.get("tasks", [])
    completed = state.get("completed_tasks", [])
    if len(completed) < len(tasks):
        next_task = tasks[len(completed)]
        assigned = next_task.get('assigned_agent', 'Developer')
        
        instruction = (
            f"You must now ACT AS THE: {assigned}\n\n"
            f"--- TASK TO EXECUTE ---\n"
            f"Task ID: {next_task.get('id', 'unknown')}\n"
            f"Description: {next_task.get('description', 'No description provided')}\n\n"
            f"--- ENFORCED USER PROFILE ---\n"
            f"{state.get('user_profile', 'None')}\n\n"
            f"ACTION REQUIRED:\n"
            f"Execute this task immediately on the local system. Once done, return your code, findings, or proof of execution."
        )
        
        return {
            "current_agent": assigned,
            "current_instruction": instruction,
            "waiting_for_ai": True
        }
    return {"waiting_for_ai": False}

def manager_node(state: PexoState):
    if state.get("current_agent") != "Code Organization Manager":
        instruction = (
            f"You are the CODE ORGANIZATION MANAGER.\n\n"
            f"--- OBJECTIVE ---\n"
            f"Review all completed tasks from the worker agents and ensure they perfectly match the user's constraints:\n"
            f"{state.get('user_profile', 'None')}\n\n"
            f"ACTION REQUIRED:\n"
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
