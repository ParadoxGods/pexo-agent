from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import chromadb
from chromadb.config import Settings
import uuid

from ..database import get_db
from ..models import Memory

router = APIRouter()

# Initialize local ChromaDB client (stores vectors purely locally in ./chroma_db)
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_or_create_collection(name="pexo_global_memory")

class MemoryStoreRequest(BaseModel):
    session_id: str
    content: str
    task_context: str

class MemorySearchRequest(BaseModel):
    query: str
    n_results: int = 3

@router.post("/store")
def store_memory(request: MemoryStoreRequest, db: Session = Depends(get_db)):
    """
    Stores a memory chunk in both SQLite (metadata) and ChromaDB (vector embeddings).
    This acts as the global, persistent brain across all tasks.
    """
    memory_id = str(uuid.uuid4())
    
    # Store in ChromaDB for semantic search
    collection.add(
        documents=[request.content],
        metadatas=[{"session_id": request.session_id, "task_context": request.task_context}],
        ids=[memory_id]
    )
    
    # Store relational metadata in SQLite
    new_memory = Memory(
        session_id=request.session_id,
        content=request.content,
        chroma_id=memory_id,
        task_context=request.task_context
    )
    db.add(new_memory)
    db.commit()
    
    return {"status": "Memory permanently embedded into Pexo's global brain.", "memory_id": memory_id}

@router.post("/search")
def search_memory(request: MemorySearchRequest):
    """
    Allows the AI to perform a semantic vector search across Pexo's entire history
    to find relevant context, past bug fixes, or user patterns.
    """
    results = collection.query(
        query_texts=[request.query],
        n_results=request.n_results
    )
    
    if not results['documents']:
        return {"results": []}
        
    # Format the results for the AI
    formatted_results = []
    for i in range(len(results['documents'][0])):
        formatted_results.append({
            "content": results['documents'][0][i],
            "metadata": results['metadatas'][0][i],
            "distance": results['distances'][0][i] if 'distances' in results and results['distances'] else None
        })
        
    return {"results": formatted_results}
