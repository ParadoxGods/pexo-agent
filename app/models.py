from sqlalchemy import Column, Integer, String, Text, DateTime, JSON, Boolean
from sqlalchemy.sql import func
from .database import Base

class AgentProfile(Base):
    """
    Dynamic registry of agents. Allows users to spin up new customized agents
    forever remembered by Pexo.
    """
    __tablename__ = "agent_profiles"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    role = Column(String) # e.g., "Frontend Developer", "DevSecOps"
    system_prompt = Column(Text)
    capabilities = Column(JSON) # e.g. ["read", "write", "execute"]
    is_core = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class Profile(Base):
    __tablename__ = "profiles"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    personality_prompt = Column(Text)
    scripting_preferences = Column(JSON)
    backup_path = Column(String, nullable=True) # Automated backup directory or network share
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

class SystemSetting(Base):
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True)
    value = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class Memory(Base):
    __tablename__ = "memories"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, index=True)
    content = Column(Text, nullable=False)
    # We remove pgvector here. Embeddings will be handled entirely via the local ChromaDB instance.
    # This table now acts as the relational metadata map for the vector store.
    chroma_id = Column(String, index=True) 
    task_context = Column(String, index=True) # Tracks which task/agent created this memory
    is_compacted = Column(Boolean, default=False) # True if this memory is a high-level summary/compaction
    is_pinned = Column(Boolean, default=False)
    is_archived = Column(Boolean, default=False)
    compacted_into_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class Artifact(Base):
    __tablename__ = "artifacts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    source_type = Column(String, index=True)  # text, upload, local_path
    source_uri = Column(String, nullable=True)
    content_type = Column(String, nullable=True)
    storage_path = Column(String, unique=True)
    extracted_text = Column(Text, nullable=True)
    session_id = Column(String, index=True)
    task_context = Column(String, index=True)
    sha256 = Column(String, index=True)
    size_bytes = Column(Integer, default=0)
    text_extraction_status = Column(String, default="ready")
    details = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class DynamicTool(Base):
    """
    Stores metadata for tools registered through Pexo's local tool system.
    """
    __tablename__ = "dynamic_tools"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    description = Column(Text)
    python_code = Column(Text) # The actual code of the tool
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


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id = Column(String, primary_key=True, index=True)
    title = Column(String, index=True)
    backend = Column(String, index=True)
    workspace_path = Column(String, nullable=True)
    pexo_session_id = Column(String, index=True, nullable=True)
    status = Column(String, default="idle")
    details = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)
    chat_session_id = Column(String, index=True)
    role = Column(String, index=True)
    content = Column(Text, nullable=False)
    details = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
