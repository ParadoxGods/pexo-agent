from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Memory
from ..paths import CHROMA_DB_DIR

try:
    import chromadb
    from chromadb.config import Settings
except ImportError:  # pragma: no cover - exercised by dependency-profile smoke paths
    chromadb = None
    Settings = None

router = APIRouter()

MAX_ACTIVE_RAW_MEMORIES_PER_CONTEXT = 6
RAW_MEMORIES_TO_KEEP_PER_CONTEXT = 2
MAX_ACTIVE_RAW_MEMORIES_GLOBAL = 150
SUMMARY_FRAGMENT_LIMIT = 6
SUMMARY_FRAGMENT_LENGTH = 240

_memory_collection = None


def memory_embeddings_enabled() -> bool:
    return chromadb is not None and Settings is not None


def get_memory_collection():
    global _memory_collection
    if not memory_embeddings_enabled():
        return None
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
        "is_pinned": bool(memory.is_pinned),
        "is_archived": bool(memory.is_archived),
        "compacted_into_id": memory.compacted_into_id,
        "created_at": memory.created_at.isoformat() if isinstance(memory.created_at, datetime) else memory.created_at,
        "updated_at": memory.updated_at.isoformat() if isinstance(memory.updated_at, datetime) else memory.updated_at,
    }


def _upsert_memory_embedding(memory: Memory) -> None:
    collection = get_memory_collection()
    if collection is None:
        memory.chroma_id = None
        return
    if not memory.chroma_id:
        memory.chroma_id = str(uuid.uuid4())
    collection.upsert(
        ids=[memory.chroma_id],
        documents=[memory.content],
        metadatas=[{"session_id": memory.session_id, "task_context": memory.task_context}],
    )


def _delete_memory_embeddings(chroma_ids: list[str]) -> None:
    deletable_ids = [chroma_id for chroma_id in chroma_ids if chroma_id]
    collection = get_memory_collection()
    if deletable_ids and collection is not None:
        collection.delete(ids=deletable_ids)


def _summarize_fragment(content: str) -> str:
    compact = " ".join(content.split())
    if len(compact) > SUMMARY_FRAGMENT_LENGTH:
        return f"{compact[:SUMMARY_FRAGMENT_LENGTH].rstrip()}..."
    return compact


def _extract_summary_fragments(content: str) -> list[str]:
    if not content:
        return []
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    bullet_fragments = [line[2:].strip() for line in lines if line.startswith("- ")]
    return bullet_fragments or lines


def _build_compacted_summary(task_context: str, fragments: list[str]) -> str:
    unique_fragments: list[str] = []
    seen = set()
    for fragment in fragments:
        cleaned = _summarize_fragment(fragment)
        if not cleaned:
            continue
        fingerprint = cleaned.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique_fragments.append(cleaned)
        if len(unique_fragments) >= SUMMARY_FRAGMENT_LIMIT:
            break

    header = f"Compacted memory summary for {task_context or 'general context'}."
    if not unique_fragments:
        return header
    bullet_block = "\n".join([f"- {fragment}" for fragment in unique_fragments])
    return f"{header}\n{bullet_block}"


def _format_memory_search_results(memories: list[Memory], *, distance_by_id: dict[int, float | None] | None = None, metadata_by_id: dict[int, dict] | None = None) -> dict:
    formatted_results = []
    for memory in memories:
        if memory.is_archived:
            continue
        formatted_results.append(
            {
                "memory_id": memory.id,
                "content": memory.content,
                "metadata": (metadata_by_id or {}).get(memory.id) or {
                    "session_id": memory.session_id,
                    "task_context": memory.task_context,
                },
                "distance": (distance_by_id or {}).get(memory.id),
                "created_at": memory.created_at.isoformat() if memory.created_at else None,
                "is_compacted": bool(memory.is_compacted),
                "is_pinned": bool(memory.is_pinned),
                "is_archived": bool(memory.is_archived),
            }
        )
    return {"results": formatted_results}


