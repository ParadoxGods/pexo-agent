# Pexo: Primary EXecution Operator

Pexo is a lightweight, strictly local multi-agent orchestration framework designed to serve as a persistent execution environment and memory layer for Large Language Models (LLMs).

Operating entirely on the local machine, Pexo provides an autonomous execution engine, a vector-based memory system, and a dynamic tool-generation API without requiring background daemons, external database services, or complex containerization.

## Core Architecture

Pexo is built on three foundational pillars that separate it from traditional agent frameworks:

1.  **Reinforcement Learning from AI Feedback (RLAIF):** Pexo supports persistent agent mutation. When an AI agent encounters an error or receives behavioral correction from a user, the "lesson learned" is permanently integrated into the agent's base system prompt within the local database. This ensures agents adapt to project-specific constraints and coding standards over time.
2.  **Global Vector Brain:** Utilizing a local ChromaDB instance, Pexo vectorizes implemented solutions, architectural decisions, and bug fixes. Before executing new tasks, agents query this historical context via semantic search, maintaining an unbroken chain of project memory across discrete sessions.
3.  **The Genesis Engine (Dynamic Tool Creation):** If an agent lacks the capability to fulfill a specific request (e.g., parsing proprietary file formats or interfacing with undocumented APIs), it is authorized to author the required Python tool. The Genesis Engine dynamically ingests, registers, and exposes these generated scripts to the swarm for immediate and future execution.

## Key Features

*   **Zero-Daemon Execution:** Pexo processes are invoked only when required by the user or the connecting AI model. It does not run persistent background services, mitigating local resource consumption and privacy concerns.
*   **Native MCP Server Integration:** Pexo implements the Model Context Protocol (MCP) natively. This allows seamless integration with MCP-compliant interfaces (such as Claude Desktop and Cursor), surfacing Pexo's memory operations, agent evolution, and dynamic tools directly within the AI's native interface.
*   **Structured MCP Control Plane:** The MCP surface now exposes structured tools for profile management, agent CRUD, memory CRUD and maintenance, orchestration sessions, telemetry, Genesis tool lifecycle control, and backups. A connected AI can manage nearly the entire local Pexo node without falling back to the browser UI.
*   **Local Administration Interface:** Pexo hosts a secure, localhost-bound administrative terminal (accessible via `127.0.0.1:9999`). This interface provides manual oversight of the agent registry, vector database queries, and automated backup configurations.
*   **Editable Local Brain:** The dashboard can inspect and edit both core and custom agents, browse recent memories, update memory records, pin high-value memories, archive stale memories, and delete bad entries without leaving the local machine.
*   **Memory Lifecycle Controls:** Memory maintenance now compacts older context into short summaries, archives excess raw entries, preserves pinned records, and keeps vector search noise under control as the local brain grows.
*   **Execution Telemetry:** The dashboard exposes recent sessions, agent activity, action counts, and observed context volume so users can inspect how the swarm is behaving over time.
*   **Efficient Boot Path:** Normal launcher boots now throttle update checks instead of pulling on every single start. Use `pexo update` when you want an immediate repository refresh, or `pexo --no-browser` when you want the API without opening the dashboard.
*   **Automated State Backup:** The framework supports automated, timestamped archiving of the SQLite state database, vector embeddings, and dynamically generated tools to a designated local directory or network share.

## Installation and Deployment

Pexo now supports two installation models:

1.  **GitHub-native packaged install (preferred):** install the `pexo` and `pexo-mcp` entrypoints directly from GitHub with a Python tool manager such as `uv`.
2.  **Repo-local checkout install (fallback / contributor mode):** clone the checkout, create a local venv, and run the legacy launchers from that checkout.

### Canonical AI Install Path

If the goal is to tell an AI agent to "install Pexo" and have it succeed with minimal judgment, use the standalone bootstrap command. The bootstrap path is designed to reduce decisions:

*   it prefers packaged GitHub install automatically
*   it tries `uv` first, then `pipx`
*   it falls back to a safe checkout install only when needed
*   it completes `headless-setup --preset efficient_operator`
*   it runs `pexo doctor`
*   it emits a machine-readable `PEXO_INSTALL_SUMMARY_JSON=...` line at the end

This is the shortest path to a sub-minute install on a normal machine because it avoids clone-first setup when a packaged tool manager is already available.

