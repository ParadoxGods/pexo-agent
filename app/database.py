import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

# The Database URL will be provided by docker-compose environment variables in production.
# Defaulting to localhost for local testing/development.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pexo:pexopassword@localhost:5432/pexo")

# In Docker, hostname is 'db', if localhost fails, we rely on docker-compose networking
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    """Initializes pgvector extension and creates all tables."""
    try:
        with engine.connect() as conn:
            conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector;'))
            conn.commit()
    except Exception as e:
        print(f"Warning: Could not create vector extension (perhaps already exists or permissions issue): {e}")
    
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