def _search_memories_without_embeddings(request: "MemorySearchRequest", db: Session) -> dict:
    query_text = (request.query or "").strip().lower()
    if not query_text:
        return {"results": []}

    tokens = [token for token in query_text.split() if token]
    recency_order = func.coalesce(Memory.updated_at, Memory.created_at)
    candidates = (
        db.query(Memory)
        .filter(Memory.is_archived.is_(False))
        .order_by(Memory.is_pinned.desc(), recency_order.desc(), Memory.id.desc())
        .limit(250)
        .all()
    )

    scored_candidates: list[tuple[int, datetime | None, Memory]] = []
    for memory in candidates:
        haystack = " ".join(
            [
                memory.content or "",
                memory.task_context or "",
                memory.session_id or "",
            ]
        ).lower()
        score = 0
        if query_text in haystack:
            score += 10
        if tokens:
            score += sum(1 for token in tokens if token in haystack)
        if score <= 0:
            continue
        scored_candidates.append((score, memory.updated_at or memory.created_at, memory))

    scored_candidates.sort(key=lambda item: (item[0], item[1] or datetime.min, item[2].id), reverse=True)
    top_memories = [memory for _, _, memory in scored_candidates[: request.n_results]]
    metadata_by_id = {
        memory.id: {
            "session_id": memory.session_id,
            "task_context": memory.task_context,
            "search_mode": "keyword_fallback",
        }
        for memory in top_memories
    }
    return _format_memory_search_results(top_memories, metadata_by_id=metadata_by_id)


def compact_memories_for_context(db: Session, task_context: str | None) -> dict:
    if not task_context:
        return {"compacted_count": 0, "summary_memory_id": None}

    active_summary = (
        db.query(Memory)
        .filter(
            Memory.task_context == task_context,
            Memory.is_compacted.is_(True),
            Memory.is_archived.is_(False),
        )
        .order_by(Memory.created_at.desc(), Memory.id.desc())
        .first()
    )
    raw_memories = (
        db.query(Memory)
        .filter(
            Memory.task_context == task_context,
            Memory.is_archived.is_(False),
            Memory.is_pinned.is_(False),
            Memory.is_compacted.is_(False),
        )
        .order_by(Memory.created_at.asc(), Memory.id.asc())
        .all()
    )

    if len(raw_memories) <= MAX_ACTIVE_RAW_MEMORIES_PER_CONTEXT:
        return {"compacted_count": 0, "summary_memory_id": active_summary.id if active_summary else None}

    source_memories = raw_memories[:-RAW_MEMORIES_TO_KEEP_PER_CONTEXT]
    summary_fragments = (
        _extract_summary_fragments(active_summary.content) if active_summary else []
    ) + [memory.content for memory in source_memories]
    summary_content = _build_compacted_summary(task_context, summary_fragments)

    if active_summary is None:
        summary_memory = Memory(
            session_id=source_memories[-1].session_id if source_memories else "maintenance",
            content=summary_content,
            task_context=task_context,
            is_compacted=True,
        )
        db.add(summary_memory)
        db.flush()
    else:
        summary_memory = active_summary
        summary_memory.content = summary_content
        summary_memory.is_archived = False

    _upsert_memory_embedding(summary_memory)

    for memory in source_memories:
        memory.is_archived = True
        memory.compacted_into_id = summary_memory.id

    _delete_memory_embeddings([memory.chroma_id for memory in source_memories])
    db.commit()
    db.refresh(summary_memory)
    return {"compacted_count": len(source_memories), "summary_memory_id": summary_memory.id}


def apply_memory_retention(db: Session) -> int:
    active_raw_memories = (
        db.query(Memory)
        .filter(
            Memory.is_archived.is_(False),
            Memory.is_pinned.is_(False),
            Memory.is_compacted.is_(False),
        )
        .order_by(Memory.created_at.desc(), Memory.id.desc())
        .all()
    )
    if len(active_raw_memories) <= MAX_ACTIVE_RAW_MEMORIES_GLOBAL:
        return 0

    stale_memories = active_raw_memories[MAX_ACTIVE_RAW_MEMORIES_GLOBAL:]
    for memory in stale_memories:
        memory.is_archived = True

    _delete_memory_embeddings([memory.chroma_id for memory in stale_memories])
    db.commit()
    return len(stale_memories)


def maintain_memory_health(db: Session, task_context: str | None = None) -> dict:
    compaction_result = compact_memories_for_context(db, task_context)
    archived_count = apply_memory_retention(db)
    return {
        "compacted_count": compaction_result["compacted_count"],
        "summary_memory_id": compaction_result["summary_memory_id"],
        "archived_count": archived_count,
    }


class MemoryStoreRequest(BaseModel):
    session_id: str
    content: str
    task_context: str


class MemorySearchRequest(BaseModel):
    query: str
    n_results: int = 3


class MemoryUpdateRequest(BaseModel):
    content: str | None = None
    task_context: str | None = None
    is_compacted: bool | None = None
    is_pinned: bool | None = None
    is_archived: bool | None = None


