from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import shutil
from datetime import datetime
from pathlib import Path

from ..database import get_db
from ..models import Profile
from ..paths import CHROMA_DB_DIR, DYNAMIC_TOOLS_DIR, PEXO_DB_PATH, normalize_user_path

router = APIRouter()

def create_backup_archive(
    backup_target: Path,
    db_path: Path = PEXO_DB_PATH,
    chroma_dir: Path = CHROMA_DB_DIR,
    dynamic_tools_dir: Path = DYNAMIC_TOOLS_DIR,
) -> Path:
    backup_target.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder = backup_target / f"pexo_backup_{timestamp}"
    backup_folder.mkdir(parents=True, exist_ok=False)

    if db_path.exists():
        shutil.copy2(db_path, backup_folder / db_path.name)

    if chroma_dir.exists():
        shutil.copytree(chroma_dir, backup_folder / chroma_dir.name)

    if dynamic_tools_dir.exists():
        shutil.copytree(
            dynamic_tools_dir,
            backup_folder / "dynamic_tools",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    archive_path = Path(shutil.make_archive(str(backup_folder), "zip", root_dir=backup_folder))
    shutil.rmtree(backup_folder)
    return archive_path


def run_backup_for_profile(db: Session) -> dict:
    """
    Zips Pexo's SQLite DB, ChromaDB vectors, and Dynamic Tools,
    and copies them to the user's configured local or network backup path.
    """
    profile = db.query(Profile).filter(Profile.name == "default_user").first()
    backup_target = normalize_user_path(profile.backup_path if profile else None)
    if backup_target is None:
        raise HTTPException(status_code=400, detail="Backup path not configured in profile. Please set a backup_path.")

    try:
        archive_path = create_backup_archive(backup_target)
        return {
            "status": "success",
            "message": f"Pexo's Brain successfully backed up to {archive_path}",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")


@router.post("/run")
def run_backup(db: Session = Depends(get_db)):
    return run_backup_for_profile(db)
