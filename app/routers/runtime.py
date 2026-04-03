from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..runtime import build_runtime_status, promote_runtime

router = APIRouter()


class RuntimePromotionRequest(BaseModel):
    profile: str


@router.get("/status")
def get_runtime_status(db: Session = Depends(get_db)):
    return build_runtime_status(db)


@router.post("/promote/{profile}")
def promote_runtime_profile(profile: str, db: Session = Depends(get_db)):
    try:
        result = promote_runtime(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result["runtime"] = build_runtime_status(db)
    return result