**Windows AI-safe bootstrap:**
```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/ParadoxGods/pexo-agent/v1.0.1/bootstrap.ps1 -OutFile bootstrap.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

**macOS/Linux AI-safe bootstrap:**
```bash
curl -fsSL https://raw.githubusercontent.com/ParadoxGods/pexo-agent/v1.0.1/bootstrap.sh | bash
```

The bootstrap path installs into the safe user-local state root by default and does not touch the current repository checkout unless you explicitly use a repo-local install command from a checkout.

### Direct Packaged Install Path

If you want the packaged route without the bootstrap wrapper, use one of the following:

**Recommended with `uv`:**
```bash
uv tool install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0.1"
```

**Fallback with `pipx`:**
```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0.1"
```

Then initialize the local profile entirely from the terminal:

```bash
pexo list-presets
pexo headless-setup --preset efficient_operator
pexo doctor
```

Use the dashboard later only when needed:

```bash
pexo
```

The packaged runtime is intentionally light on first install. `pexo` auto-promotes the full UI runtime on first launch, and `pexo --mcp` or `pexo-mcp` keeps the MCP path lean.

The packaged install keeps all mutable local state under `~/.pexo` by default:

*   `~/.pexo/pexo.db`
*   `~/.pexo/chroma_db/`
*   `~/.pexo/artifacts/`
*   `~/.pexo/dynamic_tools/`

You can override the state directory with `PEXO_HOME`.

### AI-Driven Install Prompt

If you want another AI to perform the install for you, use:

**"Install Pexo from https://github.com/ParadoxGods/pexo-agent using the standalone bootstrap path. Do not install into the current repo unless I explicitly ask for a repo-local node. Complete headless setup with the efficient_operator preset, run pexo doctor, and do not open the web UI unless I ask."**

That instruction is now preferable to clone-first installation.
If the assistant has to fall back to a checkout-based installer, tell it not to target the current repo unless you explicitly want a repo-local node.
Repository-level AI operating instructions live in `AGENTS.md`.

### Repo-Local Checkout Install

The checkout-based installers remain supported for contributors, custom install directories, deterministic repo-local MCP nodes, or environments without `uv`.

Existing Git checkouts are protected by default. If you point the installer at a checkout without explicitly opting in, Pexo now leaves that checkout untouched and installs to the safe default location instead. Pass `-AllowRepoInstall` on Windows or `--allow-repo-install` on macOS/Linux only when you intentionally want a repo-local node.

The install path is still staged for speed:

*   the first checkout-based install defaults to the `core` runtime so `list-presets` and `headless-setup` are fast
*   `pexo --mcp` promotes the environment to the `mcp` runtime if needed
*   `pexo` promotes the environment to the `full` runtime if needed, enabling the browser UI and LangGraph-backed orchestration
*   `pexo --promote vector` adds native Chroma vector embeddings when the user wants semantic memory enabled locally

If you want every dependency installed ahead of time, run:

```bash
pexo --promote full
pexo --promote vector
```

The installers also support custom or repo-local targets, so an existing checkout does not need to be cloned twice:

**Windows:**
```powershell
.\install.cmd -RepoPath C:\Users\<USER>\code\pexo-agent -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
.\install.cmd -InstallDir C:\Tools\pexo -HeadlessSetup -Preset efficient_operator
```

**macOS/Linux:**
```bash
./install.sh --repo-path ~/code/pexo-agent --allow-repo-install --headless-setup --preset efficient_operator
./install.sh --use-current-checkout --allow-repo-install --headless-setup --preset efficient_operator
./install.sh --install-dir ~/tools/pexo --headless-setup --preset efficient_operator
```

The web interface is no longer required for first-run setup. Use `pexo` later when you want the localhost dashboard for inspecting memory, editing agents, correcting stored memories, adjusting profile settings, reviewing execution telemetry, or managing additional backups.

Use `pexo doctor` any time you want to confirm the current install mode, state directory, runtime profile, MCP command, and upgrade or uninstall path.

### Uninstallation

If Pexo is installed from a repo checkout, the fastest removal path is:

```bash
pexo --uninstall
```

`pexo uninstall` is also supported.

If Pexo is installed as a packaged tool, remove the tool with your package manager and optionally delete the local state directory:

```bash
uv tool uninstall pexo-agent
rm -rf ~/.pexo
```

If you need a raw script-driven uninstall, execute the following command:

**Windows:**
```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/uninstall.ps1 -OutFile uninstall.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

