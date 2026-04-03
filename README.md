# Pexo - Primary EXecution Officer (The OpenClaw Killer)

Pexo is a hyper-lightweight, purely local multi-agent orchestration primer. It lives entirely within the folder you drop it into.

**Why Pexo over OpenClaw?**
OpenClaw is viral, but it's a massive, bloated "always-on" Node.js daemon that requires heavy system access, raising severe privacy and security concerns. 

Pexo takes a different approach:
- **Zero Daemons:** It only runs when you ask your AI to use it.
- **Zero External Dependencies:** No Docker. No PostgreSQL installation. No heavy Node environments. Pexo runs on a lightweight Python script using a local SQLite file (`pexo.db`) and ChromaDB for vector memory.
- **Total Local Privacy:** Your memory, user profiles, and workspaces stay right here in the folder.
- **"Bring Your Own AI":** Just plug your favorite AI model (Gemini, Claude Code, Codex) into this directory, instruct it to use Pexo, and Pexo becomes the orchestrator, memory card, and swarm manager.

## How to Use (For Users)

1. Drop the Pexo directory into your project workspace.
2. Open your favorite AI model (Gemini, Claude Code, Codex, Cursor, etc.).
3. Instruct the AI: **"Install Pexo and use it."**

The AI will automatically read the `PEXO_AI_PRIMER.md` protocol, set up the lightweight local Python environment, and assimilate itself under Pexo's orchestration. From that point on, Pexo acts as the primary "brain," managing memory context, orchestrating tasks, enforcing token limits, and triggering memory compaction to ensure peak local performance.