from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uuid

app = FastAPI(title="Pexo - Primary EXecution Officer")

class PromptRequest(BaseModel):
    user_id: str
    prompt: str
    session_id: str = None

class ClarificationResponse(BaseModel):
    session_id: str
    clarification_question: str

@app.post("/intake", response_model=ClarificationResponse)
async def intake_prompt(request: PromptRequest):
    """
    Step 1 & 2: Intake and Clarification (The 'One-Ask' Rule)
    Any prompt the user posts to pexo should immediately be asked more on only once
    so that the agents all have the parameters for preference for the user.
    """
    session_id = request.session_id or str(uuid.uuid4())
    
    # TODO: Invoke a lightweight LLM call here to analyze the prompt 
    # and generate the SINGLE most important clarifying question to set agent parameters.
    # For now, we return a mocked smart clarification question.
    
    mock_clarification = (
        f"You asked: '{request.prompt}'. To ensure the Supervisor and Context Managers "
        "can organize this perfectly, could you clarify your specific requirements regarding "
        "performance constraints and preferred directory structure for this request?"
    )
    
    return ClarificationResponse(
        session_id=session_id,
        clarification_question=mock_clarification
    )

@app.post("/execute")
async def execute_plan(session_id: str, clarification_answer: str):
    """
    Steps 3 - 6: The core multi-agent execution loop.
    This endpoint is called AFTER the user answers the clarification question.
    """
    # 3. Extraction
    # 4. Context Review (Profiles, Workspaces, Memory via pgvector)
    # 5. Execution (Delegation to Supervisor -> Developer, monitored by Time, Context, Resource, CodeOrg Managers)
    # 6. Persistence (All agents write back to Postgres)
    
    return {"status": "Execution started", "session_id": session_id}
