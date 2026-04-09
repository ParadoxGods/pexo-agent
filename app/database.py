import importlib
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

from .paths import PEXO_DB_PATH


def current_db_path() -> Path:
    return Path(PEXO_DB_PATH).resolve(strict=False)


def current_database_url() -> str:
    return f"sqlite:///{current_db_path().as_posix()}"


def _connect_current_sqlite():
    db_path = current_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)


engine = create_engine(
    "sqlite://",
    creator=_connect_current_sqlite,
    poolclass=NullPool,
)


def _apply_sqlite_pragmas(cursor) -> None:
    cursor.execute("PRAGMA busy_timeout = 30000")
    for statement in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"):
        try:
            cursor.execute(statement)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    try:
        _apply_sqlite_pragmas(cursor)
    finally:
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
_initialized_db_paths: set[str] = set()


MEMORY_TABLE_MIGRATIONS = {
    "is_pinned": "ALTER TABLE memories ADD COLUMN is_pinned BOOLEAN DEFAULT 0",
    "is_archived": "ALTER TABLE memories ADD COLUMN is_archived BOOLEAN DEFAULT 0",
    "compacted_into_id": "ALTER TABLE memories ADD COLUMN compacted_into_id INTEGER",
    "updated_at": "ALTER TABLE memories ADD COLUMN updated_at DATETIME",
    "memory_fields": "ALTER TABLE memories ADD COLUMN memory_fields JSON",
    "lookup_key": "ALTER TABLE memories ADD COLUMN lookup_key VARCHAR",
    "lookup_value": "ALTER TABLE memories ADD COLUMN lookup_value VARCHAR",
    "artifact_token": "ALTER TABLE memories ADD COLUMN artifact_token VARCHAR",
}

ARTIFACT_TABLE_MIGRATIONS = {
    "text_extraction_status": "ALTER TABLE artifacts ADD COLUMN text_extraction_status VARCHAR DEFAULT 'ready'",
}


def run_schema_migrations() -> None:
    from .search_index import ensure_search_indexes

    inspector = inspect(engine)
    statements = []

    if "memories" in inspector.get_table_names():
        existing_memory_columns = {column["name"] for column in inspector.get_columns("memories")}
        statements.extend(
            ddl
            for column_name, ddl in MEMORY_TABLE_MIGRATIONS.items()
            if column_name not in existing_memory_columns
        )

    if "artifacts" in inspector.get_table_names():
        existing_artifact_columns = {column["name"] for column in inspector.get_columns("artifacts")}
        statements.extend(
            ddl
            for column_name, ddl in ARTIFACT_TABLE_MIGRATIONS.items()
            if column_name not in existing_artifact_columns
        )

    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    ensure_search_indexes()


def init_db():
    """Initializes the active local SQLite database."""
    from .core_agents import ensure_core_agent_profiles

    db_path = current_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    importlib.import_module("app.models")  # Ensure SQLAlchemy metadata is registered before create_all.
    Base.metadata.create_all(bind=engine)
    run_schema_migrations()
    db = SessionLocal()
    try:
        ensure_core_agent_profiles(db)
    finally:
        db.close()
    _initialized_db_paths.add(str(db_path))


def ensure_db_ready():
    db_path = current_db_path()
    db_key = str(db_path)
    if db_key in _initialized_db_paths and db_path.exists():
        return
    init_db()


def reset_database_runtime() -> None:
    engine.dispose()
    _initialized_db_paths.clear()


def get_db():
    ensure_db_ready()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

