# Pexo - Primary EXecution Officer

Pexo is a multi-agent primer that lives locally on your system. It utilizes Docker and a local PostgreSQL (pgvector) database, spinning itself up entirely within the folder you drop it into.

## How to Use (For Users)

1. Drop the Pexo directory into your project workspace.
2. Open your favorite AI model (Gemini, Claude Code, Codex, Cursor, etc.).
3. Instruct the AI: **"Install Pexo and use it."**

The AI will automatically read the `PEXO_AI_PRIMER.md` protocol, spin up the Docker containers, and assimilate itself under Pexo's orchestration. From that point on, Pexo acts as the primary "brain," managing memory context, orchestrating tasks, enforcing token limits, and triggering memory compaction to ensure peak local performance.
