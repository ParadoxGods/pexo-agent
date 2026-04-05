import importlib
from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..cache import invalidate_surface_caches
from ..database import get_db, SessionLocal
from ..models import Memory
from ..paths import CHROMA_DB_DIR
from ..runtime import build_runtime_status, build_vector_promotion_offer, maybe_issue_vector_promotion_offer, promote_runtime
from ..search_index import delete_memory_search_document, search_memory_ids, upsert_memory_search_document
from ..direct_chat import run_direct_chat_backend, _adaptive_backend_order

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


def refresh_memory_runtime() -> bool:
    global chromadb, Settings, _memory_collection
    if chromadb is not None and Settings is not None:
        return True
    try:
        chromadb = importlib.import_module("chromadb")
        Settings = importlib.import_module("chromadb.config").Settings
        _memory_collection = None
        return True
    except ImportError:
        chromadb = None
        Settings = None
        _memory_collection = None
        return False


def memory_embeddings_enabled() -> bool:
    return refresh_memory_runtime()


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
    if not fragments:
        return f"Compacted memory summary for {task_context or 'general context'}."
    
    bullet_block = "\n".join([f"- {fragment}" for fragment in fragments])
    prompt = (
        f"Summarize the following memory fragments into a dense, semantic summary for {task_context or 'general context'}. "
        f"Keep it extremely concise and factual. Do not include conversational filler.\n\n{bullet_block}"
    )
    
    for backend in _adaptive_backend_order():
        try:
            summary = run_direct_chat_backend(backend, prompt, workspace_path=".", timeout_seconds=15)
            if summary and "Error:" not in summary:
                return summary.strip()
        except Exception:
            continue

    # Fallback to simple truncation
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
    bullet_block_fallback = "\n".join([f"- {fragment}" for fragment in unique_fragments])
    return f"{header}\n{bullet_block_fallback}"


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


def _with_runtime_metadata(
    payload: dict,
    db: Session,
    *,
    promotion_offer: dict | None = None,
    promotion_result: dict | None = None,
) -> dict:
    enriched = dict(payload)
    enriched["runtime"] = build_runtime_status(db)
    if promotion_offer is not None:
        enriched["promotion_offer"] = promotion_offer
    if promotion_result is not None:
        enriched["promotion_result"] = promotion_result
    return enriched


def _resolve_vector_runtime(
    db: Session,
    *,
    auto_promote_vector: bool = False,
) -> tuple[dict | None, dict | None]:
    promotion_offer = None
    promotion_result = None

    if memory_embeddings_enabled():
        return promotion_offer, promotion_result

    promotion_offer = maybe_issue_vector_promotion_offer(db) or build_vector_promotion_offer()
    if auto_promote_vector:
        promotion_result = promote_runtime("vector")
        if promotion_result["status"] == "success":
            refresh_memory_runtime()
            promotion_offer = None

    return promotion_offer, promotion_result


def _search_memories_without_embeddings(request: "MemorySearchRequest", db: Session) -> dict:
    fts_memory_ids = search_memory_ids(request.query, request.n_results)
    if fts_memory_ids:
        matched_records = db.query(Memory).filter(Memory.id.in_(fts_memory_ids)).all()
        memory_by_id = {memory.id: memory for memory in matched_records}
        ordered_records = [memory_by_id[memory_id] for memory_id in fts_memory_ids if memory_id in memory_by_id]
        metadata_by_id = {
            memory.id: {
                "session_id": memory.session_id,
                "task_context": memory.task_context,
                "search_mode": "keyword_fallback",
                "retrieval_backend": "sqlite_fts",
            }
            for memory in ordered_records
        }
        return _format_memory_search_results(ordered_records, metadata_by_id=metadata_by_id)

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

    archived_memory_ids: list[int] = []
    for memory in source_memories:
        memory.is_archived = True
        memory.compacted_into_id = summary_memory.id
        archived_memory_ids.append(memory.id)

    _delete_memory_embeddings([memory.chroma_id for memory in source_memories])
    db.commit()
    db.refresh(summary_memory)
    upsert_memory_search_document(
        summary_memory.id,
        content=summary_memory.content,
        task_context=summary_memory.task_context,
        session_id=summary_memory.session_id,
    )
    for memory_id in archived_memory_ids:
        delete_memory_search_document(memory_id)
    invalidate_surface_caches()
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
    stale_memory_ids = [memory.id for memory in stale_memories]
    for memory in stale_memories:
        memory.is_archived = True

    _delete_memory_embeddings([memory.chroma_id for memory in stale_memories])
    db.commit()
    for memory_id in stale_memory_ids:
        delete_memory_search_document(memory_id)
    invalidate_surface_caches()
    return len(stale_memories)


