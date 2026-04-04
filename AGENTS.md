# AGENTS.md

This repository is designed to be installed and operated by AI agents.

## Default behavior

1. Prefer the latest GitHub Release install bundle first.
2. Do not touch the current repo unless the user explicitly asks for a repo-local node.
3. Complete first-run setup in the terminal.
4. Run `pexo doctor` after setup.
5. Do not open the browser UI unless the user asks for it.
6. Do not execute raw remote scripts unless the user explicitly asks for the legacy bootstrap path.

## Canonical Install

Use the latest GitHub Release install bundle first. It is the default install path because it uses immutable release assets instead of executing raw remote scripts, completes headless setup, connects supported clients, runs `pexo doctor`, and emits `PEXO_INSTALL_SUMMARY_JSON=...`.

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

## Direct Packaged Install

Use this only if the release bundle path is unavailable.

```bash
uv tool install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

Packaged installs keep mutable state under `~/.pexo` by default.

## Checkout Install

Use checkout mode only when the user explicitly wants a repo-local node. Existing Git checkouts are protected by default.

Windows:

```powershell
.\install.cmd -UseCurrentCheckout -AllowRepoInstall -HeadlessSetup -Preset efficient_operator
```

macOS/Linux:

```bash
./install.sh --use-current-checkout --allow-repo-install --headless-setup --preset efficient_operator
```

Never write `.pexo-deps-profile` manually. It is not a substitute for dependency installation.

## After Install

Run:

```bash
pexo connect all --scope user
pexo doctor
```

Useful commands:

- `pexo`
- `pexo --no-browser`
- `pexo --mcp`
- `pexo-mcp`
- `pexo promote full`
- `pexo promote vector`
- `pexo update`
  Use this to update any existing Pexo install in place.
- `pexo uninstall`

## Simple Task Flow

When using Pexo from Codex, Claude, or Gemini, treat Pexo as the default local brain.

Default call order:

1. `pexo_exchange`
2. Reuse the returned `session_id` with `pexo_exchange`

Default exchange behavior:

- First call: send the user's plain-language request as `message`
- If Pexo asks a clarification question, send the user's answer back as `message` with the same `session_id`
- If Pexo returns `agent_instruction`, do the work internally and send the result back as `agent_result` with the same `session_id`
- Show only `user_message` to the user unless they explicitly ask for internals

Lower-level tools remain available if you need direct control:

- `pexo_bootstrap_brain`
- `pexo_start_task`
- `pexo_continue_task`
- `pexo_get_task_status`

Use these context tools around that flow:

- `pexo_recall_context`
- `pexo_remember_context`
- `pexo_attach_context`
- `pexo_attach_text_context`

Prefer the simplified task tools over the raw orchestration tools:

- `pexo_exchange`
- `pexo_start_task`
- `pexo_continue_task`
- `pexo_get_task_status`

Behavior rules:

1. Keep user-facing replies short and plain.
2. Use the returned `user_message` for the reply whenever available.
3. Keep `instruction` or `agent_instruction` internal unless the user explicitly asks for orchestration details.
4. Ask only the single clarification question returned by Pexo when one is required.
5. Continue the task through Pexo instead of making the user drive each internal step manually.
6. Use `pexo_recall_context` before asking the user to restate prior context.
7. Use `pexo_remember_context` and `pexo_attach_context` to keep Pexo useful across sessions.

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
