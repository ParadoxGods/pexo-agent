# Pexo

Pexo is a local-first brain and control plane for AI work.

It gives Codex, Gemini, Claude, and other MCP-capable clients one shared local place for memory, artifacts, preferences, agents, sessions, and task state. Instead of each AI console becoming its own silo, Pexo keeps the continuity on your machine and lets connected clients work against the same local context.

## Why Pexo

- Keep project memory local instead of trapped inside one AI client.
- Hand work from one model to another without restating everything.
- Store artifacts, decisions, preferences, and session state in one place.
- Use MCP as a stable buffer between the user and multiple AI tools.
- Recover faster from client failure, quota limits, or tool switching.

Pexo is most useful as the layer underneath Codex, Gemini, Claude, or another MCP-capable client. It is not just another chat UI.

## Install

Use the latest GitHub Release bundle. This is the canonical install path.

Windows:

```powershell
gh release download -R ParadoxGods/pexo-agent -p pexo-install-windows.zip --clobber
tar -xf .\pexo-install-windows.zip
.\install.cmd
```

macOS/Linux:

```bash
gh release download -R ParadoxGods/pexo-agent -p pexo-install-unix.tar.gz --clobber
tar -xzf pexo-install-unix.tar.gz
./install.sh
```

The release bundle installs Pexo, runs headless setup, connects supported local AI clients, runs `pexo doctor`, and warms the runtime.

## Start Using Pexo

The normal path is simple:

1. Start the local control plane.
2. Use Codex, Gemini, or Claude normally.
3. Let Pexo hold the memory and handoff state underneath.

Start Pexo:

```powershell
pexo
```

If you want to verify the install or reconnect clients:

```powershell
pexo doctor --json
pexo connect all --scope user
```

If Pexo is connected to your AI clients, use it as the shared local brain for ordinary tasks. Some clients will reach for it automatically; others may need a short instruction in the prompt.

Example prompt:

```text
Use Pexo as the shared local brain for this task.
Review this repo, tell me the top 3 concrete issues,
and store the result in Pexo memory.
```

## What Pexo Handles

Pexo is built to manage the local continuity layer:

- Memory
  Durable facts, decisions, preferences, and summaries.
- Artifacts
  Files, notes, imported docs, and generated context.
- Sessions
  Shared task state that another client can continue later.
- Agents and tools
  Local definitions for how work is organized and reused.
- Routing and fallback
  Chooses available backends based on task type, installed clients, and health.

This is what makes Pexo valuable: it turns repeated prompt reconstruction into reusable local system state.

## MCP First

Pexo exposes a local MCP surface so multiple AI clients can work against the same state.

Packaged installs expose this MCP entrypoint:

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

Repository-level AI usage rules live in `AGENTS.md`.

## Optional Direct Chat

Pexo also has first-party direct chat surfaces, but they are optional:

- `pexo`
  Starts the local API and browser control plane.
- `pexo --chat`
  Starts direct terminal chat.
- `pexo --mcp` or `pexo-mcp`
  Starts MCP only.

Direct chat is useful when you want to talk to Pexo itself, but the primary product value is still the local memory and control layer shared across AI clients.

## Commands

- `pexo`
  Start the local control plane.
- `pexo --chat`
  Talk to Pexo directly in the terminal.
- `pexo --no-browser`
  Start the local API without opening the browser.
- `pexo --mcp` or `pexo-mcp`
  Start MCP only.
- `pexo --update`
  Update the current Pexo install.
- `pexo doctor`
  Print local installation and runtime diagnostics.
- `pexo connect all --scope user`
  Connect supported AI clients to `pexo-mcp`.
- `pexo warmup`
  Prime local state after install or update.
- `pexo promote full`
  Repair or reinstall the standard local runtime.
- `pexo promote vector`
  Add optional advanced semantic-memory support.

## State And Maintenance

Packaged installs keep mutable state under `~/.pexo` by default. Override it with `PEXO_HOME` if needed.

Routine maintenance is usually just:

```powershell
pexo --update
pexo doctor --json
```

If client wiring drifts:

```powershell
pexo connect all --scope user
```

Important note: semantic vector memory is optional. A default install is healthy and usable without it.

## Repo-Local Install

Checkout mode is for contributors or users who explicitly want a repo-backed node.

Windows:

```powershell
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

macOS/Linux:

```bash
./install.sh --use-current-checkout --allow-repo-install --headless-setup --preset efficient_operator
```

Checkout mode keeps its mutable state under the repo-local `.pexo` directory.

## Uninstall

Show uninstall guidance for the current install mode:

```powershell
pexo uninstall
```

Typical packaged uninstall:

```powershell
uv tool uninstall pexo-agent
```

If you also want to remove local state, delete the Pexo home directory afterward.