def normalize_for_likeness(text: str) -> str:
    """
    Normalizes text to detect "like-words" by stripping punctuation, 
    lowercasing, and sorting unique words.
    """
    import re
    if not text:
        return ""
    # Strip everything but alphanumeric, then split into unique sorted words
    words = re.sub(r'[^\w\s]', '', text.lower()).split()
    return " ".join(sorted(list(set(words))))


def find_semantic_duplicates(db: Session, similarity_threshold: float = 0.65) -> list[list[int]]:
    """
    Scans the memory collection for near-duplicate entries using both 
    word-set normalization and vector similarity.
    """
    collection = get_memory_collection()
    if collection is None:
        return []

    # Efficiency: Scan larger batches for the "Cogmachine"
    candidates = (
        db.query(Memory)
        .filter(Memory.is_archived.is_(False), Memory.is_compacted.is_(False))
        .order_by(Memory.created_at.desc())
        .limit(500)
        .all()
    )
    if not candidates:
        return []

    clusters = []
    processed_ids = set()
    
    # Pre-compute normalized versions for fast "like-word" lookup
    normalized_map = {m.id: normalize_for_likeness(m.content) for m in candidates}

    for memory in candidates:
        if memory.id in processed_ids:
            continue

        cluster = [memory.id]
        processed_ids.add(memory.id)
        
        # 1. Immediate "Like-Word" Match (Lexical Redundancy)
        norm_content = normalized_map.get(memory.id)
        for other_id, other_norm in normalized_map.items():
            if other_id == memory.id or other_id in processed_ids:
                continue
            if norm_content == other_norm:
                cluster.append(other_id)
                processed_ids.add(other_id)

        # 2. Semantic Likeness (Vector Overlap)
        if memory.chroma_id:
            try:
                # Query more results to catch wider "likeness" clusters
                results = collection.query(
                    query_texts=[memory.content],
                    n_results=15,
                    include=["distances"]
                )
                if results and results["ids"] and results["distances"]:
                    # threshold 0.35 distance corresponds to 0.65 similarity (very broad paraphrasing)
                    for i, other_chroma_id in enumerate(results["ids"][0]):
                        distance = results["distances"][0][i]
                        if other_chroma_id == memory.chroma_id:
                            continue
                        
                        if distance < (1.0 - similarity_threshold):
                            other_mem = db.query(Memory).filter(Memory.chroma_id == other_chroma_id).first()
                            if other_mem and other_mem.id not in processed_ids:
                                cluster.append(other_mem.id)
                                processed_ids.add(other_mem.id)
            except Exception:
                pass
        
        if len(cluster) > 1:
            clusters.append(cluster)

    return clusters


