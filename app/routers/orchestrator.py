from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import json
import re
import uuid
from typing import Any, Optional

from ..database import get_db
from ..models import AgentState, Memory
from ..agents.graph import PexoState, invoke_pexo_graph
from ..cache import invalidate_telemetry_caches
from ..orchestration_context import build_session_context_snapshot

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


class SimpleContinueRequest(BaseModel):
    session_id: str
    clarification_answer: str | None = None
    result_data: Any | None = None


SPECIFIC_TASK_HINTS = (
    "build",
    "create",
    "design",
    "implement",
    "fix",
    "review",
    "audit",
    "analyze",
    "analyse",
    "refactor",
    "debug",
    "optimize",
    "write",
    "edit",
    "scaffold",
    "generate",
    "develop",
)

TASK_OBJECT_HINTS = (
    "landing page",
    "website",
    "homepage",
    "dashboard",
    "agent",
    "repo",
    "repository",
    "codebase",
    "ui",
    "api",
    "workflow",
    "prompt",
    "tool",
)

VAGUE_TASK_HINTS = (
    "help me with this",
    "work on this",
    "fix it",
    "improve it",
    "make it better",
    "do this",
    "something",
    "stuff",
)


def estimate_context_tokens(payload: Any) -> int:
    serialized = json.dumps(payload, default=str)
    return max(1, len(serialized) // 4)


def build_output_preview(payload: Any, limit: int = 220) -> str:
    preview = json.dumps(payload, default=str)
    if len(preview) > limit:
        return f"{preview[:limit].rstrip()}..."
    return preview


def coerce_final_response(result_data: Any) -> str:
    if isinstance(result_data, str):
        compact = result_data.strip()
        if compact:
            return compact
    if isinstance(result_data, dict):
        for key in ("final_response", "response", "message", "summary"):
            value = str(result_data.get(key) or "").strip()
            if value:
                return value
    preview = build_output_preview(result_data, limit=400).strip()
    return preview or "Task completed."


def _build_initial_state(session_id: str, prompt: str, clarification_question: str, clarification_answer: str) -> PexoState:
    return {
        "session_id": session_id,
        "user_prompt": prompt,
        "clarification_question": clarification_question,
        "clarification_answer": clarification_answer,
        "tasks": [],
        "completed_tasks": [],
        "current_agent": "Supervisor",
        "current_instruction": "",
        "waiting_for_ai": False,
        "final_response": "",
        "user_profile": "",
        "available_agents": "",
        "available_tools": "",
        "context_snapshot": {},
    }


def should_require_clarification(prompt: str) -> bool:
    text = " ".join((prompt or "").strip().lower().split())
    if not text:
        return True

    words = re.findall(r"[a-z0-9']+", text)
    if len(words) <= 3:
        return True

    if any(hint in text for hint in VAGUE_TASK_HINTS):
        return True

    has_action = any(hint in text for hint in SPECIFIC_TASK_HINTS)
    has_object = any(hint in text for hint in TASK_OBJECT_HINTS)
    has_constraint = any(token in text for token in (" with ", " using ", " for ", " in ", " keep it ", " include ", " without "))

    if has_action and (has_object or has_constraint or len(words) >= 7):
        return False

    if text.endswith("?") and not has_action:
        return True

    if text.startswith(("can you help me", "help me ")) and not (has_action and has_object):
        return True

    return not (has_action and (has_object or len(words) >= 6))


def log_agent_state(
    db: Session,
    session_id: str,
    agent_name: str,
    status: str,
    data: dict[str, Any],
) -> None:
    db.add(
        AgentState(
            session_id=session_id,
            agent_name=agent_name,
            status=status,
            context_size_tokens=estimate_context_tokens(data),
            data=data,
        )
    )


def _require_orchestrator_state(db: Session, session_id: str) -> tuple[AgentState, PexoState]:
    db_state = (
        db.query(AgentState)
        .filter(AgentState.session_id == session_id, AgentState.agent_name == "orchestrator")
        .first()
    )
    if not db_state:
        raise HTTPException(status_code=404, detail="Session not found")
    return db_state, db_state.data


def build_simple_user_message(role: str | None) -> str:
    role_messages = {
        "Supervisor": "Pexo is breaking the work into a short task list.",
        "Developer": "Pexo is working through the next implementation step.",
        "Context Manager": "Pexo is gathering the context needed for the next step.",
        "Time Manager": "Pexo is checking timing and sequencing for the work.",
        "Resource Manager": "Pexo is checking what resources are needed next.",
        "Code Organization Manager": "Pexo is organizing the resulting work into a clean structure.",
    }
    if role in role_messages:
        return role_messages[role]
    if role:
        return f"Pexo is ready for the next {role} step."
    return "Pexo is ready for the next step."


def build_simple_task_payload(session_id: str, state: PexoState) -> dict:
    clarification_question = state.get("clarification_question")
    if clarification_question and not state.get("clarification_answer"):
        return {
            "status": "clarification_required",
            "session_id": session_id,
            "response": clarification_question,
            "user_message": clarification_question,
            "question": clarification_question,
        }

    if state.get("final_response"):
        return {
            "status": "complete",
            "session_id": session_id,
            "response": state["final_response"],
            "user_message": state["final_response"],
            "final_response": state["final_response"],
        }

    if state.get("waiting_for_ai"):
        role = state.get("current_agent")
        instruction = state.get("current_instruction")
        user_message = build_simple_user_message(role)
        return {
            "status": "agent_action_required",
            "session_id": session_id,
            "response": user_message,
            "user_message": user_message,
            "role": role,
            "instruction": instruction,
            "agent_instruction": instruction,
        }

    return {
        "status": "processing",
        "session_id": session_id,
        "response": "Pexo is processing the current task graph. Check again shortly.",
        "user_message": "Pexo is processing the current task graph. Check again shortly.",
    }

@router.post("/intake", response_model=ClarificationResponse)
def intake_prompt(request: PromptRequest, db: Session = Depends(get_db)):
    """Step 1 & 2: Intake and Clarification (The 'One-Ask' Rule)"""
    session_id = request.session_id or str(uuid.uuid4())
    clarification_question = (
        f"You asked: '{request.prompt}'. Before assigning the Supervisor and Context Managers, "
        "could you clarify any specific performance constraints or preferred directory structures for this request?"
    )
    
    # Store initial state in AgentState table (acting as graph memory)
    initial_state = _build_initial_state(
        session_id,
        request.prompt,
        clarification_question,
        "",
    )
    initial_state["context_snapshot"] = build_session_context_snapshot(db)
    
    db_state = AgentState(
        session_id=session_id,
        agent_name="orchestrator",
        status="clarification_pending",
        context_size_tokens=estimate_context_tokens(initial_state),
        data=initial_state
    )
    db.add(db_state)
    db.commit()
    invalidate_telemetry_caches()

    return ClarificationResponse(session_id=session_id, clarification_question=clarification_question)

@router.post("/execute")
def execute_plan(request: ExecuteRequest, db: Session = Depends(get_db)):
    """Start the LangGraph orchestrator after clarification."""
    db_state, state = _require_orchestrator_state(db, request.session_id)
    state["clarification_answer"] = request.clarification_answer
    
    # Run the graph to get the first pending task
    new_state = invoke_pexo_graph(state)
    
    db_state.data = new_state
    db_state.status = "running"
    db_state.context_size_tokens = estimate_context_tokens(new_state)
    log_agent_state(
        db,
        request.session_id,
        "orchestrator",
        "graph_started",
        {
            "clarification_answer": request.clarification_answer,
            "next_agent": new_state.get("current_agent"),
            "task_count": len(new_state.get("tasks", [])),
            "waiting_for_ai": new_state.get("waiting_for_ai"),
        },
    )
    db.commit()
    invalidate_telemetry_caches()
    
    return {"status": "Execution started. External AI should poll /orchestrator/next", "session_id": request.session_id}

@router.get("/next")
def get_next_task(session_id: str, db: Session = Depends(get_db)):
    """External AI polls this to find out what role it needs to assume and what task to do."""
    _, state = _require_orchestrator_state(db, session_id)
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
    db_state, state = _require_orchestrator_state(db, result.session_id)
    
    # AI has completed the waiting action
    state["waiting_for_ai"] = False
    
    current_agent = state.get("current_agent")

    if current_agent == "Supervisor":
        # Expecting a list of tasks
        state["tasks"] = result.result_data if isinstance(result.result_data, list) else []
        telemetry_data = {
            "task_count": len(state["tasks"]),
            "output_preview": build_output_preview(result.result_data),
            "result_type": type(result.result_data).__name__,
        }
    elif current_agent not in ["Supervisor", "Code Organization Manager"]:
        # Append completed task (works for 'Developer' or any custom agent like 'DevSecOps')
        tasks = state.get("tasks", [])
        completed = state.get("completed_tasks", [])
        telemetry_data = {
            "output_preview": build_output_preview(result.result_data),
            "result_type": type(result.result_data).__name__,
        }
        if len(completed) < len(tasks):
            current_task = tasks[len(completed)]
            completed.append({"task": current_task, "result": result.result_data})
            state["completed_tasks"] = completed
            telemetry_data["task_id"] = current_task.get("id")
            telemetry_data["task_description"] = current_task.get("description")
            telemetry_data["assigned_agent"] = current_task.get("assigned_agent")
    else:
        telemetry_data = {
            "output_preview": build_output_preview(result.result_data),
            "result_type": type(result.result_data).__name__,
        }
        state = dict(state)
        state["final_response"] = coerce_final_response(result.result_data)
        state["current_instruction"] = ""
        state["waiting_for_ai"] = False

    # Log the AI's specific action in AgentState for persistence tracking
    agent_log = AgentState(
        session_id=result.session_id,
        agent_name=current_agent,
        status="completed",
        context_size_tokens=estimate_context_tokens(result.result_data),
        data={**telemetry_data, "output": result.result_data}
    )
    db.add(agent_log)

    if current_agent == "Code Organization Manager":
        new_state = dict(state)
    else:
        # Resume the LangGraph to compute the next node
        new_state = invoke_pexo_graph(state)
    db_state.data = new_state
    db_state.context_size_tokens = estimate_context_tokens(new_state)
    if new_state.get("final_response"):
        log_agent_state(
            db,
            result.session_id,
            "orchestrator",
            "session_complete",
            {
                "final_response": new_state.get("final_response"),
                "completed_task_count": len(new_state.get("completed_tasks", [])),
            },
        )
    db.commit()
    invalidate_telemetry_caches()
    
    return {"status": "Result accepted. Graph advanced."}


@router.post("/simple/start")
def start_simple_task(request: PromptRequest, db: Session = Depends(get_db)):
    if should_require_clarification(request.prompt):
        clarification = intake_prompt(request, db)
        return {
            "status": "clarification_required",
            "session_id": clarification.session_id,
            "response": clarification.clarification_question,
            "user_message": clarification.clarification_question,
            "question": clarification.clarification_question,
        }

    session_id = request.session_id or str(uuid.uuid4())
    initial_state = _build_initial_state(
        session_id,
        request.prompt,
        "",
        "No extra clarification required.",
    )
    initial_state["context_snapshot"] = build_session_context_snapshot(db)

    db_state = AgentState(
        session_id=session_id,
        agent_name="orchestrator",
        status="running",
        context_size_tokens=estimate_context_tokens(initial_state),
        data=initial_state,
    )
    db.add(db_state)
    db.flush()

    new_state = invoke_pexo_graph(initial_state)
    db_state.data = new_state
    db_state.context_size_tokens = estimate_context_tokens(new_state)
    log_agent_state(
        db,
        session_id,
        "orchestrator",
        "graph_started",
        {
            "auto_started": True,
            "next_agent": new_state.get("current_agent"),
            "task_count": len(new_state.get("tasks", [])),
            "waiting_for_ai": new_state.get("waiting_for_ai"),
        },
    )
    db.commit()
    invalidate_telemetry_caches()
    return build_simple_task_payload(session_id, new_state)


@router.post("/simple/continue")
def continue_simple_task(request: SimpleContinueRequest, db: Session = Depends(get_db)):
    has_clarification = request.clarification_answer is not None
    has_result = request.result_data is not None
    if has_clarification == has_result:
        raise HTTPException(
            status_code=400,
            detail="Provide either clarification_answer or result_data, but not both.",
        )

    if has_clarification:
        execute_plan(ExecuteRequest(session_id=request.session_id, clarification_answer=request.clarification_answer or ""), db)
    else:
        submit_task_result(TaskResult(session_id=request.session_id, result_data=request.result_data), db)

    _, state = _require_orchestrator_state(db, request.session_id)
    return build_simple_task_payload(request.session_id, state)


@router.get("/simple/status")
def get_simple_task_status(session_id: str, db: Session = Depends(get_db)):
    _, state = _require_orchestrator_state(db, session_id)
    return build_simple_task_payload(session_id, state)

@router.post("/memory")
def store_memory(session_id: str, content: str, task_context: str, db: Session = Depends(get_db)):
    """Store context as a memory chunk."""
    # In a full production environment, an embedding model would vectorise `content` here.
    # For now, we store the raw text in the database.
    new_memory = Memory(session_id=session_id, content=content, task_context=task_context)
    db.add(new_memory)
    db.commit()
    return {"status": "Memory stored"}
