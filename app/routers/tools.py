from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import os
import sys
import importlib.util

from ..database import get_db
from ..models import DynamicTool

router = APIRouter()

class ToolRegistrationRequest(BaseModel):
    name: str
    description: str
    python_code: str

class ToolExecutionRequest(BaseModel):
    kwargs: dict = {}

@router.post("/execute/{tool_name}")
def execute_tool(tool_name: str, request: ToolExecutionRequest, db: Session = Depends(get_db)):
    """
    Executes a dynamically created tool using importlib.
    The tool script must define a function named 'run' that accepts **kwargs.
    """
    tool = db.query(DynamicTool).filter(DynamicTool.name == tool_name).first()
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found.")
        
    tool_path = f"app/dynamic_tools/{tool_name}.py"
    if not os.path.exists(tool_path):
        raise HTTPException(status_code=500, detail=f"Tool file missing from disk: {tool_path}")
        
    try:
        # Dynamically load the module
        spec = importlib.util.spec_from_file_location(tool_name, tool_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[tool_name] = module
        spec.loader.exec_module(module)
        
        # Ensure it has a 'run' function
        if not hasattr(module, 'run'):
            raise HTTPException(status_code=400, detail=f"Tool '{tool_name}' does not implement a 'run' function.")
            
        # Execute the tool
        result = module.run(**request.kwargs)
        return {"status": "success", "result": result}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool execution failed: {str(e)}")

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
