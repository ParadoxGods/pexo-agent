from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
import os
import webbrowser
import threading
import time
from .database import init_db
from .paths import STATIC_DIR
from .routers import admin, agents, artifacts, profile, orchestrator, memory, evolve, runtime, tools, backup

def open_browser():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:9999/ui/")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize local persistence before serving requests.
    init_db()
    if os.environ.get("PEXO_NO_BROWSER") != "1":
        threading.Thread(target=open_browser, daemon=True).start()
    yield

app = FastAPI(title="Pexo - Primary EXecution Operator", lifespan=lifespan)

# Mount static files for the local web UI.
app.mount("/ui", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

@app.get("/")
def redirect_to_ui():
    return RedirectResponse(url="/ui/")

# Include dynamic agents CRUD endpoints
app.include_router(agents.router, prefix="/agents", tags=["Agents"])
# Include user profile and onboarding endpoints
app.include_router(profile.router, prefix="/profile", tags=["Profile"])
# Include the main orchestrator LangGraph API
app.include_router(orchestrator.router, prefix="/orchestrator", tags=["Orchestrator"])
# Include the True Vector Memory API
app.include_router(memory.router, prefix="/memory", tags=["Global Memory"])
# Include the Self-Evolving Agents API
app.include_router(evolve.router, prefix="/evolve", tags=["Evolution"])
# Include the Genesis Engine (Dynamic Tool Creation)
app.include_router(tools.router, prefix="/tools", tags=["Genesis Engine"])
# Include Automated Backup API
app.include_router(backup.router, prefix="/backup", tags=["System Backup"])
# Include runtime status and promotion API
app.include_router(runtime.router, prefix="/runtime", tags=["Runtime"])
# Include local artifact/attachment API
app.include_router(artifacts.router, prefix="/artifacts", tags=["Artifacts"])
# Include admin dashboard snapshot API
app.include_router(admin.router, prefix="/admin", tags=["Admin"])
