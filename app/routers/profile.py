from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Dict, Optional
from ..database import get_db
from ..models import Profile
from ..paths import normalize_user_path

router = APIRouter()

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

PROFILE_PRESETS = {
    "efficient_operator": {
        "label": "Efficient Operator",
        "description": "Direct, low-ceremony defaults tuned for fast local execution.",
        "personality_answers": {
            "p1": "1",
            "p2": "1",
            "p3": "1",
            "p4": "2",
            "p5": "3",
            "p6": "1",
            "p7": "1",
            "p8": "1",
            "p9": "2",
            "p10": "2",
        },
        "scripting_answers": {
            "s1": "4",
            "s2": "1",
            "s3": "1",
            "s4": "2",
            "s5": "1",
            "s6": "1",
            "s7": "2",
            "s8": "1",
            "s9": "4",
            "s10": "1",
        },
    },
    "balanced_builder": {
        "label": "Balanced Builder",
        "description": "A moderate default that keeps explanations and guardrails without much drag.",
        "personality_answers": {
            "p1": "2",
            "p2": "2",
            "p3": "1",
            "p4": "2",
            "p5": "2",
            "p6": "1",
            "p7": "2",
            "p8": "2",
            "p9": "1",
            "p10": "2",
        },
        "scripting_answers": {
            "s1": "4",
            "s2": "2",
            "s3": "3",
            "s4": "2",
            "s5": "2",
            "s6": "3",
            "s7": "3",
            "s8": "2",
            "s9": "4",
            "s10": "2",
        },
    },
    "strict_engineer": {
        "label": "Strict Engineer",
        "description": "Higher rigor, stricter typing, and more explicit discipline when safety matters.",
        "personality_answers": {
            "p1": "1",
            "p2": "3",
            "p3": "1",
            "p4": "3",
            "p5": "2",
            "p6": "1",
            "p7": "1",
            "p8": "3",
            "p9": "1",
            "p10": "3",
        },
        "scripting_answers": {
            "s1": "4",
            "s2": "2",
            "s3": "1",
            "s4": "1",
            "s5": "1",
            "s6": "3",
            "s7": "3",
            "s8": "1",
            "s9": "4",
            "s10": "3",
        },
    },
}


class ProfileAnswers(BaseModel):
    name: str = "default_user"
    personality_answers: Dict[str, str] = Field(default_factory=dict)
    scripting_answers: Dict[str, str] = Field(default_factory=dict)
    backup_path: str = ""
    clear_backup_path: bool = False

class QuickSetupRequest(BaseModel):
    name: str = "default_user"
    backup_path: str = ""
    clear_backup_path: bool = False


def build_profile_payload(
    name: str,
    personality_answers: Dict[str, str],
    scripting_answers: Dict[str, str],
    backup_path: str = "",
) -> ProfileAnswers:
    return ProfileAnswers(
        name=name,
        personality_answers=dict(personality_answers),
        scripting_answers=dict(scripting_answers),
        backup_path=backup_path,
    )


def build_profile_from_preset(
    preset_name: str,
    name: str = "default_user",
    backup_path: str = "",
) -> ProfileAnswers:
    preset = PROFILE_PRESETS.get(preset_name)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"Unknown profile preset '{preset_name}'.")
    return build_profile_payload(
        name=name,
        personality_answers=preset["personality_answers"],
        scripting_answers=preset["scripting_answers"],
        backup_path=backup_path,
    )


def map_profile_answers(answers: ProfileAnswers) -> tuple[str, dict, Optional[str]]:
    normalized_backup_path = normalize_user_path(answers.backup_path)
    if normalized_backup_path:
        try:
            normalized_backup_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid backup path. Could not create directory '{normalized_backup_path}': {str(e)}")

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
    return full_personality_prompt, scripting_prefs, str(normalized_backup_path) if normalized_backup_path else None


def upsert_profile(answers: ProfileAnswers, db: Session) -> Profile:
    full_personality_prompt, scripting_prefs, normalized_backup_path = map_profile_answers(answers)
    profile = db.query(Profile).filter(Profile.name == answers.name).first()

    if profile:
        if answers.clear_backup_path:
            profile.backup_path = None
        if full_personality_prompt:
            profile.personality_prompt = full_personality_prompt
        if scripting_prefs:
            profile.scripting_preferences = scripting_prefs
        if normalized_backup_path:
            profile.backup_path = normalized_backup_path
    else:
        profile = Profile(
            name=answers.name,
            personality_prompt=full_personality_prompt,
            scripting_preferences=scripting_prefs,
            backup_path=normalized_backup_path,
        )
        db.add(profile)

    db.commit()
    db.refresh(profile)
    return profile


@router.get("/questions")
def get_onboarding_questions():
    """Returns the 10 personality and 10 scripting questions for UI/AI to present to the user."""
    return {
        "personality": PERSONALITY_QUESTIONS,
        "scripting": SCRIPTING_QUESTIONS
    }

@router.get("/presets")
def get_profile_presets():
    return {
        "presets": [
            {
                "id": preset_id,
                "label": preset["label"],
                "description": preset["description"],
            }
            for preset_id, preset in PROFILE_PRESETS.items()
        ]
    }

@router.get("/{name}")
def get_profile(name: str, db: Session = Depends(get_db)):
    profile = db.query(Profile).filter(Profile.name == name).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found. Onboarding required.")
    return profile

@router.post("/quick-setup/{preset_name}")
def quick_setup_profile(preset_name: str, request: QuickSetupRequest, db: Session = Depends(get_db)):
    preset_answers = build_profile_from_preset(preset_name, name=request.name, backup_path=request.backup_path)
    preset_answers.clear_backup_path = request.clear_backup_path
    profile = upsert_profile(preset_answers, db)
    return {
        "status": "success",
        "message": f"Profile initialized with preset '{preset_name}'.",
        "profile_id": profile.id,
    }

@router.post("/")
def create_or_update_profile(answers: ProfileAnswers, db: Session = Depends(get_db)):
    """Maps the numbered answers to the actual text preferences and saves to DB."""
    profile = upsert_profile(answers, db)
    return {"status": "success", "message": "Profile updated successfully.", "profile_id": profile.id}
