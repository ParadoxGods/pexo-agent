from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import os

from ..database import get_db
from ..models import DynamicTool

router = APIRouter()

class ToolRegistrationRequest(BaseModel):
    name: str
    description: str
    python_code: str

@router.post("/register")
def register_tool(request: ToolRegistrationRequest, db: Session = Depends(get_db)):
    """
    THE GENESIS ENGINE: Allows the AI swarm to write and register its own tools.
    If the AI needs a specific API integration or data processing capability,
    it writes the Python script and POSTs it here. Pexo dynamically exposes it.
    """
    # Check if tool exists
    existing_tool = db.query(DynamicTool).filter(DynamicTool.name == request.name).first()
    if existing_tool:
        raise HTTPException(status_code=400, detail=f"Tool '{request.name}' already exists.")
    
    # Save the physical python file to the dynamic_tools folder
    tool_filename = f"app/dynamic_tools/{request.name}.py"
    try:
        with open(tool_filename, "w", encoding="utf-8") as f:
            f.write(request.python_code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write tool to disk: {str(e)}")

    # Register metadata in the SQLite DB
    new_tool = DynamicTool(
        name=request.name,
        description=request.description,
        python_code=request.python_code
    )
    db.add(new_tool)
    db.commit()
    db.refresh(new_tool)
    
    return {
        "status": "Success. Genesis Engine has assimilated the new tool.",
        "message": f"Tool '{request.name}' is now permanently available to all agents.",
        "tool_id": new_tool.id
    }

@router.get("/")
def list_tools(db: Session = Depends(get_db)):
    """Returns a list of all dynamically generated tools."""
    tools = db.query(DynamicTool).all()
    return [{"name": t.name, "description": t.description} for t in tools]
