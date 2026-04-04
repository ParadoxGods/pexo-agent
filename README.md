# Pexo

Pexo is a local-first memory, orchestration, and MCP layer for AI work.

It gives Codex, Claude, Gemini, and other MCP-capable clients one shared local brain for profile settings, agents, memories, artifacts, tools, and task flow.

## Install

Use the latest GitHub Release bundle.

The bundle installs Pexo, runs headless setup, connects supported local AI clients, runs `pexo doctor`, and prints `PEXO_INSTALL_SUMMARY_JSON=...` when it finishes.

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

Fallback only if the release bundle path is unavailable:

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

## Use

Most users should not need to think about Pexo after install.

- If Pexo is connected to the AI client, the model should use it automatically as the default local brain.
- If you want to talk to Pexo directly in the browser, run `pexo`.
- If you want to talk to Pexo directly in the terminal, run `pexo --chat`.
- If you want the MCP server only, use `pexo-mcp` or `pexo --mcp`.

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

## Commands

- `pexo`: start the local API and dashboard
- `pexo --chat`: start direct terminal chat with Pexo
- `pexo --no-browser`: start the local API without opening the browser
- `pexo-mcp` or `pexo --mcp`: start the MCP server over stdio
- `pexo --update`: update the current install
- `pexo doctor`: check install, runtime, and client status
- `pexo connect all --scope user`: connect supported AI clients
- `pexo promote vector`: install local vector-memory support

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
