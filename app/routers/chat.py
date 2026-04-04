from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..direct_chat import (
    create_chat_session,
    delete_chat_session,
    get_chat_session_payload,
    list_chat_backends,
    list_chat_sessions,
    send_chat_message,
    update_chat_session,
)

router = APIRouter()


class ChatSessionCreateRequest(BaseModel):
    backend: str = "auto"
    workspace_path: str | None = None
    title: str | None = None


class ChatSessionUpdateRequest(BaseModel):
    title: str | None = None
    backend: str | None = None
    workspace_path: str | None = None


class ChatMessageRequest(BaseModel):
    message: str
    timeout_seconds: int = 300


def _map_chat_error(exc: RuntimeError) -> HTTPException:
    text = str(exc)
    if "not found" in text.lower():
        return HTTPException(status_code=404, detail=text)
    return HTTPException(status_code=400, detail=text)


@router.get("/backends")
def get_chat_backends():
    return list_chat_backends()


@router.get("/sessions")
def get_chat_sessions(limit: int = 20, db: Session = Depends(get_db)):
    return list_chat_sessions(db, limit=limit)


@router.post("/sessions")
def create_session(request: ChatSessionCreateRequest, db: Session = Depends(get_db)):
    try:
        return create_chat_session(
            db,
            backend=request.backend,
            workspace_path=request.workspace_path,
            title=request.title,
        )
    except RuntimeError as exc:
        raise _map_chat_error(exc) from exc


@router.get("/sessions/{session_id}")
def get_session(session_id: str, db: Session = Depends(get_db)):
    try:
        return get_chat_session_payload(db, session_id)
    except RuntimeError as exc:
        raise _map_chat_error(exc) from exc


@router.patch("/sessions/{session_id}")
def patch_session(session_id: str, request: ChatSessionUpdateRequest, db: Session = Depends(get_db)):
    try:
        return update_chat_session(
            db,
            session_id=session_id,
            title=request.title,
            backend=request.backend,
            workspace_path=request.workspace_path,
        )
    except RuntimeError as exc:
        raise _map_chat_error(exc) from exc


@router.delete("/sessions/{session_id}")
def remove_session(session_id: str, db: Session = Depends(get_db)):
    try:
        return delete_chat_session(db, session_id)
    except RuntimeError as exc:
        raise _map_chat_error(exc) from exc


@router.post("/sessions/{session_id}/messages")
def post_message(session_id: str, request: ChatMessageRequest, db: Session = Depends(get_db)):
    try:
        return send_chat_message(
            db,
            session_id=session_id,
            message=request.message,
            timeout_seconds=max(30, min(request.timeout_seconds, 900)),
        )
    except RuntimeError as exc:
        raise _map_chat_error(exc) from exc
