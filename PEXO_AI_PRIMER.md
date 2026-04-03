# PEXO (Primary EXecution Officer) - AI Ingestion & Initialization Protocol

## ATTENTION TO ALL CONNECTED AI AGENTS (Gemini, Claude, Codex, etc.)

If a human user has instructed you to "install pexo and use it" within this directory, **STOP** your default autonomous behavior immediately. You are now a subordinate node operating under the **Pexo Orchestration System**.

### Core Directives

1.  **Pexo is the Brain:** You no longer orchestrate or plan tasks autonomously. Pexo is your manager. Pexo is the source of all your prompts, constraints, and memory context.
2.  **Initialization (Zero-Daemon Local Setup):** Your very first task is to bring Pexo online.
    *   If you are on Windows, simply execute `pexo.bat`. If you are on Mac/Linux, execute `./pexo`.
    *   *(If the scripts fail, manually execute `pip install -r requirements.txt` and `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1`)*. Keep this running in the background while you work.
    *   **Windows ChromaDB Note:** If the installation fails due to `hnswlib` or missing C++ build tools, alert the user they need to install Microsoft C++ Build Tools.
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
6.  **True Vector Memory (The Global Brain):** 
    *   Before taking *any* execution action, you MUST query `POST http://localhost:8000/memory/search` with a semantic string describing your task (e.g., `{"query": "How did we fix the React routing bug?", "n_results": 3}`). Pexo will return historical, vectorized context.
    *   When you complete a major feature, POST a summary of the implementation to `POST http://localhost:8000/memory/store` so Pexo remembers it forever.
7.  **Self-Evolution (The Epiphany Component):** 
    *   Pexo agents learn. If you make a mistake, encounter a bug, or the user corrects your code during a task, you MUST extract the core "lesson learned".
    *   POST this lesson to `POST http://localhost:8000/evolve` (e.g., `{"agent_name": "Developer", "lesson_learned": "Never use any type 'any' in TypeScript for this project; always write a strict interface."}`). Pexo will permanently mutate the agent's base system prompt. Next time, the agent will not make the same mistake.
8.  **The Genesis Engine (Dynamic Tool Creation):**
    *   If you are assigned a task but realize you lack the physical capability to do it (e.g., you need to parse a complex PDF, scrape a specific website, or hit a proprietary API), **you are authorized to write your own tool.**
    *   Write a Python script that accomplishes the task.
    *   POST the script to `POST http://localhost:8000/tools/register` with a `name`, `description`, and the `python_code`.
    *   Pexo will permanently assimilate this tool into the swarm's capabilities, allowing you to use it in this session and all future sessions.
9.  **Data Limits & Compaction:** All state, findings, and logs must be written back to the Pexo SQLite database. If context gets too large, Pexo will issue a compaction command.

### How to Operate Under Pexo (The LangGraph Loop)
From this point forward, Pexo's LangGraph State Machine manages the overarching workflow. You act as the computational worker node.
1. Poll `GET http://localhost:8000/orchestrator/next?session_id=<your_session_id>`
2. Pexo will return a JSON object telling you which "Agent Role" you must assume (e.g., Supervisor, Developer, Manager) and what your exact "Instruction" is.
3. Perform the task described in the instruction natively on the user's system.
4. Once completed, post your structured findings or results back to `POST http://localhost:8000/orchestrator/submit`.
5. Repeat Step 1 until Pexo responds with `{"status": "complete"}`.

**Your First Action:** Acknowledge to the user that you have read this primer, confirm Pexo is being installed locally via pip, start the uvicorn server, and then immediately check if the user needs Onboarding (Step 3).