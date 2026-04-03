from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import sys
import importlib.util
import re
from pathlib import Path

from ..database import get_db
from ..models import DynamicTool
from ..paths import DYNAMIC_TOOLS_DIR

router = APIRouter()
SAFE_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

class ToolRegistrationRequest(BaseModel):
    name: str
    description: str
    python_code: str

class ToolExecutionRequest(BaseModel):
    kwargs: dict = Field(default_factory=dict)


def validate_tool_name(tool_name: str) -> str:
    if not SAFE_TOOL_NAME_RE.fullmatch(tool_name):
        raise HTTPException(
            status_code=400,
            detail="Tool names must start with a letter or underscore and contain only letters, numbers, and underscores.",
        )
    return tool_name


def resolve_tool_path(tool_name: str, base_dir: Path = DYNAMIC_TOOLS_DIR) -> Path:
    safe_tool_name = validate_tool_name(tool_name)
    return base_dir / f"{safe_tool_name}.py"

@router.post("/execute/{tool_name}")
def execute_tool(tool_name: str, request: ToolExecutionRequest, db: Session = Depends(get_db)):
    """
    Executes a dynamically created tool using importlib.
    The tool script must define a function named 'run' that accepts **kwargs.
    """
    safe_tool_name = validate_tool_name(tool_name)
    tool = db.query(DynamicTool).filter(DynamicTool.name == safe_tool_name).first()
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool '{safe_tool_name}' not found.")
        
    tool_path = resolve_tool_path(safe_tool_name)
    if not tool_path.exists():
        raise HTTPException(status_code=500, detail=f"Tool file missing from disk: {tool_path}")
        
    try:
        # Dynamically load the module
        module_name = f"pexo_dynamic_tool_{safe_tool_name}"
        spec = importlib.util.spec_from_file_location(module_name, tool_path)
        if spec is None or spec.loader is None:
            raise HTTPException(status_code=500, detail=f"Could not load tool '{safe_tool_name}'.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        
        # Ensure it has a 'run' function
        run_callable = getattr(module, "run", None)
        if not callable(run_callable):
            raise HTTPException(status_code=400, detail=f"Tool '{safe_tool_name}' does not implement a callable 'run' function.")
            
        # Execute the tool
        result = run_callable(**request.kwargs)
        return {"status": "success", "result": result}
    except HTTPException:
        raise
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
    safe_tool_name = validate_tool_name(request.name)
    existing_tool = db.query(DynamicTool).filter(DynamicTool.name == safe_tool_name).first()
    if existing_tool:
        raise HTTPException(status_code=400, detail=f"Tool '{safe_tool_name}' already exists.")
    
    # Save the physical python file to the dynamic_tools folder
    DYNAMIC_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    tool_path = resolve_tool_path(safe_tool_name)
    if tool_path.exists():
        raise HTTPException(status_code=400, detail=f"Tool file '{tool_path.name}' already exists on disk.")

    try:
        with tool_path.open("w", encoding="utf-8") as f:
            f.write(request.python_code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write tool to disk: {str(e)}")

    # Register metadata in the SQLite DB
    new_tool = DynamicTool(
        name=safe_tool_name,
        description=request.description,
        python_code=request.python_code
    )
    try:
        db.add(new_tool)
        db.commit()
        db.refresh(new_tool)
    except Exception as e:
        db.rollback()
        tool_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to register tool metadata: {str(e)}")
    
    return {
        "status": "Success. Genesis Engine has assimilated the new tool.",
        "message": f"Tool '{safe_tool_name}' is now permanently available to all agents.",
        "tool_id": new_tool.id
    }

@router.get("/")
def list_tools(db: Session = Depends(get_db)):
    """Returns a list of all dynamically generated tools."""
    tools = db.query(DynamicTool).all()
    return [{"name": t.name, "description": t.description} for t in tools]
