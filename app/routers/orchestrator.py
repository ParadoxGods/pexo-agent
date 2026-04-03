from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import uuid
from typing import Any, Optional

from ..database import get_db
from ..models import AgentState, Memory
from ..agents.graph import pexo_app, PexoState

router = APIRouter()

class PromptRequest(BaseModel):
    user_id: str = "default_user"
    prompt: str
    session_id: Optional[str] = None

class ClarificationResponse(BaseModel):
    session_id: str
    clarification_question: str

class ExecuteRequest(BaseModel):
    session_id: str
    clarification_answer: str

class TaskResult(BaseModel):
    session_id: str
    result_data: Any

@router.post("/intake", response_model=ClarificationResponse)
def intake_prompt(request: PromptRequest, db: Session = Depends(get_db)):
    """Step 1 & 2: Intake and Clarification (The 'One-Ask' Rule)"""
    session_id = request.session_id or str(uuid.uuid4())
    
    # Store initial state in AgentState table (acting as graph memory)
    initial_state = {
        "session_id": session_id,
        "user_prompt": request.prompt,
        "clarification_answer": "",
        "tasks": [],
        "completed_tasks": [],
        "current_agent": "Supervisor",
        "current_instruction": "",
        "waiting_for_ai": False,
        "final_response": "",
        "user_profile": "",
        "available_agents": "",
        "available_tools": "",
    }
    
    db_state = AgentState(
        session_id=session_id,
        agent_name="orchestrator",
        status="clarification_pending",
        data=initial_state
    )
    db.add(db_state)
    db.commit()

    return ClarificationResponse(
        session_id=session_id,
        clarification_question=f"You asked: '{request.prompt}'. Before assigning the Supervisor and Context Managers, could you clarify any specific performance constraints or preferred directory structures for this request?"
    )

@router.post("/execute")
def execute_plan(request: ExecuteRequest, db: Session = Depends(get_db)):
    """Start the LangGraph orchestrator after clarification."""
    db_state = db.query(AgentState).filter(AgentState.session_id == request.session_id, AgentState.agent_name == "orchestrator").first()
    if not db_state:
        raise HTTPException(status_code=404, detail="Session not found")
        
    state: PexoState = db_state.data
    state["clarification_answer"] = request.clarification_answer
    
    # Run the graph to get the first pending task
    new_state = pexo_app.invoke(state)
    
    db_state.data = new_state
    db_state.status = "running"
    db.commit()
    
    return {"status": "Execution started. External AI should poll /orchestrator/next", "session_id": request.session_id}

@router.get("/next")
def get_next_task(session_id: str, db: Session = Depends(get_db)):
    """External AI polls this to find out what role it needs to assume and what task to do."""
    db_state = db.query(AgentState).filter(AgentState.session_id == session_id, AgentState.agent_name == "orchestrator").first()
    if not db_state:
        raise HTTPException(status_code=404, detail="Session not found")
        
    state: PexoState = db_state.data
    if state.get("final_response"):
        return {"status": "complete", "message": state["final_response"]}
        
    if state.get("waiting_for_ai"):
        return {
            "status": "pending_action",
            "role": state.get("current_agent"),
            "instruction": state.get("current_instruction")
        }
    
    return {"status": "processing", "message": "Graph is transitioning. Poll again."}

@router.post("/submit")
def submit_task_result(result: TaskResult, db: Session = Depends(get_db)):
    """External AI posts the result of its task here, which resumes the LangGraph."""
    db_state = db.query(AgentState).filter(AgentState.session_id == result.session_id, AgentState.agent_name == "orchestrator").first()
    if not db_state:
        raise HTTPException(status_code=404, detail="Session not found")
        
    state: PexoState = db_state.data
    
    # AI has completed the waiting action
    state["waiting_for_ai"] = False
    
    if state["current_agent"] == "Supervisor":
        # Expecting a list of tasks
        state["tasks"] = result.result_data if isinstance(result.result_data, list) else []
    elif state["current_agent"] not in ["Supervisor", "Code Organization Manager"]:
        # Append completed task (works for 'Developer' or any custom agent like 'DevSecOps')
        tasks = state.get("tasks", [])
        completed = state.get("completed_tasks", [])
        if len(completed) < len(tasks):
            completed.append({"task": tasks[len(completed)], "result": result.result_data})
            state["completed_tasks"] = completed

    # Log the AI's specific action in AgentState for persistence tracking
    agent_log = AgentState(
        session_id=result.session_id,
        agent_name=state["current_agent"],
        status="completed",
        data={"output": result.result_data}
    )
    db.add(agent_log)
    
    # Resume the LangGraph to compute the next node
    new_state = pexo_app.invoke(state)
    db_state.data = new_state
    db.commit()
    
    return {"status": "Result accepted. Graph advanced."}

@router.post("/memory")
def store_memory(session_id: str, content: str, task_context: str, db: Session = Depends(get_db)):
    """Store context as a memory chunk."""
    # In a full production environment, an embedding model would vectorise `content` here.
    # For now, we store the raw text in the database.
    new_memory = Memory(session_id=session_id, content=content, task_context=task_context)
    db.add(new_memory)
    db.commit()
    return {"status": "Memory stored"}