def merge_memory_cluster(db: Session, memory_ids: list[int]) -> int | None:
    """
    Uses an LLM to consolidate a cluster of similar memories into one 
    ruthlessly efficient, high-density entry.
    """
    memories = db.query(Memory).filter(Memory.id.in_(memory_ids)).all()
    if len(memories) < 2:
        return None

    primary = memories[0]
    content_block = "\n".join([f"- {m.content}" for m in memories])
    
    prompt = (
        "You are the Swarm Efficiency Manager. The following memories are redundant, repetitive, or use 'like-words' "
        "to describe the same result. Your goal is to eliminate all fluff and consolidate them into ONE singular, "
        "high-density factual record. If two memories differ only by phrasing, keep only the most accurate version. "
        "Output ONLY the final merged text.\n\n"
        f"{content_block}"
    )

    merged_content = None
    for backend in _adaptive_backend_order():
        try:
            res = run_direct_chat_backend(backend, prompt, workspace_path=".", timeout_seconds=25)
            if res and "Error:" not in res:
                merged_content = res.strip()
                break
        except Exception:
            continue

    if not merged_content:
        return None

    # Cogmachine logic: If the new content is essentially identical to an existing one, 
    # just pick one instead of creating a new record.
    norm_merged = normalize_for_likeness(merged_content)
    for m in memories:
        if normalize_for_likeness(m.content) == norm_merged:
            # Found an existing one that perfectly captures the merge result. Use it.
            for other_m in memories:
                if other_m.id != m.id:
                    other_m.is_archived = True
                    other_m.compacted_into_id = m.id
            db.commit()
            return m.id

    # Create new high-efficiency record
    new_memory = Memory(
        session_id=primary.session_id,
        task_context=primary.task_context,
        content=merged_content,
        is_compacted=False
    )
    db.add(new_memory)
    db.flush()
    _upsert_memory_embedding(new_memory)

    for m in memories:
        m.is_archived = True
        m.compacted_into_id = new_memory.id
    
    db.commit()
    return new_memory.id


def deduplicate_memories(db: Session) -> dict:
    """
    Sweeps memory to merge duplicates. Returns metrics on efficiency gains.
    """
    clusters = find_semantic_duplicates(db)
    merge_count = 0
    total_chars_before = 0
    total_chars_after = 0
    
    for cluster in clusters:
        # Measure efficiency gain
        mems = db.query(Memory).filter(Memory.id.in_(cluster)).all()
        before = sum(len(m.content) for m in mems)
        
        merged_id = merge_memory_cluster(db, cluster)
        if merged_id:
            merged = db.query(Memory).filter(Memory.id == merged_id).first()
            total_chars_before += before
            total_chars_after += len(merged.content) if merged else 0
            merge_count += 1
            
    return {
        "merges": merge_count,
        "efficiency_gain_chars": total_chars_before - total_chars_after
    }


def maintain_memory_health(db: Session, task_context: str | None = None) -> dict:
    dedup_metrics = deduplicate_memories(db)
    compaction_result = compact_memories_for_context(db, task_context)
    archived_count = apply_memory_retention(db)
    return {
        "deduplicated_clusters": dedup_metrics["merges"],
        "efficiency_gain_chars": dedup_metrics["efficiency_gain_chars"],
        "compacted_count": compaction_result["compacted_count"],
        "summary_memory_id": compaction_result["summary_memory_id"],
        "archived_count": archived_count,
    }


def maintain_memory_health_bg(task_context: str | None = None) -> None:
    db = SessionLocal()
    try:
        maintain_memory_health(db, task_context)
    finally:
        db.close()


def autonomous_memory_cogmachine_loop() -> None:
    """
    Perpetual background loop that ensures memory efficiency.
    """
    import logging
    import time
    
    logger = logging.getLogger("pexo.memory_cogmachine")
    logger.info("Memory Cogmachine started.")
    
    while True:
        try:
            db = SessionLocal()
            try:
                # Proactive sweep of ALL context-less or global memories
                metrics = deduplicate_memories(db)
                dedup_count = metrics.get("merges", 0)
                if dedup_count > 0:
                    logger.info(f"Memory Cogmachine cleaned up {dedup_count} redundant memory clusters.")
                
                # Global retention check
                archived = apply_memory_retention(db)
                if archived > 0:
                    logger.info(f"Memory Cogmachine archived {archived} stale memories.")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Memory Cogmachine error: {e}")
        
        # Sleep for 10 minutes between global sweeps to preserve local resources
        time.sleep(600)


def start_autonomous_memory_cogmachine() -> None:
    """
    Spawns the memory efficiency background thread.
    """
    import threading
    thread = threading.Thread(target=autonomous_memory_cogmachine_loop, daemon=True)
    thread.start()


class MemoryStoreRequest(BaseModel):
    session_id: str
    content: str
    task_context: str
    auto_promote_vector: bool = False


