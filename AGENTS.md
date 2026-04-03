# AGENTS.md

This repository is designed to be installed and operated by AI coding agents.

If a user tells you to install or use Pexo, follow this file exactly.

## Default behavior

1. Treat Pexo as the orchestration layer, memory layer, and MCP control plane for this repository.
2. Prefer the standalone bootstrap path first because it removes the most installer decisions.
3. Do not install into the current repository checkout unless the user explicitly asks for a repo-local node.
4. Complete first-run setup in the terminal. Do not force the browser UI during install.
5. Run `pexo doctor` after setup to verify the install.
6. Start the local UI only when the user explicitly wants it.

## Install rules

### Canonical install

Use the standalone bootstrap path first. It is the default install path because it:

- prefers packaged GitHub install automatically
- tries `uv` first, then `pipx`
- falls back to a safe checkout install only when needed
- completes headless setup automatically
- connects supported AI clients automatically when they are installed locally
- runs `pexo doctor`
- emits `PEXO_INSTALL_SUMMARY_JSON=...` for machine-readable confirmation

Windows:

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/ParadoxGods/pexo-agent/v1.0.2/bootstrap.ps1 -OutFile bootstrap.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/ParadoxGods/pexo-agent/v1.0.2/bootstrap.sh | bash
```

### Direct packaged install

If the bootstrap path is unavailable but a packaged tool manager is present, use a packaged install directly.

With `uv`:

```bash
uv tool install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0.2"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

With `pipx`:

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0.2"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

Packaged installs keep mutable state under `~/.pexo` by default:

- `~/.pexo/pexo.db`
- `~/.pexo/chroma_db/`
- `~/.pexo/artifacts/`
- `~/.pexo/dynamic_tools/`

### Checkout installer fallback

If packaged install is unavailable, or the user explicitly wants a checkout-based install, use the platform installer.

Windows:

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/install.ps1 -OutFile install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -HeadlessSetup -Preset efficient_operator
```

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/install.sh | bash -s -- --headless-setup --preset efficient_operator
```

### Repo-local installs are opt-in only

Existing Git checkouts are protected by default. If you need a repo-local node, the user must explicitly want that behavior.

Windows repo-local install:

```powershell
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

Windows existing checkout path:

```powershell
.\install.cmd -RepoPath C:\Users\<USER>\code\pexo-agent -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

macOS/Linux repo-local install:

```bash
./install.sh --use-current-checkout --allow-repo-install --headless-setup --preset efficient_operator
```

macOS/Linux existing checkout path:

```bash
./install.sh --repo-path ~/code/pexo-agent --allow-repo-install --headless-setup --preset efficient_operator
```

Never write `.pexo-deps-profile` manually. It is only a runtime marker and is not a substitute for dependency installation.

## Setup and runtime rules

1. Prefer terminal-first setup:
   - `pexo list-presets`
   - `pexo headless-setup --preset efficient_operator`
   - `pexo connect all --scope user`
   - `pexo doctor`
2. If the user wants all optional runtime pieces installed immediately:
   - `pexo promote full`
   - `pexo promote vector`
3. The packaged install is intentionally light. `pexo` will auto-promote the full runtime on first UI launch.
4. The web UI is optional. Use it later for:
   - memory inspection and correction
   - agent editing
   - profile and backup configuration
   - telemetry review
5. To start the local server without opening the browser:
   - `pexo --no-browser`
6. To start MCP:
   - `pexo --mcp`
   - or `pexo-mcp` for packaged installs
7. To connect supported AI clients:
   - `pexo connect all --scope user`
   - `pexo connect codex --scope user`
   - `pexo connect claude --scope user`
   - `pexo connect gemini --scope user`

## Update and uninstall rules

Use `pexo update` when the user explicitly wants an immediate refresh.

If the user asks to uninstall:

- checkout install:
  - `pexo uninstall`
  - `pexo --uninstall`
- packaged install:
  - `uv tool uninstall pexo-agent`
  - remove `~/.pexo` if the user also wants local state removed

Windows raw uninstall fallback:

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/uninstall.ps1 -OutFile uninstall.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

macOS/Linux raw uninstall fallback:

```bash
curl -fsSL https://raw.githubusercontent.com/ParadoxGods/pexo-agent/master/uninstall.sh | bash
```

## Operational rules after install

1. Prefer structured MCP tools whenever the connected client exposes them.
2. For regular tasks, use the intake/execute loop:
   - `POST http://localhost:9999/orchestrator/intake`
   - ask the single clarifying question returned by Pexo
   - `POST http://localhost:9999/orchestrator/execute`
3. Before major execution, query memory:
   - `POST http://localhost:9999/memory/search`
4. After major implementation work, store memory:
   - `POST http://localhost:9999/memory/store`
5. If the user corrects the system or a mistake reveals a reusable lesson:
   - `POST http://localhost:9999/evolve`
6. If a needed capability is missing, create and register a tool:
   - write a Python tool
   - `POST http://localhost:9999/tools/register`
7. Keep all durable state in Pexo's local database and memory system.

## First action for an AI agent

If the user says to install Pexo, do this:

1. Use the standalone bootstrap path if possible.
2. Fall back to direct packaged install only if the bootstrap path is unavailable.
3. Fall back to the platform installer only if needed.
4. Do not touch the current repo unless the user explicitly asked for a repo-local node.
5. Complete `headless-setup --preset efficient_operator`.
6. Run `pexo connect all --scope user`.
7. Run `pexo doctor`.
8. Do not launch the browser UI unless the user asks for it.
