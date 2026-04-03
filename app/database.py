from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from .paths import PEXO_DB_PATH

# Pure local SQLite database. Zero external dependencies.
DATABASE_URL = f"sqlite:///{PEXO_DB_PATH.as_posix()}"

# connect_args={"check_same_thread": False} is needed for SQLite in FastAPI
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
_db_initialized = False


MEMORY_TABLE_MIGRATIONS = {
    "is_pinned": "ALTER TABLE memories ADD COLUMN is_pinned BOOLEAN DEFAULT 0",
    "is_archived": "ALTER TABLE memories ADD COLUMN is_archived BOOLEAN DEFAULT 0",
    "compacted_into_id": "ALTER TABLE memories ADD COLUMN compacted_into_id INTEGER",
    "updated_at": "ALTER TABLE memories ADD COLUMN updated_at DATETIME",
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

    if not statements:
        ensure_search_indexes()
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    ensure_search_indexes()

def init_db():
    """Initializes the local SQLite database."""
    global _db_initialized
    from . import models  # Ensure SQLAlchemy metadata is registered before create_all.
    from .core_agents import ensure_core_agent_profiles

    PEXO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    run_schema_migrations()
    db = SessionLocal()
    try:
        ensure_core_agent_profiles(db)
    finally:
        db.close()
    _db_initialized = True


def ensure_db_ready():
    if _db_initialized and PEXO_DB_PATH.exists():
        return
    init_db()

def get_db():
    ensure_db_ready()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
