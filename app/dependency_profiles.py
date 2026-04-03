PROFILE_ORDER = {
    "core": 1,
    "mcp": 2,
    "full": 3,
    "vector": 4,
}

PROFILE_DEPENDENCIES = {
    "core": [
        "fastapi==0.115.0",
        "pydantic==2.12.5",
        "sqlalchemy==2.0.29",
    ],
    "mcp": [
        "fastapi==0.115.0",
        "pydantic==2.12.5",
        "sqlalchemy==2.0.29",
        "mcp==1.27.0",
    ],
    "full": [
        "fastapi==0.115.0",
        "pydantic==2.12.5",
        "sqlalchemy==2.0.29",
        "mcp==1.27.0",
        "uvicorn==0.32.0",
        "langgraph==0.2.0",
    ],
    "vector": [
        "fastapi==0.115.0",
        "pydantic==2.12.5",
        "sqlalchemy==2.0.29",
        "mcp==1.27.0",
        "uvicorn==0.32.0",
        "langgraph==0.2.0",
        "chromadb==0.4.24",
    ],
}

CONSTRAINT_SPECS = sorted({spec for specs in PROFILE_DEPENDENCIES.values() for spec in specs})
