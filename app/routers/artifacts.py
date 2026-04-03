from __future__ import annotations

import hashlib
import mimetypes
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Artifact
from ..paths import ARTIFACTS_DIR, normalize_user_path

router = APIRouter()

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".yaml",
    ".yml",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".csv",
    ".log",
    ".xml",
}
TEXT_PREVIEW_LIMIT_BYTES = 1024 * 1024
ARTIFACT_PREVIEW_LENGTH = 1200


class ArtifactTextRequest(BaseModel):
    name: str
    content: str
    session_id: str = "artifact_session"
    task_context: str = "general"
    source_uri: str | None = None
    content_type: str = "text/plain"


class ArtifactPathRequest(BaseModel):
    path: str
    session_id: str = "artifact_session"
    task_context: str = "general"
    name: str | None = None


def _safe_filename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in name.strip())
    return cleaned or "artifact"


def _artifact_storage_path(original_name: str) -> Path:
    suffix = Path(original_name).suffix
    stem = Path(original_name).stem or "artifact"
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR / f"{_safe_filename(stem)}_{uuid.uuid4().hex[:12]}{suffix}"


def _calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_text(path: Path, content_type: str | None = None) -> bool:
    if (content_type or "").startswith("text/"):
        return True
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    guessed_type, _ = mimetypes.guess_type(path.name)
    return bool(guessed_type and guessed_type.startswith("text/"))


def _extract_text(path: Path, content_type: str | None = None) -> str | None:
    if not _looks_like_text(path, content_type=content_type):
        return None

    with path.open("rb") as handle:
        raw = handle.read(TEXT_PREVIEW_LIMIT_BYTES)
    return raw.decode("utf-8", errors="ignore").strip() or None


def serialize_artifact(artifact: Artifact, include_text: bool = False) -> dict:
    payload = {
        "id": artifact.id,
        "name": artifact.name,
        "source_type": artifact.source_type,
        "source_uri": artifact.source_uri,
        "content_type": artifact.content_type,
        "storage_path": artifact.storage_path,
        "session_id": artifact.session_id,
        "task_context": artifact.task_context,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "details": artifact.details or {},
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "updated_at": artifact.updated_at.isoformat() if artifact.updated_at else None,
        "preview": (artifact.extracted_text or "")[:ARTIFACT_PREVIEW_LENGTH],
        "has_text": bool(artifact.extracted_text),
    }
    if include_text:
        payload["extracted_text"] = artifact.extracted_text
    return payload


def _persist_artifact(
    *,
    db: Session,
    name: str,
    session_id: str,
    task_context: str,
    source_type: str,
    source_uri: str | None,
    content_type: str | None,
    stored_path: Path,
) -> Artifact:
    artifact = Artifact(
        name=name,
        source_type=source_type,
        source_uri=source_uri,
        content_type=content_type,
        storage_path=str(stored_path),
        extracted_text=_extract_text(stored_path, content_type=content_type),
        session_id=session_id,
        task_context=task_context,
        sha256=_calculate_sha256(stored_path),
        size_bytes=stored_path.stat().st_size,
        details={
            "filename": stored_path.name,
            "suffix": stored_path.suffix.lower(),
        },
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


def _require_artifact(db: Session, artifact_id: int) -> Artifact:
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


@router.post("/register-text")
def register_artifact_text(request: ArtifactTextRequest, db: Session = Depends(get_db)):
    stored_path = _artifact_storage_path(request.name)
    stored_path.write_text(request.content, encoding="utf-8")
    artifact = _persist_artifact(
        db=db,
        name=request.name,
        session_id=request.session_id,
        task_context=request.task_context,
        source_type="text",
        source_uri=request.source_uri,
        content_type=request.content_type,
        stored_path=stored_path,
    )
    return {"status": "success", "artifact": serialize_artifact(artifact, include_text=True)}


@router.post("/register-path")
def register_artifact_path(request: ArtifactPathRequest, db: Session = Depends(get_db)):
    source_path = normalize_user_path(request.path)
    if source_path is None or not source_path.exists() or not source_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact source path not found.")

    stored_path = _artifact_storage_path(request.name or source_path.name)
    shutil.copy2(source_path, stored_path)
    guessed_content_type, _ = mimetypes.guess_type(source_path.name)
    artifact = _persist_artifact(
        db=db,
        name=request.name or source_path.name,
        session_id=request.session_id,
        task_context=request.task_context,
        source_type="local_path",
        source_uri=str(source_path),
        content_type=guessed_content_type,
        stored_path=stored_path,
    )
    return {"status": "success", "artifact": serialize_artifact(artifact, include_text=True)}


@router.post("/upload")
async def upload_artifact(
    file: UploadFile = File(...),
    session_id: str = Form("artifact_session"),
    task_context: str = Form("general"),
    db: Session = Depends(get_db),
):
    stored_path = _artifact_storage_path(file.filename or "upload.bin")
    with stored_path.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)
    artifact = _persist_artifact(
        db=db,
        name=file.filename or stored_path.name,
        session_id=session_id,
        task_context=task_context,
        source_type="upload",
        source_uri=file.filename,
        content_type=file.content_type,
        stored_path=stored_path,
    )
    return {"status": "success", "artifact": serialize_artifact(artifact, include_text=True)}


@router.get("/")
def list_artifacts(
    limit: int = 20,
    query: str | None = None,
    session_id: str | None = None,
    task_context: str | None = None,
    db: Session = Depends(get_db),
):
    safe_limit = max(1, min(limit, 100))
    artifact_query = db.query(Artifact)
    if session_id:
        artifact_query = artifact_query.filter(Artifact.session_id == session_id)
    if task_context:
        artifact_query = artifact_query.filter(Artifact.task_context == task_context)
    if query:
        like_query = f"%{query.strip()}%"
        artifact_query = artifact_query.filter(
            (Artifact.name.ilike(like_query))
            | (Artifact.extracted_text.ilike(like_query))
            | (Artifact.session_id.ilike(like_query))
            | (Artifact.task_context.ilike(like_query))
            | (Artifact.source_uri.ilike(like_query))
            | (Artifact.storage_path.ilike(like_query))
        )
    recency_order = func.coalesce(Artifact.updated_at, Artifact.created_at)
    artifacts = artifact_query.order_by(recency_order.desc(), Artifact.id.desc()).limit(safe_limit).all()
    return {"artifacts": [serialize_artifact(artifact) for artifact in artifacts]}


@router.get("/{artifact_id}")
def get_artifact(artifact_id: int, db: Session = Depends(get_db)):
    artifact = _require_artifact(db, artifact_id)
    return serialize_artifact(artifact, include_text=True)


@router.get("/{artifact_id}/download")
def download_artifact(artifact_id: int, db: Session = Depends(get_db)):
    artifact = _require_artifact(db, artifact_id)
    artifact_path = Path(artifact.storage_path)
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing from disk.")
    return FileResponse(path=artifact_path, filename=artifact.name, media_type=artifact.content_type)


@router.delete("/{artifact_id}")
def delete_artifact(artifact_id: int, db: Session = Depends(get_db)):
    artifact = _require_artifact(db, artifact_id)
    artifact_path = Path(artifact.storage_path)
    db.delete(artifact)
    db.commit()
    artifact_path.unlink(missing_ok=True)
    return {"status": "success", "message": f"Artifact {artifact_id} deleted successfully"}
