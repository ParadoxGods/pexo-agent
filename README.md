# Pexo

Pexo is a local-first memory and control plane for AI work.

It gives Codex, Gemini, Claude, and other MCP-capable clients one shared local system for memory, artifacts, preferences, sessions, agents, and task state. Instead of each AI console becoming its own silo, Pexo keeps continuity on your machine and lets connected clients work against the same context.

Pexo is most useful as the layer underneath your AI tools, not as a replacement for them.

## Highlights

- One local memory layer shared by multiple AI clients.
- Local artifacts, preferences, sessions, and task state.
- MCP-first design for Codex, Gemini, Claude, and similar tools.
- Healthy default install without optional vector dependencies.
- Explicit trust model for local tool execution.

## Why Pexo

- Keep context local instead of trapped inside one AI client.
- Hand work from one model to another without restating the same project history.
- Store memory, artifacts, and decisions in one place on the local machine.
- Use MCP as a stable buffer between the user and multiple AI clients.
- Recover from client failures, model switching, or quota limits without losing project state.
- Inspect what the system knows through one local UI instead of guessing what each model remembers.

## What Pexo Actually Does

Pexo manages the local continuity layer:

- `Memory`
  Durable facts, decisions, preferences, summaries, and lessons learned.
- `Artifacts`
  Files, notes, imported docs, and generated context stored locally.
- `Sessions`
  Shared task state that another AI client can continue later.
- `Agents and tools`
  Local definitions for how work is organized and reused.
- `Routing and fallback`
  Chooses available backends based on task type, installed clients, and recent health.

That is the core value: Pexo turns repeated prompt reconstruction into reusable local system state.

## How It Works

1. Pexo runs locally and exposes a local MCP server.
2. Connected AI clients read and write the same local memory, artifacts, and session state.
3. Pexo preserves continuity even when you switch models, terminals, or clients.

This is why Pexo saves time: the expensive part of AI work is often rebuilding context. Pexo keeps that context on your machine so the next model starts from state instead of from scratch.

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

The release bundle installs Pexo, completes headless setup, connects supported clients, runs `pexo doctor`, and warms the local runtime.

If `gh` is unavailable, download the latest release asset manually from:

`https://github.com/ParadoxGods/pexo-agent/releases`

Then extract it and run the included `install.cmd` or `install.sh`.

### Fallback Packaged Install

Use this only if the release bundle path is unavailable.

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.1.1"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

Packaged installs keep mutable state under `~/.pexo` by default. Override it with `PEXO_HOME` if you need Pexo state somewhere else.

## Start Using Pexo

The normal path is:

1. Start Pexo.
2. Use Codex, Gemini, or Claude normally.
3. Let Pexo hold the local memory and handoff state underneath.

## One-Minute Workflow

```powershell
pexo
pexo doctor --json
pexo connect all --scope user
```

Then use your AI client normally with one short instruction when needed:

```text
Use Pexo as the shared local brain for this task.
Review this repo, tell me the top 3 concrete issues,
and store the result in Pexo memory.
```

Start the local control plane:

```powershell
pexo
```

Useful follow-up commands:

```powershell
pexo doctor --json
pexo connect all --scope user
```

If Pexo is connected, use it as the shared local brain for ordinary tasks. Some clients will reach for it automatically. Others may need one short instruction in the prompt.

Open the local dashboard if you want to inspect state directly:

`http://127.0.0.1:9999/ui/`

## MCP First

Pexo is built around a local MCP surface so multiple AI clients can work against the same state.

You do not need to pick one AI console as the source of truth. Pexo keeps that state locally and makes it available to whichever connected client is working next.

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

## Safety Model

Pexo is designed to be a middle layer, so trust boundaries matter.

- Default installs are healthy without semantic vector memory.
- Local memory uses SQLite and keyword-backed retrieval by default.
- Optional semantic vector memory can be added later, but it is not required for a normal install.
- Genesis tool execution is not unrestricted by default.
- The default Genesis trust mode is `approval-required`.
- Tool mutation and broad local execution require explicit host trust via `full-local-exec`.

In other words: a normal install works out of the box, and the more dangerous local-exec path is opt-in.

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
  Connect supported local AI clients to `pexo-mcp`.
- `pexo warmup`
  Prime local state after install or update.
- `pexo promote full`
  Repair or reinstall the standard local runtime.
- `pexo promote vector`
  Add optional semantic-memory support.
- `pexo uninstall`
  Remove the current install.
- `pexo uninstall --keep-state`
  Remove the install but preserve local memory, artifacts, and state.

## Maintenance

Routine maintenance is usually just:

```powershell
pexo --update
pexo doctor --json
```

If MCP client wiring drifts:

```powershell
pexo connect all --scope user
```

Important note: semantic vector memory is optional. A default install is healthy and usable without it.

## Repo-Local Mode

Checkout mode is for contributors or users who explicitly want a repo-backed node.

Windows:

```powershell
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

macOS/Linux:

```bash
./install.sh --use-current-checkout --allow-repo-install --headless-setup --preset efficient_operator
```

Checkout mode keeps mutable state under the repo-local `.pexo` directory.

## Direct Chat

Pexo also has first-party direct chat surfaces, but they are optional:

- `pexo`
  Starts the local API and browser control plane.
- `pexo --chat`
  Starts terminal chat.

Direct chat is useful when you want to talk to Pexo itself, but the primary product value is still the shared local memory and control layer underneath your AI clients.

## Uninstall

Remove the current Pexo install and local state:

```powershell
pexo uninstall
```

Keep local memory, artifacts, and state:

```powershell
pexo uninstall --keep-state
```

## Bottom Line

If you use more than one AI client, switch models often, or care about keeping project context local, Pexo gives you one place to keep continuity.

It is the local layer that makes multiple AI tools behave more like one working system.