class MemoryMaintenanceRequest(BaseModel):
    task_context: str | None = None


@router.post("/store")
def store_memory(request: MemoryStoreRequest, db: Session = Depends(get_db)):
    """
    Stores a memory chunk in both SQLite (metadata) and ChromaDB (vector embeddings).
    This acts as the global, persistent brain across all tasks.
    """
    new_memory = Memory(
        session_id=request.session_id,
        content=request.content,
        task_context=request.task_context,
    )
    db.add(new_memory)
    db.flush()

    try:
        _upsert_memory_embedding(new_memory)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to write memory to ChromaDB: {str(exc)}")

    db.commit()
    db.refresh(new_memory)
    maintenance = maintain_memory_health(db, task_context=request.task_context)

    return {
        "status": "Memory permanently embedded into Pexo's global brain.",
        "memory_id": new_memory.id,
        "chroma_id": new_memory.chroma_id,
        "embedding_mode": "vector" if memory_embeddings_enabled() else "sqlite_keyword_fallback",
        "maintenance": maintenance,
    }


@router.post("/search")
def search_memory(request: MemorySearchRequest, db: Session = Depends(get_db)):
    """
    Allows the AI to perform a semantic vector search across Pexo's entire history
    to find relevant context, past bug fixes, or user patterns.
    """
    collection = get_memory_collection()
    if collection is None:
        return _search_memories_without_embeddings(request, db)

    results = collection.query(query_texts=[request.query], n_results=request.n_results)

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

    matched_records: list[Memory] = []
    distance_by_id: dict[int, float | None] = {}
    metadata_by_id: dict[int, dict] = {}
    for index, document in enumerate(documents[0]):
        chroma_id = chroma_ids[index] if index < len(chroma_ids) else None
        memory_record = memory_map.get(chroma_id)
        if memory_record is None:
            continue
        memory_record.content = document
        matched_records.append(memory_record)
        metadata_by_id[memory_record.id] = metadatas[0][index] if metadatas and metadatas[0] else {}
        distance_by_id[memory_record.id] = distances[0][index] if distances and distances[0] else None

    return _format_memory_search_results(
        matched_records,
        distance_by_id=distance_by_id,
        metadata_by_id=metadata_by_id,
    )


@router.get("/recent")
def list_recent_memories(limit: int = 12, include_archived: bool = True, db: Session = Depends(get_db)):
    safe_limit = max(1, min(limit, 100))
    query = db.query(Memory)
    if not include_archived:
        query = query.filter(Memory.is_archived.is_(False))
    recency_order = func.coalesce(Memory.updated_at, Memory.created_at)
    memories = query.order_by(recency_order.desc(), Memory.id.desc()).limit(safe_limit).all()
    return {"memories": [serialize_memory(memory) for memory in memories]}


@router.post("/maintenance")
def run_memory_maintenance(request: MemoryMaintenanceRequest, db: Session = Depends(get_db)):
    result = maintain_memory_health(db, task_context=request.task_context)
    return {"status": "success", **result}


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

    previous_task_context = memory.task_context

    if request.content is not None:
        memory.content = request.content
    if request.task_context is not None:
        memory.task_context = request.task_context
    if request.is_compacted is not None:
        memory.is_compacted = request.is_compacted
    if request.is_pinned is not None:
        memory.is_pinned = request.is_pinned
    if request.is_archived is not None:
        memory.is_archived = request.is_archived

    try:
        if memory.is_archived:
            _delete_memory_embeddings([memory.chroma_id])
        else:
            _upsert_memory_embedding(memory)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to sync ChromaDB memory: {str(exc)}")

    db.commit()
    db.refresh(memory)

    task_contexts_to_maintain = {context for context in [previous_task_context, memory.task_context] if context}
    maintenance = {"compacted_count": 0, "summary_memory_id": None, "archived_count": 0}
    for task_context in task_contexts_to_maintain:
        result = maintain_memory_health(db, task_context=task_context)
        maintenance["compacted_count"] += result["compacted_count"]
        maintenance["archived_count"] += result["archived_count"]
        maintenance["summary_memory_id"] = maintenance["summary_memory_id"] or result["summary_memory_id"]

    return {"status": "success", "memory": serialize_memory(memory), "maintenance": maintenance}


@router.delete("/{memory_id}")
def delete_memory(memory_id: int, db: Session = Depends(get_db)):
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    _delete_memory_embeddings([memory.chroma_id])
    db.delete(memory)
    db.commit()
    return {"status": "success", "message": f"Memory {memory_id} deleted successfully"}
