from mcp.server.fastmcp import FastMCP
from .database import SessionLocal, init_db
from .models import Profile, AgentProfile
from .routers.backup import run_backup_for_profile
from .routers.memory import MemoryStoreRequest, MemorySearchRequest, store_memory, search_memory
from .routers.evolve import EvolutionRequest, evolve_agent
from .routers.tools import ToolRegistrationRequest, ToolExecutionRequest, execute_tool, register_tool

mcp = FastMCP("Pexo")

@mcp.tool()
def pexo_read_profile() -> str:
    """Reads the user's permanent personality and scripting profile from Pexo's local database."""
    db = SessionLocal()
    try:
        profile = db.query(Profile).filter(Profile.name == "default_user").first()
        agents = db.query(AgentProfile).order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc()).all()
        
        prof_text = "No profile set."
        if profile:
            prof_text = f"Personality: {profile.personality_prompt}\nScripting: {profile.scripting_preferences}"

        core_agents = ", ".join([f"{a.name} (Role: {a.role})" for a in agents if a.is_core]) or "No core agents registered."
        custom_agents = ", ".join([f"{a.name} (Role: {a.role})" for a in agents if not a.is_core]) or "No custom agents registered."

        return (
            f"--- USER PROFILE ---\n{prof_text}\n\n"
            f"--- AVAILABLE CORE AGENTS ---\n{core_agents}\n\n"
            f"--- AVAILABLE CUSTOM AGENTS ---\n{custom_agents}"
        )
    finally:
        db.close()

@mcp.tool()
def pexo_search_memory(query: str, n_results: int = 3) -> str:
    """
    Searches Pexo's Global Vector Brain for past bug fixes, architectural decisions, and code snippets.
    ALWAYS use this before writing new logic.
    """
    db = SessionLocal()
    try:
        req = MemorySearchRequest(query=query, n_results=n_results)
        res = search_memory(req, db)
        return str(res)
    finally:
        db.close()

@mcp.tool()
def pexo_store_memory(content: str, task_context: str) -> str:
    """
    Embeds a completed code snippet, architectural decision, or bug fix into the Global Vector Brain forever.
    """
    db = SessionLocal()
    try:
        req = MemoryStoreRequest(session_id="mcp_session", content=content, task_context=task_context)
        res = store_memory(req, db)
        return str(res)
    finally:
        db.close()

@mcp.tool()
def pexo_evolve_agent(agent_name: str, lesson_learned: str) -> str:
    """
    Permanently mutates the base system prompt of an agent with a new lesson learned (RLAIF).
    """
    db = SessionLocal()
    try:
        req = EvolutionRequest(agent_name=agent_name, lesson_learned=lesson_learned)
        res = evolve_agent(req, db)
        return str(res)
    finally:
        db.close()

@mcp.tool()
def pexo_register_tool(name: str, description: str, python_code: str) -> str:
    """
    THE GENESIS ENGINE: Write a Python script to perform an action you currently lack the ability to do.
    Pexo will assimilate this tool and it can be called via pexo_execute_tool.
    The python_code MUST contain a 'run(**kwargs)' function.
    """
    db = SessionLocal()
    try:
        req = ToolRegistrationRequest(name=name, description=description, python_code=python_code)
        res = register_tool(req, db)
        return str(res)
    finally:
        db.close()

@mcp.tool()
def pexo_execute_tool(tool_name: str, kwargs_json_str: str) -> str:
    """
    Executes a tool previously registered via the Genesis Engine.
    Pass the exact tool_name and a JSON string of kwargs to pass to its run() function.
    """
    import json
    db = SessionLocal()
    try:
        kwargs = json.loads(kwargs_json_str)
        req = ToolExecutionRequest(kwargs=kwargs)
        res = execute_tool(tool_name, req, db)
        return str(res)
    except Exception as e:
        return f"Execution Failed: {str(e)}"
    finally:
        db.close()

@mcp.tool()
def pexo_run_backup() -> str:
    """
    Backs up Pexo's global vector brain, SQLite database, and dynamic tools to the user's configured backup path.
    """
    db = SessionLocal()
    try:
        res = run_backup_for_profile(db)
        return str(res)
    except Exception as e:
        return f"Backup Failed: {str(e)}"
    finally:
        db.close()

def start_mcp_server():
    """Starts the Pexo MCP server over stdio for native AI integration."""
    init_db()
    mcp.run(transport="stdio")
