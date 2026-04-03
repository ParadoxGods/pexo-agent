# Pexo

Pexo is a local-first orchestration layer, memory store, and MCP server for AI coding workflows.

It runs on the local machine, keeps its state local, and gives AI clients a shared place to manage profile settings, agents, memory, tools, artifacts, and execution flow.

## What It Does

- Runs a local API, dashboard, and MCP server without requiring a background daemon.
- Stores local profile data, agents, memories, artifacts, and execution telemetry.
- Lets supported AI clients connect to the same local Pexo node through MCP.

## Install

The recommended path is the latest GitHub Release install bundle. It avoids raw remote script execution, installs from a versioned release asset, completes headless setup, connects supported AI clients, runs `pexo doctor`, and prints `PEXO_INSTALL_SUMMARY_JSON=...` at the end.

Windows:

```powershell
gh release download -R ParadoxGods/pexo-agent --pattern "pexo-install-windows.zip" --clobber
Expand-Archive .\pexo-install-windows.zip -DestinationPath . -Force
.\pexo-install\install.cmd
```

macOS/Linux:

```bash
gh release download -R ParadoxGods/pexo-agent --pattern "pexo-install-unix.tar.gz" --clobber
tar -xzf pexo-install-unix.tar.gz
./pexo-install/install.sh
```

If `gh` is unavailable, use a direct packaged install from a pinned tag:

```bash
uv tool install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0.5"
```

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0.5"
```

## Quick Start

1. Verify the install.

```bash
pexo doctor
```

2. Connect supported AI clients.

```bash
pexo connect all --scope user
```

3. Use Pexo.

```bash
pexo
```

Optional terminal-first commands:

```bash
pexo headless-setup --preset efficient_operator
pexo --no-browser
pexo --mcp
pexo-mcp
```

## AI Clients

Pexo can register itself with supported local clients:

```bash
pexo connect all --scope user
pexo connect codex --scope user
pexo connect claude --scope user
pexo connect gemini --scope user
```

Packaged installs expose a direct MCP entrypoint:

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

Repository-level AI install and usage rules live in `AGENTS.md`.

## Commands

- `pexo`: start the local API and dashboard
- `pexo --no-browser`: start the local API without opening the browser
- `pexo --mcp` or `pexo-mcp`: start the MCP server over stdio
- `pexo doctor`: show install, runtime, and client status
- `pexo connect all --scope user`: register Pexo with supported AI clients
- `pexo headless-setup --preset efficient_operator`: initialize the profile without the web UI
- `pexo promote full`: install the full local UI/runtime profile
- `pexo promote vector`: install local vector-memory dependencies
- `pexo update`: refresh a checkout install
- `pexo uninstall`: remove a checkout install

Packaged installs keep mutable state in `~/.pexo` by default. Override it with `PEXO_HOME` if needed.

## Repo-Local Install

Repo-local installs are for contributors or users who explicitly want a checkout-backed node. Existing Git checkouts are protected by default.

Windows:

```powershell
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

macOS/Linux:

```bash
./install.sh --use-current-checkout --allow-repo-install --headless-setup --preset efficient_operator
```

Use `-RepoPath` or `--repo-path` if you want to target an existing checkout without using the current working tree.

Legacy raw bootstrap scripts still exist, but they are fallback-only and should not be the default path for AI-driven installs.

## Uninstall

Checkout install:

```bash
pexo uninstall
```

Packaged install:

```bash
uv tool uninstall pexo-agent
rm -rf ~/.pexo
```
