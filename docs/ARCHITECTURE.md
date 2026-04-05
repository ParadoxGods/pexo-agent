# Pexo Architecture

Pexo is a local-first control plane that sits between the user, local state, and one or more AI clients.

Its job is not to replace Codex, Gemini, or Claude. Its job is to hold shared context, memory, artifacts, preferences, and workflow state so those clients can work against the same local system instead of becoming isolated chat silos.

## Core Model

Pexo has four main responsibilities:

1. Persist local state.
   Pexo stores memory, artifacts, agent definitions, workflow state, profile defaults, and tool metadata in a local SQLite database under the active state root.

2. Expose a shared control plane.
   Pexo serves a local API, UI, and MCP surface so multiple clients can read and write the same local state.

3. Orchestrate work.
   Pexo can start and continue structured task sessions, track agent progress, and preserve session state for handoff between clients.

4. Keep context compact.
   Pexo maintains search indexes, archives stale memory, compacts repeated context, and keeps retrieval local-first.

## Persistence

The shipped runtime is SQLite-based and local-first.

- Database: `pexo.db`
- Artifact storage: local files under the active state root
- Dynamic tools: Python files under the active state root
- Optional semantic memory: local ChromaDB store when the vector profile is explicitly installed

By default, packaged installs keep mutable state under `~/.pexo`. Checkout mode keeps mutable state under the repo-local `.pexo` directory.

## Runtime State

Pexo resolves its active state root at runtime rather than assuming one fixed global path forever.

That lets:

- packaged installs use managed state outside the repo
- checkout mode keep state inside the working copy
- tests point Pexo at isolated temporary state roots
- embedders override `PEXO_HOME` without needing a separate build

## Orchestration

Pexo tracks task sessions in persisted agent state.

The normal simplified flow is:

1. intake or start
2. optional clarification
3. supervisor planning
4. worker execution
5. mandatory QA review
6. delivery review / final response

Quality Assurance is intended to be a real gate, not a cosmetic role.

## Memory Lifecycle

Pexo keeps memory in three coordinated layers:

- SQLite rows for canonical persistence
- SQLite FTS indexes for fast local keyword retrieval
- optional Chroma embeddings for semantic retrieval when the vector profile is installed

Archive, compaction, update, delete, and dedup paths should keep those layers in sync.

## Genesis Tools

Genesis tools are local Python tools registered through Pexo. They are powerful and therefore guarded by an explicit trust model.

Pexo supports three Genesis trust modes:

- `read-only`
  Tool inspection only. No execution or mutation.
- `approval-required`
  Only pre-approved tool names may execute. Tool registration, update, and deletion stay blocked.
- `full-local-exec`
  Full local registration and execution of Genesis tools.

This trust boundary matters because Genesis tools run with local user privileges. They are not a sandbox.

## MCP Role

Pexo is primarily an MCP middle layer.

The preferred production model is:

- the user works through Codex, Gemini, Claude, or another MCP-capable client
- those clients use Pexo for shared memory, artifacts, and session state
- Pexo keeps continuity on the local machine even when clients change

## UI Role

The UI is a control plane, not the primary product surface.

It should make it easy to inspect:

- runtime health
- connected clients
- important memory
- recent context
- workflow sessions
- advanced system controls
