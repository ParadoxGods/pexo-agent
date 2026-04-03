from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel, Field
from ..database import get_db
from ..models import AgentProfile

router = APIRouter()

class AgentCreate(BaseModel):
    name: str
    role: str
    system_prompt: str
    capabilities: list[str] = Field(default_factory=list)

class AgentResponse(AgentCreate):
    id: int
    is_core: bool

    class Config:
        from_attributes = True

@router.post("/", response_model=AgentResponse)
def create_agent(agent: AgentCreate, db: Session = Depends(get_db)):
    """Creates and persists a new custom agent into Pexo's memory."""
    db_agent = db.query(AgentProfile).filter(AgentProfile.name == agent.name).first()
    if db_agent:
        raise HTTPException(status_code=400, detail="Agent name already registered")
    new_agent = AgentProfile(**agent.model_dump())
    db.add(new_agent)
    db.commit()
    db.refresh(new_agent)
    return new_agent

@router.get("/", response_model=List[AgentResponse])
def list_agents(db: Session = Depends(get_db)):
    """Lists all agents available in Pexo, both core and dynamically created."""
    return db.query(AgentProfile).all()

@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: int, db: Session = Depends(get_db)):
    db_agent = db.query(AgentProfile).filter(AgentProfile.id == agent_id).first()
    if not db_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return db_agent

@router.put("/{agent_id}", response_model=AgentResponse)
def update_agent(agent_id: int, agent: AgentCreate, db: Session = Depends(get_db)):
    """Edits an existing agent's prompt, role, or capabilities."""
    db_agent = db.query(AgentProfile).filter(AgentProfile.id == agent_id).first()
    if not db_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    for key, value in agent.model_dump().items():
        setattr(db_agent, key, value)
        
    db.commit()
    db.refresh(db_agent)
    return db_agent

@router.delete("/{agent_id}")
def delete_agent(agent_id: int, db: Session = Depends(get_db)):
    """Deletes a custom agent from Pexo (Core agents cannot be deleted)."""
    db_agent = db.query(AgentProfile).filter(AgentProfile.id == agent_id).first()
    if not db_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if db_agent.is_core:
        raise HTTPException(status_code=400, detail="Cannot delete core agents")
    db.delete(db_agent)
    db.commit()
    return {"status": "success", "message": f"Agent {db_agent.name} deleted successfully"}
