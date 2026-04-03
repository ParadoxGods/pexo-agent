import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# The Database URL will be provided by docker-compose environment variables in production.
# Defaulting to localhost for local testing/development.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://pexo:pexopassword@localhost:5432/pexo")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
