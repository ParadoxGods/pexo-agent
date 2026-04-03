from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..cache import invalidate_surface_caches
from ..database import get_db
from ..models import AgentProfile
from ..orchestration_context import invalidate_session_context_snapshot

router = APIRouter()

class EvolutionRequest(BaseModel):
    agent_name: str
    lesson_learned: str

@router.post("/")
def evolve_agent(request: EvolutionRequest, db: Session = Depends(get_db)):
    """
    THE EPIPHANY COMPONENT: Self-Evolving Agents (RLAIF).
    When an AI finishes a task, if it encountered errors, was corrected by the user,
    or figured out a better way to do something, it posts the 'lesson' here.
    Pexo permanently modifies the Custom Agent's underlying system prompt.
    The agent gets permanently smarter across all future projects.
    """
    db_agent = db.query(AgentProfile).filter(AgentProfile.name == request.agent_name).first()
    if not db_agent:
        raise HTTPException(status_code=404, detail=f"Agent {request.agent_name} not found.")
    
    # Append the newly learned lesson to the agent's permanent system prompt
    evolution_tag = f"\n[EVOLUTION/LESSON LEARNED]: {request.lesson_learned}"
    
    if db_agent.system_prompt:
        db_agent.system_prompt += evolution_tag
    else:
        db_agent.system_prompt = f"Core Role: {db_agent.role}{evolution_tag}"
        
    db.commit()
    db.refresh(db_agent)
    invalidate_surface_caches()
    invalidate_session_context_snapshot()
    
    return {
        "status": "Evolution complete.",
        "message": f"Agent '{request.agent_name}' has permanently learned this lesson and its core prompt has been updated.",
        "new_prompt": db_agent.system_prompt
    }
