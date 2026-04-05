from __future__ import annotations

import re
from functools import lru_cache

from sqlalchemy import text

from .database import current_db_path, engine

MEMORY_FTS_TABLE = "memories_fts"
ARTIFACT_FTS_TABLE = "artifacts_fts"
FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@lru_cache(maxsize=16)
def _sqlite_fts_enabled_for_db(db_key: str) -> bool:
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS pexo_fts_probe USING fts5(content)"))
            connection.execute(text("DROP TABLE IF EXISTS pexo_fts_probe"))
        return True
    except Exception:
        return False


def sqlite_fts_enabled() -> bool:
    return _sqlite_fts_enabled_for_db(str(current_db_path()))


def _fts_query(query: str) -> str:
    tokens = [token.lower() for token in FTS_TOKEN_RE.findall(query or "")]
    if not tokens:
        return ""
    return " OR ".join([f"{token}*" for token in tokens])


def ensure_search_indexes() -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_agent_states_session_created "
                "ON agent_states(session_id, created_at DESC, id DESC)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_agent_states_created "
                "ON agent_states(created_at DESC, id DESC)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memories_context_archived_updated "
                "ON memories(task_context, is_archived, updated_at DESC, id DESC)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_artifacts_context_updated "
                "ON artifacts(task_context, updated_at DESC, id DESC)"
            )
        )

        if not sqlite_fts_enabled():
            return

        connection.execute(
            text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {MEMORY_FTS_TABLE} "
                "USING fts5(content, task_context, session_id)"
            )
        )
        connection.execute(
            text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {ARTIFACT_FTS_TABLE} "
                "USING fts5(name, source_uri, task_context, session_id, extracted_text)"
            )
        )

        memory_count = connection.execute(text("SELECT COUNT(*) FROM memories")).scalar() or 0
        memory_fts_count = connection.execute(text(f"SELECT COUNT(*) FROM {MEMORY_FTS_TABLE}")).scalar() or 0
        if memory_count != memory_fts_count:
            rebuild_memory_search_index(connection=connection)

        artifact_count = connection.execute(text("SELECT COUNT(*) FROM artifacts")).scalar() or 0
        artifact_fts_count = connection.execute(text(f"SELECT COUNT(*) FROM {ARTIFACT_FTS_TABLE}")).scalar() or 0
        if artifact_count != artifact_fts_count:
            rebuild_artifact_search_index(connection=connection)


def rebuild_memory_search_index(connection=None) -> None:
    if not sqlite_fts_enabled():
        return

    def _run(conn):
        conn.execute(text(f"DELETE FROM {MEMORY_FTS_TABLE}"))
        conn.execute(
            text(
                f"INSERT INTO {MEMORY_FTS_TABLE}(rowid, content, task_context, session_id) "
                "SELECT id, COALESCE(content, ''), COALESCE(task_context, ''), COALESCE(session_id, '') "
                "FROM memories"
            )
        )

    if connection is not None:
        _run(connection)
        return
    with engine.begin() as conn:
        _run(conn)


def rebuild_artifact_search_index(connection=None) -> None:
    if not sqlite_fts_enabled():
        return

    def _run(conn):
        conn.execute(text(f"DELETE FROM {ARTIFACT_FTS_TABLE}"))
        conn.execute(
            text(
                f"INSERT INTO {ARTIFACT_FTS_TABLE}(rowid, name, source_uri, task_context, session_id, extracted_text) "
                "SELECT id, COALESCE(name, ''), COALESCE(source_uri, ''), COALESCE(task_context, ''), "
                "COALESCE(session_id, ''), COALESCE(extracted_text, '') FROM artifacts"
            )
        )

    if connection is not None:
        _run(connection)
        return
    with engine.begin() as conn:
        _run(conn)


def upsert_memory_search_document(memory_id: int, *, content: str, task_context: str | None, session_id: str | None, connection=None) -> None:
    if not sqlite_fts_enabled():
        return

    def _run(conn):
        conn.execute(text(f"DELETE FROM {MEMORY_FTS_TABLE} WHERE rowid = :rowid"), {"rowid": memory_id})
        conn.execute(
            text(
                f"INSERT INTO {MEMORY_FTS_TABLE}(rowid, content, task_context, session_id) "
                "VALUES (:rowid, :content, :task_context, :session_id)"
            ),
            {
                "rowid": memory_id,
                "content": content or "",
                "task_context": task_context or "",
                "session_id": session_id or "",
            },
        )

    if connection is not None:
        _run(connection)
        return
    with engine.begin() as connection:
        _run(connection)


def delete_memory_search_document(memory_id: int, connection=None) -> None:
    if not sqlite_fts_enabled():
        return
    if connection is not None:
        connection.execute(text(f"DELETE FROM {MEMORY_FTS_TABLE} WHERE rowid = :rowid"), {"rowid": memory_id})
        return
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM {MEMORY_FTS_TABLE} WHERE rowid = :rowid"), {"rowid": memory_id})


def upsert_artifact_search_document(
    artifact_id: int,
    *,
    name: str,
    source_uri: str | None,
    task_context: str | None,
    session_id: str | None,
    extracted_text: str | None,
    connection=None
) -> None:
    if not sqlite_fts_enabled():
        return

    def _run(conn):
        conn.execute(text(f"DELETE FROM {ARTIFACT_FTS_TABLE} WHERE rowid = :rowid"), {"rowid": artifact_id})
        conn.execute(
            text(
                f"INSERT INTO {ARTIFACT_FTS_TABLE}(rowid, name, source_uri, task_context, session_id, extracted_text) "
                "VALUES (:rowid, :name, :source_uri, :task_context, :session_id, :extracted_text)"
            ),
            {
                "rowid": artifact_id,
                "name": name or "",
                "source_uri": source_uri or "",
                "task_context": task_context or "",
                "session_id": session_id or "",
                "extracted_text": extracted_text or "",
            },
        )

    if connection is not None:
        _run(connection)
        return
    with engine.begin() as connection:
        _run(connection)


def delete_artifact_search_document(artifact_id: int) -> None:
    if not sqlite_fts_enabled():
        return
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM {ARTIFACT_FTS_TABLE} WHERE rowid = :rowid"), {"rowid": artifact_id})


def search_memory_ids(query: str, limit: int) -> list[int]:
    if not sqlite_fts_enabled():
        return []
    compiled_query = _fts_query(query)
    if not compiled_query:
        return []
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT rowid FROM {MEMORY_FTS_TABLE} "
                f"WHERE {MEMORY_FTS_TABLE} MATCH :query "
                "ORDER BY bm25(memories_fts) ASC, rowid DESC LIMIT :limit"
            ),
            {"query": compiled_query, "limit": max(1, min(limit, 100))},
        ).all()
    return [int(row[0]) for row in rows]


def search_artifact_ids(query: str, limit: int) -> list[int]:
    if not sqlite_fts_enabled():
        return []
    compiled_query = _fts_query(query)
    if not compiled_query:
        return []
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT rowid FROM {ARTIFACT_FTS_TABLE} "
                f"WHERE {ARTIFACT_FTS_TABLE} MATCH :query "
                "ORDER BY bm25(artifacts_fts) ASC, rowid DESC LIMIT :limit"
            ),
            {"query": compiled_query, "limit": max(1, min(limit, 100))},
        ).all()
    return [int(row[0]) for row in rows]


def reset_search_index_runtime() -> None:
    _sqlite_fts_enabled_for_db.cache_clear()