class MemorySearchRequest(BaseModel):
    query: str
    n_results: int = 3
    auto_promote_vector: bool = False


class MemoryUpdateRequest(BaseModel):
    content: str | None = None
    task_context: str | None = None
    is_compacted: bool | None = None
    is_pinned: bool | None = None
    is_archived: bool | None = None


class MemoryMaintenanceRequest(BaseModel):
    task_context: str | None = None


@router.post("/store")
def store_memory(request: MemoryStoreRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Stores a memory chunk in both SQLite (metadata) and ChromaDB (vector embeddings).
    This acts as the global, persistent brain across all tasks.
    """
    promotion_offer, promotion_result = _resolve_vector_runtime(
        db,
        auto_promote_vector=request.auto_promote_vector,
    )

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
    upsert_memory_search_document(
        new_memory.id,
        content=new_memory.content,
        task_context=new_memory.task_context,
        session_id=new_memory.session_id,
    )
    
    background_tasks.add_task(maintain_memory_health_bg, request.task_context)
    invalidate_surface_caches()

    return _with_runtime_metadata({
        "status": "Memory permanently embedded into Pexo's global brain.",
        "memory_id": new_memory.id,
        "chroma_id": new_memory.chroma_id,
        "embedding_mode": "vector" if memory_embeddings_enabled() else "sqlite_keyword_fallback",
        "maintenance": "Deferred to background task",
    }, db, promotion_offer=promotion_offer, promotion_result=promotion_result)


@router.post("/search")
def search_memory(request: MemorySearchRequest, db: Session = Depends(get_db)):
    """
    Allows the AI to perform a semantic vector search across Pexo's entire history
    to find relevant context, past bug fixes, or user patterns.
    """
    promotion_offer, promotion_result = _resolve_vector_runtime(
        db,
        auto_promote_vector=request.auto_promote_vector,
    )

    collection = get_memory_collection()
    if collection is None:
        return _with_runtime_metadata(
            _search_memories_without_embeddings(request, db),
            db,
            promotion_offer=promotion_offer,
            promotion_result=promotion_result,
        )

    results = collection.query(query_texts=[request.query], n_results=request.n_results)

    documents = results.get("documents") or []
    ids = results.get("ids") or []
    metadatas = results.get("metadatas") or []
    distances = results.get("distances") or []
    if not documents or not documents[0]:
        return _with_runtime_metadata(
            {"results": []},
            db,
            promotion_offer=promotion_offer,
            promotion_result=promotion_result,
        )

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

    return _with_runtime_metadata(
        _format_memory_search_results(
            matched_records,
            distance_by_id=distance_by_id,
            metadata_by_id=metadata_by_id,
        ),
        db,
        promotion_offer=promotion_offer,
        promotion_result=promotion_result,
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
    if memory.is_archived:
        delete_memory_search_document(memory.id)
    else:
        upsert_memory_search_document(
            memory.id,
            content=memory.content,
            task_context=memory.task_context,
            session_id=memory.session_id,
        )

    task_contexts_to_maintain = {context for context in [previous_task_context, memory.task_context] if context}
    maintenance = {"compacted_count": 0, "summary_memory_id": None, "archived_count": 0}
    for task_context in task_contexts_to_maintain:
        result = maintain_memory_health(db, task_context=task_context)
        maintenance["compacted_count"] += result["compacted_count"]
        maintenance["archived_count"] += result["archived_count"]
        maintenance["summary_memory_id"] = maintenance["summary_memory_id"] or result["summary_memory_id"]
    invalidate_surface_caches()

    return {"status": "success", "memory": serialize_memory(memory), "maintenance": maintenance}


@router.delete("/{memory_id}")
def delete_memory(memory_id: int, db: Session = Depends(get_db)):
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    _delete_memory_embeddings([memory.chroma_id])
    delete_memory_search_document(memory.id)
    db.delete(memory)
    db.commit()
    invalidate_surface_caches()
    return {"status": "success", "message": f"Memory {memory_id} deleted successfully"}
