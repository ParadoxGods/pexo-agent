from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
DYNAMIC_TOOLS_DIR = APP_DIR / "dynamic_tools"
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
PEXO_DB_PATH = PROJECT_ROOT / "pexo.db"
CHROMA_DB_DIR = PROJECT_ROOT / "chroma_db"
RUNTIME_MARKER_PATH = PROJECT_ROOT / ".pexo-deps-profile"


def normalize_user_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    return Path(raw_path).expanduser().resolve(strict=False)
