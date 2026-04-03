from fastapi import FastAPI
from contextlib import asynccontextmanager
from .database import init_db
from .routers import agents, profile, orchestrator

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize the database and pgvector extension on startup
    init_db()
    yield

app = FastAPI(title="Pexo - Primary EXecution Officer", lifespan=lifespan)

# Include dynamic agents CRUD endpoints
app.include_router(agents.router, prefix="/agents", tags=["Agents"])
# Include user profile and onboarding endpoints
app.include_router(profile.router, prefix="/profile", tags=["Profile"])
# Include the main orchestrator LangGraph API
app.include_router(orchestrator.router, prefix="/orchestrator", tags=["Orchestrator"])
