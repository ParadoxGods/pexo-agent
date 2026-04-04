# Pexo

Pexo is a local-first brain and control plane for AI work.

It gives Codex, Gemini, Claude, and other MCP-capable clients one shared local place for memory, artifacts, agents, tools, sessions, and task flow. Users can also talk to Pexo directly in the browser or terminal without living inside an AI client console.

## What It Does

- Runs a local API, dashboard, terminal chat, and MCP server.
- Keeps project context, memories, artifacts, and agent state on the local machine.
- Lets multiple AI clients share the same local brain on the same project.
- Routes work to available backends based on the kind of request.
- Falls back cleanly when a preferred backend is not installed or not healthy.

## Install

Use the latest GitHub Release bundle.

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

The bundle installs Pexo, runs headless setup, connects supported local AI clients, runs `pexo doctor`, warms the runtime, and prints `PEXO_INSTALL_SUMMARY_JSON=...` when it finishes.

Fallback only if the release bundle path is unavailable:

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

## Use

After install, there are three normal entry points:

- `pexo`
  Start the local API and browser control plane.
- `pexo --chat`
  Talk to Pexo directly in the terminal.
- `pexo-mcp` or `pexo --mcp`
  Run the MCP server only.

If Pexo is connected to Codex, Gemini, or Claude, the model should use it automatically as the default local brain for normal tasks.

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

## Direct Chat

Pexo can respond directly and also coordinate real work behind the scenes.

- Simple local facts like name, date, and time are answered immediately.
- Fast factual lookups use a web fact path.
- Stored-context questions prefer local Pexo memory and artifacts first.
- Concrete work requests can open a real Pexo task session and continue in the background.
- In `auto` mode, Pexo chooses among installed backends based on request type, availability, and observed performance.

Today, the broad routing behavior is:

- search and factual lookups prefer Gemini first
- coding and repo work prefer Codex first
- frontend and website work prefer Codex first
- image and visual asset work prefer Codex first, then Gemini
- missing clients fall back to whatever supported backend is installed

## Commands

- `pexo`
- `pexo --chat`
- `pexo --no-browser`
- `pexo-mcp` or `pexo --mcp`
- `pexo --update`
- `pexo warmup`
- `pexo doctor`
- `pexo connect all --scope user`
- `pexo promote vector`

Packaged installs keep mutable state in `~/.pexo` by default. Override it with `PEXO_HOME` if needed.

## Repo-Local Install

Checkout mode is for contributors or users who explicitly want a checkout-backed node.

Windows:

```powershell
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

macOS/Linux:

```bash
./install.sh --use-current-checkout --allow-repo-install --headless-setup --preset efficient_operator
```

Existing Git checkouts are protected by default.

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
