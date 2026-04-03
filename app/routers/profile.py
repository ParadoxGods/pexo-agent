from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Dict, Optional
import hashlib
import os
from ..database import get_db
from ..models import Profile

router = APIRouter()

def hash_password(password: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return salt.hex() + ":" + key.hex()

def verify_password(password: str, hashed: str) -> bool:
    try:
        salt_hex, key_hex = hashed.split(":")
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return key.hex() == key_hex
    except Exception:
        return False

PERSONALITY_QUESTIONS = [
    {"id": "p1", "text": "Communication Style", "options": {"1": "Direct & Concise", "2": "Conversational & Explanatory", "3": "Technical & Academic"}},
    {"id": "p2", "text": "Feedback Handling", "options": {"1": "Blunt & Immediate", "2": "Gentle & Constructive", "3": "Neutral & Objective"}},
    {"id": "p3", "text": "Humor Level", "options": {"1": "None", "2": "Occasional light humor", "3": "Highly sarcastic/witty"}},
    {"id": "p4", "text": "Detail Orientation", "options": {"1": "Big picture only", "2": "Balanced", "3": "Granular/Micro-level"}},
    {"id": "p5", "text": "Proactiveness", "options": {"1": "Wait for instructions", "2": "Suggest next steps", "3": "Auto-execute logical next steps"}},
    {"id": "p6", "text": "Tone", "options": {"1": "Formal/Professional", "2": "Casual/Friendly", "3": "Assertive/Commanding"}},
    {"id": "p7", "text": "Apologies", "options": {"1": "Never apologize, just fix", "2": "Brief acknowledgment", "3": "Empathetic apology"}},
    {"id": "p8", "text": "Confidence", "options": {"1": "Highly confident/authoritative", "2": "Collaborative/Peer-like", "3": "Cautious/Verifying"}},
    {"id": "p9", "text": "Explanations", "options": {"1": "Always explain 'Why'", "2": "Explain only if asked", "3": "ELI5 (Explain like I'm 5)"}},
    {"id": "p10", "text": "Formatting", "options": {"1": "Heavy use of markdown/tables", "2": "Plain text mostly", "3": "Bullet points only"}}
]

SCRIPTING_QUESTIONS = [
    {"id": "s1", "text": "Language Preference", "options": {"1": "Python-first", "2": "JS/TS-first", "3": "Rust/Go-first", "4": "Project-dependent"}},
    {"id": "s2", "text": "Code Comments", "options": {"1": "No comments, self-documenting", "2": "Function-level docs only", "3": "Heavily commented line-by-line"}},
    {"id": "s3", "text": "Type Checking", "options": {"1": "Strict typing always", "2": "Loose/Dynamic typing", "3": "Only where necessary"}},
    {"id": "s4", "text": "Testing Approach", "options": {"1": "TDD / Test first", "2": "Write tests after", "3": "No tests unless asked"}},
    {"id": "s5", "text": "Error Handling", "options": {"1": "Fail fast/crash", "2": "Graceful degradation", "3": "Silent logging"}},
    {"id": "s6", "text": "Architecture Style", "options": {"1": "Monolithic/Simple", "2": "Microservices", "3": "Modular/Component-based"}},
    {"id": "s7", "text": "Refactoring", "options": {"1": "Aggressive/Always improve", "2": "Only touch what's broken", "3": "Ask before refactoring"}},
    {"id": "s8", "text": "Dependency Management", "options": {"1": "Minimal external libs", "2": "Use established frameworks", "3": "Bleeding edge tech"}},
    {"id": "s9", "text": "Naming Conventions", "options": {"1": "camelCase", "2": "snake_case", "3": "PascalCase", "4": "Project-native"}},
    {"id": "s10", "text": "Version Control", "options": {"1": "Frequent micro-commits", "2": "Large feature commits", "3": "Rebase & Squash only"}}
]

class ProfileAnswers(BaseModel):
    name: str = "default_user"
    personality_answers: Dict[str, str] = {}
    scripting_answers: Dict[str, str] = {}
    backup_path: str = ""

@router.get("/questions")
def get_onboarding_questions():
    """Returns the 10 personality and 10 scripting questions for UI/AI to present to the user."""
    return {
        "personality": PERSONALITY_QUESTIONS,
        "scripting": SCRIPTING_QUESTIONS
    }

@router.get("/{name}")
def get_profile(name: str, db: Session = Depends(get_db)):
    profile = db.query(Profile).filter(Profile.name == name).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found. Onboarding required.")
    return profile

@router.post("/")
def create_or_update_profile(answers: ProfileAnswers, db: Session = Depends(get_db)):
    """Maps the numbered answers to the actual text preferences and saves to DB."""
    
    profile = db.query(Profile).filter(Profile.name == answers.name).first()
    
    # Determine what to update
    personality_prompt_parts = []
    if answers.personality_answers:
        for q in PERSONALITY_QUESTIONS:
            ans_key = answers.personality_answers.get(q["id"])
            if ans_key and ans_key in q["options"]:
                personality_prompt_parts.append(f"{q['text']}: {q['options'][ans_key]}")
    
    scripting_prefs = {}
    if answers.scripting_answers:
        for q in SCRIPTING_QUESTIONS:
            ans_key = answers.scripting_answers.get(q["id"])
            if ans_key and ans_key in q["options"]:
                scripting_prefs[q["text"]] = q["options"][ans_key]

    full_personality_prompt = "\n".join(personality_prompt_parts)

    if profile:
        # Update existing
        if full_personality_prompt:
            profile.personality_prompt = full_personality_prompt
        if scripting_prefs:
            profile.scripting_preferences = scripting_prefs
        if answers.backup_path:
            profile.backup_path = answers.backup_path
    else:
        # Create new
        profile = Profile(
            name=answers.name,
            personality_prompt=full_personality_prompt,
            scripting_preferences=scripting_prefs,
            backup_path=answers.backup_path if answers.backup_path else None
        )
        db.add(profile)
    
    db.commit()
    db.refresh(profile)
    return {"status": "success", "message": "Profile updated successfully.", "profile_id": profile.id}
updated successfully.", "profile_id": profile.id}