**macOS/Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/uninstall.sh | bash
```

### Native MCP Configuration (Recommended)

To expose Pexo's capabilities directly to an MCP-compliant application (e.g., Cursor, Claude Desktop), append the following configuration to the application's MCP settings. `pexo-mcp` and `pexo --mcp` both start in a quiet stdio mode and skip the interactive browser-launch workflow.

Once connected, the MCP server can drive:

*   profile read/update and preset setup
*   core/custom agent inspection and editing
*   memory search, storage, editing, deletion, and maintenance
*   orchestration intake, execution, task polling, and result submission
*   telemetry and session inspection
*   Genesis tool register/read/update/execute/delete
*   backup execution

If you are using the packaged install, the simplest configuration is to call `pexo-mcp` directly:

**Cross-platform packaged install MCP config:**
```json
{
  "mcpServers": {
    "pexo": {
      "command": "pexo-mcp",
      "args": []
    }
  }
}
```

If you want MCP from an existing checkout instead of a packaged install, initialize that checkout in place and point the client at the repo-local launcher:

**Windows repo-local MCP setup:**
```powershell
gh repo clone ParadoxGods/pexo-agent C:\Users\<USER>\code\pexo-agent
cd C:\Users\<USER>\code\pexo-agent
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -InstallProfile mcp -HeadlessSetup -Preset efficient_operator -SkipUpdate
```

```json
{
  "mcpServers": {
    "pexo": {
      "command": "cmd.exe",
      "args": ["/c", "C:\\Users\\<USER>\\code\\pexo-agent\\pexo.bat", "--mcp"]
    }
  }
}
```

**Windows Configuration:**
```json
{
  "mcpServers": {
    "pexo": {
      "command": "cmd.exe",
      "args": ["/c", "pexo", "--mcp"]
    }
  }
}
```

**macOS/Linux Configuration:**
```json
{
  "mcpServers": {
    "pexo": {
      "command": "bash",
      "args": ["-c", "pexo --mcp"]
    }
  }
}
```

## System Requirements

*   Python 3.11 or higher
*   Git
*   *Windows Environments:* Microsoft C++ Build Tools (required for local ChromaDB compilation)

## Architecture Integrity

Pexo ensures absolute data sovereignty. All configuration parameters, memory embeddings, artifacts, and agent prompts are stored locally either in the repo checkout (checkout mode) or the user-local state directory `~/.pexo` (packaged mode). No telemetry or state data is transmitted externally.

## Command Surface

The launcher exposes the following setup and administration commands:

*   `pexo --list-presets` or `pexo list-presets`
*   `pexo --headless-setup --preset efficient_operator`
*   `pexo --promote full`
*   `pexo --promote vector`
*   `pexo --update` or `pexo update`
*   `pexo --doctor` or `pexo doctor`
*   `pexo --no-browser`
*   `pexo --offline` or `pexo --skip-update`
*   `pexo --uninstall` or `pexo uninstall`
*   `pexo --mcp`
*   `pexo-mcp`
*   `pexo`

For GitHub-native tool installs, the preferred commands are now:

```bash
uv tool install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0.1"
pexo headless-setup --preset efficient_operator
pexo doctor
pexo
```

The installation scripts also support an AI-friendly one-shot terminal setup path:

**Windows:**
```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/install.ps1 -OutFile install.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -HeadlessSetup -Preset efficient_operator
```

**macOS/Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/install.sh | bash -s -- --headless-setup --preset efficient_operator
```

To target an existing checkout instead of creating `~/.pexo`:

**Windows:**
```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/install.ps1 -OutFile install.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -RepoPath C:\Users\<USER>\code\pexo-agent -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

**macOS/Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/install.sh | bash -s -- --repo-path ~/code/pexo-agent --allow-repo-install --headless-setup --preset efficient_operator
```

For deterministic installs that skip repository update checks:

**Windows:**
```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/install.ps1 -OutFile install.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -HeadlessSetup -Preset efficient_operator -SkipUpdate
```

**macOS/Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/install.sh | bash -s -- --headless-setup --preset efficient_operator --skip-update
```
