from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from .paths import PEXO_DB_PATH

# Pure local SQLite database. Zero external dependencies.
DATABASE_URL = f"sqlite:///{PEXO_DB_PATH.as_posix()}"

# connect_args={"check_same_thread": False} is needed for SQLite in FastAPI
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    """Initializes the local SQLite database."""
    from . import models  # Ensure SQLAlchemy metadata is registered before create_all.
    from .core_agents import ensure_core_agent_profiles

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_core_agent_profiles(db)
    finally:
        db.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
