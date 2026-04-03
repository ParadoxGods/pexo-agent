from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import shutil
import os
from datetime import datetime

from ..database import get_db
from ..models import Profile

router = APIRouter()

@router.post("/run")
def run_backup(db: Session = Depends(get_db)):
    """
    Zips Pexo's SQLite DB, ChromaDB vectors, and Dynamic Tools, 
    and copies them to the user's configured local or network backup path.
    """
    profile = db.query(Profile).filter(Profile.name == "default_user").first()
    if not profile or not profile.backup_path:
        raise HTTPException(status_code=400, detail="Backup path not configured in profile. Please set a backup_path.")
    
    backup_target = profile.backup_path
    if not os.path.exists(backup_target):
        try:
            os.makedirs(backup_target, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not create backup directory '{backup_target}': {str(e)}")
            
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder = os.path.join(backup_target, f"pexo_backup_{timestamp}")
    
    try:
        os.makedirs(backup_folder, exist_ok=True)
        
        # Copy SQLite DB
        if os.path.exists("pexo.db"):
            shutil.copy2("pexo.db", backup_folder)
            
        # Copy ChromaDB
        if os.path.exists("chroma_db"):
            shutil.copytree("chroma_db", os.path.join(backup_folder, "chroma_db"))
            
        # Copy Dynamic Tools
        if os.path.exists("app/dynamic_tools"):
            shutil.copytree("app/dynamic_tools", os.path.join(backup_folder, "dynamic_tools"))
            
        # Zip it up for efficiency
        shutil.make_archive(backup_folder, 'zip', backup_folder)
        shutil.rmtree(backup_folder) # clean up the unzipped folder
        
        return {
            "status": "success", 
            "message": f"Pexo's Brain successfully backed up to {backup_folder}.zip"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")
