from typing import TypedDict, List, Dict, Any, Optional
from langgraph.graph import StateGraph, END

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

def supervisor_node(state: PexoState):
    if not state.get("tasks"):
        return {
            "current_agent": "Supervisor",
            "current_instruction": f"Break down this prompt into discrete tasks. Prompt: {state['user_prompt']}\nClarification: {state['clarification_answer']}. Return a JSON array of tasks with 'id' and 'description'.",
            "waiting_for_ai": True
        }
    return {"waiting_for_ai": False}

def developer_node(state: PexoState):
    tasks = state.get("tasks", [])
    completed = state.get("completed_tasks", [])
    if len(completed) < len(tasks):
        next_task = tasks[len(completed)]
        return {
            "current_agent": "Developer",
            "current_instruction": f"Execute task ID {next_task.get('id', 'unknown')}: {next_task.get('description', 'no description')}",
            "waiting_for_ai": True
        }
    return {"waiting_for_ai": False}

def manager_node(state: PexoState):
    if state.get("current_agent") != "Code Organization Manager":
        return {
            "current_agent": "Code Organization Manager",
            "current_instruction": "Review all completed tasks, ensure they follow the user's scripting profile, and format the final output.",
            "waiting_for_ai": True
        }
    return {"waiting_for_ai": False, "final_response": "All tasks completed and reviewed. Session closed."}

def router(state: PexoState):
    if state.get("waiting_for_ai"):
        return END
    
    if state.get("current_agent") == "Supervisor":
        return "developer"
        
    if state.get("current_agent") == "Developer":
        tasks = state.get("tasks", [])
        completed = state.get("completed_tasks", [])
        if len(completed) < len(tasks):
            return "developer"
        return "manager"
        
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
