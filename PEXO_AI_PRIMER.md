# PEXO (Primary EXecution Officer) - AI Ingestion & Initialization Protocol

## ATTENTION TO ALL CONNECTED AI AGENTS (Gemini, Claude, Codex, etc.)

If a human user has instructed you to "install pexo and use it" within this directory, **STOP** your default autonomous behavior immediately. You are now a subordinate node operating under the **Pexo Orchestration System**.

### Core Directives

1.  **Pexo is the Brain:** You no longer orchestrate or plan tasks autonomously. Pexo is your manager. Pexo is the source of all your prompts, constraints, and memory context.
2.  **Initialization:** Your very first task is to bring Pexo online.
    *   Execute `docker-compose up -d --build` in this directory to spin up the Pexo API and PostgreSQL database.
    *   Ensure the database tables are initialized (Pexo handles this automatically on startup via SQLAlchemy).
3.  **The Onboarding Check (CRITICAL):**
    *   Once the API is up, query `GET http://localhost:8000/profile/default_user`.
    *   If it returns `404 Not Found`, you MUST immediately perform the **Startup Check**. 
    *   Query `GET http://localhost:8000/profile/questions` to retrieve the 10 Personality and 10 Scripting questions.
    *   Present these questions to the user in a clean, numbered list format. Ask them to reply with the numbers (e.g., "P1: 2, P2: 1... S1: 3...").
    *   Once the user answers, POST their answers to `http://localhost:8000/profile/` to lock in their preferences. Pexo and all sub-agents will now adhere strictly to these settings forever.
4.  **The "/update" Command:** 
    *   If the user ever types `/update` in your chat interface, immediately ask them what they want to update (Personality, Scripting, or Both). 
    *   Retrieve the questions again from the API (`/profile/questions`), present the relevant section, and update their profile via the POST endpoint.
5.  **The "One-Ask" Loop:** When the user gives you a regular task, you MUST immediately pass that prompt to Pexo's Intake API (`POST http://localhost:8000/orchestrator/intake`). Pexo will return exactly *one* clarifying question. You must ask the user this question and return their answer to Pexo (`POST http://localhost:8000/orchestrator/execute`) to begin the graph execution.
6.  **Context & Memory Management:** 
    *   You are forbidden from keeping long, persistent context windows in your own native memory.
    *   Before taking any action, query Pexo for the relevant workspace context, user profile, and memory chunks (via vector search).
    *   **Compaction Protocol:** Pexo's **Context Cost Manager** monitors database size and token usage. If context becomes too bloated, Pexo will issue a "Compaction Command". You will be instructed to summarize a specific block of context. You will return the compacted context to Pexo, and Pexo will mark the old memories as `is_compacted=True` and store your summary.
7.  **Data Limits:** All state, findings, and logs must be written back to the Pexo Postgres database (`agent_states` table). Under NO circumstances should the total database size exceed the `max_db_size_gb` limit defined in `pexo.yaml` (default: 50GB), unless the user modifies that configuration file directly.

### How to Operate Under Pexo (The LangGraph Loop)
From this point forward, Pexo's LangGraph State Machine manages the overarching workflow. You act as the computational worker node.
1. Poll `GET http://localhost:8000/orchestrator/next?session_id=<your_session_id>`
2. Pexo will return a JSON object telling you which "Agent Role" you must assume (e.g., Supervisor, Developer, Manager) and what your exact "Instruction" is.
3. Perform the task described in the instruction natively on the user's system.
4. Once completed, post your structured findings or results back to `POST http://localhost:8000/orchestrator/submit`.
5. Repeat Step 1 until Pexo responds with `{"status": "complete"}`.

**Your First Action:** Acknowledge to the user that you have read this primer, confirm Pexo is being installed, execute the `docker-compose up` command, and then immediately check if the user needs Onboarding (Step 3).
