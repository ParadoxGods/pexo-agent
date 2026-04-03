from datetime import datetime
import uuid

import chromadb
from chromadb.config import Settings
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Memory
from ..paths import CHROMA_DB_DIR

router = APIRouter()

_memory_collection = None


def get_memory_collection():
    global _memory_collection
    if _memory_collection is None:
        chroma_client = chromadb.PersistentClient(
            path=str(CHROMA_DB_DIR),
            settings=Settings(anonymized_telemetry=False),
        )
        _memory_collection = chroma_client.get_or_create_collection(name="pexo_global_memory")
    return _memory_collection


def serialize_memory(memory: Memory) -> dict:
    return {
        "id": memory.id,
        "session_id": memory.session_id,
        "content": memory.content,
        "task_context": memory.task_context,
        "chroma_id": memory.chroma_id,
        "is_compacted": bool(memory.is_compacted),
        "created_at": memory.created_at.isoformat() if isinstance(memory.created_at, datetime) else memory.created_at,
    }


class MemoryStoreRequest(BaseModel):
    session_id: str
    content: str
    task_context: str


class MemorySearchRequest(BaseModel):
    query: str
    n_results: int = 3


class MemoryUpdateRequest(BaseModel):
    content: str
    task_context: str
    is_compacted: bool = False


@router.post("/store")
def store_memory(request: MemoryStoreRequest, db: Session = Depends(get_db)):
    """
    Stores a memory chunk in both SQLite (metadata) and ChromaDB (vector embeddings).
    This acts as the global, persistent brain across all tasks.
    """
    memory_id = str(uuid.uuid4())

    try:
        get_memory_collection().upsert(
            documents=[request.content],
            metadatas=[{"session_id": request.session_id, "task_context": request.task_context}],
            ids=[memory_id],
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write memory to ChromaDB: {str(exc)}")

    new_memory = Memory(
        session_id=request.session_id,
        content=request.content,
        chroma_id=memory_id,
        task_context=request.task_context,
    )
    db.add(new_memory)
    db.commit()
    db.refresh(new_memory)

    return {
        "status": "Memory permanently embedded into Pexo's global brain.",
        "memory_id": new_memory.id,
        "chroma_id": memory_id,
    }


@router.post("/search")
def search_memory(request: MemorySearchRequest, db: Session = Depends(get_db)):
    """
    Allows the AI to perform a semantic vector search across Pexo's entire history
    to find relevant context, past bug fixes, or user patterns.
    """
    results = get_memory_collection().query(
        query_texts=[request.query],
        n_results=request.n_results,
    )

    documents = results.get("documents") or []
    ids = results.get("ids") or []
    metadatas = results.get("metadatas") or []
    distances = results.get("distances") or []
    if not documents or not documents[0]:
        return {"results": []}

    chroma_ids = ids[0] if ids else []
    memory_records = (
        db.query(Memory).filter(Memory.chroma_id.in_(chroma_ids)).all()
        if chroma_ids
        else []
    )
    memory_map = {record.chroma_id: record for record in memory_records}

    formatted_results = []
    for index, document in enumerate(documents[0]):
        chroma_id = chroma_ids[index] if index < len(chroma_ids) else None
        memory_record = memory_map.get(chroma_id)
        metadata = metadatas[0][index] if metadatas and metadatas[0] else {}
        formatted_results.append(
            {
                "memory_id": memory_record.id if memory_record else None,
                "content": document,
                "metadata": metadata,
                "distance": distances[0][index] if distances and distances[0] else None,
                "created_at": memory_record.created_at.isoformat() if memory_record and memory_record.created_at else None,
                "is_compacted": bool(memory_record.is_compacted) if memory_record else False,
            }
        )

    return {"results": formatted_results}


@router.get("/recent")
def list_recent_memories(limit: int = 12, db: Session = Depends(get_db)):
    safe_limit = max(1, min(limit, 100))
    memories = db.query(Memory).order_by(Memory.created_at.desc()).limit(safe_limit).all()
    return {"memories": [serialize_memory(memory) for memory in memories]}


@router.get("/{memory_id}")
def get_memory(memory_id: int, db: Session = Depends(get_db)):
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    return serialize_memory(memory)


@router.put("/{memory_id}")
def update_memory(memory_id: int, request: MemoryUpdateRequest, db: Session = Depends(get_db)):
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    memory.content = request.content
    memory.task_context = request.task_context
    memory.is_compacted = request.is_compacted

    if memory.chroma_id:
        try:
            get_memory_collection().upsert(
                ids=[memory.chroma_id],
                documents=[request.content],
                metadatas=[{"session_id": memory.session_id, "task_context": request.task_context}],
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to update ChromaDB memory: {str(exc)}")

    db.commit()
    db.refresh(memory)
    return {"status": "success", "memory": serialize_memory(memory)}


@router.delete("/{memory_id}")
def delete_memory(memory_id: int, db: Session = Depends(get_db)):
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    if memory.chroma_id:
        try:
            get_memory_collection().delete(ids=[memory.chroma_id])
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to delete ChromaDB memory: {str(exc)}")

    db.delete(memory)
    db.commit()
    return {"status": "success", "message": f"Memory {memory_id} deleted successfully"}
