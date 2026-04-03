from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Boolean
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector
from .database import Base

class Profile(Base):
    __tablename__ = "profiles"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    personality_prompt = Column(Text)
    scripting_preferences = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class Workspace(Base):
    __tablename__ = "workspaces"
    id = Column(Integer, primary_key=True, index=True)
    path = Column(String, unique=True, index=True)
    structure_snapshot = Column(JSON)  # Snapshot of directory tree for fast Context Cost Management
    standards_enforced = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class Memory(Base):
    __tablename__ = "memories"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, index=True)
    content = Column(Text, nullable=False)
    # Using 1536 dimensions as standard for common embeddings (e.g., OpenAI text-embedding-ada-002)
    embedding = Column(Vector(1536))
    task_context = Column(String, index=True) # Tracks which task/agent created this memory
    is_compacted = Column(Boolean, default=False) # True if this memory is a high-level summary/compaction
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class AgentState(Base):
    """
    Persistence table for all agents (Supervisor, Developer, TimeManager, etc.)
    to write their state and findings. This enables Pexo to manage AI workflows securely.
    """
    __tablename__ = "agent_states"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, index=True)
    agent_name = Column(String, index=True) # e.g. "context_cost_manager", "developer"
    status = Column(String) # e.g. "running", "completed", "error", "needs_compaction"
    context_size_tokens = Column(Integer, default=0)
    data = Column(JSON) # The actual findings, generated code, or metric logs
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
