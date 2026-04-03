# AGENTS.md

This repository is designed to be installed and operated by AI agents.

## Default behavior

1. Prefer the standalone bootstrap path first.
2. Do not touch the current repo unless the user explicitly asks for a repo-local node.
3. Complete first-run setup in the terminal.
4. Run `pexo doctor` after setup.
5. Do not open the browser UI unless the user asks for it.

## Canonical Install

Use the standalone bootstrap path first. It prefers packaged install automatically, falls back safely, completes headless setup, connects supported clients, runs `pexo doctor`, and emits `PEXO_INSTALL_SUMMARY_JSON=...`.

Windows:

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/ParadoxGods/pexo-agent/v1.0.3/bootstrap.ps1 -OutFile bootstrap.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/ParadoxGods/pexo-agent/v1.0.3/bootstrap.sh | bash
```

## Direct Packaged Install

Use this only if the bootstrap path is unavailable.

```bash
uv tool install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0.3"
pexo headless-setup --preset efficient_operator
pexo connect all --scope user
pexo doctor
```

```bash
pipx install "git+https://github.com/ParadoxGods/pexo-agent.git@v1.0.3"
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
- `pexo uninstall`

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
