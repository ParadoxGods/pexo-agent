#!/bin/bash
set -euo pipefail

BUNDLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_ROOT="${HOME}/.pexo"
INSTALL_METADATA_PATH="${STATE_ROOT}/.pexo-install.json"
MANIFEST_PATH="${BUNDLE_ROOT}/pexo-install-manifest.json"
PRESET="efficient_operator"
PROFILE_NAME="default_user"
BACKUP_PATH=""
CONNECT_CLIENTS="all"
SKIP_DOCTOR=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --preset) PRESET="$2"; shift 2 ;;
        --name) PROFILE_NAME="$2"; shift 2 ;;
        --backup-path) BACKUP_PATH="$2"; shift 2 ;;
        --connect-clients) CONNECT_CLIENTS="$2"; shift 2 ;;
        --skip-doctor) SKIP_DOCTOR=1; shift ;;
        *) echo "Unknown installer option: $1" >&2; exit 1 ;;
    esac
done

print_step() {
    local percent="$1"
    local status="$2"
    local width=28
    local filled=$(( percent * width / 100 ))
    local empty=$(( width - filled ))
    local bar
    printf -v bar '%*s' "$filled" ''
    bar="${bar// /#}"
    local gap
    printf -v gap '%*s' "$empty" ''
    gap="${gap// /-}"
    printf '[%s%s] %3s%% %s\n' "$bar" "$gap" "$percent" "$status"
}

resolve_python_command() {
    if command -v python3 >/dev/null 2>&1; then echo "python3"; return; fi
    if command -v python >/dev/null 2>&1; then echo "python"; return; fi
    return 1
}

append_session_path() {
    local entry="$1"
    if [ -z "$entry" ]; then return; fi
    case ":$PATH:" in
        *":$entry:"*) ;;
        *) export PATH="$PATH:$entry" ;;
    esac
}

append_shell_path() {
    local entry="$1"
    if [ -z "$entry" ]; then return; fi
    grep -Fqx "export PATH=\"\$PATH:$entry\"" "$HOME/.bashrc" || echo "export PATH=\"\$PATH:$entry\"" >> "$HOME/.bashrc"
    grep -Fqx "export PATH=\"\$PATH:$entry\"" "$HOME/.zshrc" || echo "export PATH=\"\$PATH:$entry\"" >> "$HOME/.zshrc"
}

find_wheel() {
    local wheel_path
    wheel_path="$(find "$BUNDLE_ROOT" -maxdepth 1 -name 'pexo_agent-*-py3-none-any.whl' | sort | head -n 1)"
    if [ -z "$wheel_path" ]; then echo "No wheel asset was found in $BUNDLE_ROOT." >&2; exit 1; fi
    printf '%s\n' "$wheel_path"
}

wheel_version() {
    local wheel_path="$1"
    basename "$wheel_path" | sed -e 's/^pexo_agent-//' -e 's/-py3-none-any\.whl$//'
}

verify_wheel_checksum() {
    local wheel_path="$1"
    local checksum_file="$BUNDLE_ROOT/SHA256SUMS.txt"
    if [ ! -f "$checksum_file" ]; then echo "SHA256SUMS.txt is missing from the install bundle." >&2; exit 1; fi
    local wheel_name expected actual
    wheel_name="$(basename "$wheel_path")"
    expected="$(grep -F "$wheel_name" "$checksum_file" | awk '{print $1}' | head -n 1)"
    if [ -z "$expected" ]; then echo "The checksum file does not contain an entry for $wheel_name." >&2; exit 1; fi
    actual="$("$PYTHON_CMD" - "$wheel_path" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys
path = Path(sys.argv[1])
digest = sha256()
with path.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
)"
    if [ "$actual" != "$expected" ]; then echo "SHA256 mismatch for $wheel_name." >&2; exit 1; fi
}

write_install_metadata() {
    local version="$1" method="$2" command_path="$3" mcp_command="$4" uninstall_command="$5" update_command="$6" wheel_sha="$7" dependency_fingerprint="$8"
    mkdir -p "$STATE_ROOT"
    "$PYTHON_CMD" - "$INSTALL_METADATA_PATH" "$version" "$method" "$command_path" "$mcp_command" "$uninstall_command" "$update_command" "$wheel_sha" "$dependency_fingerprint" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
payload = {
    "version": sys.argv[2],
    "method": sys.argv[3],
    "release": f"https://github.com/ParadoxGods/pexo-agent/releases/tag/v{sys.argv[2]}",
    "command_path": sys.argv[4],
    "mcp_command": sys.argv[5],
    "wheel_sha256": sys.argv[8],
    "dependency_fingerprint": sys.argv[9],
    "guidance": {"uninstall": sys.argv[6], "update": sys.argv[7]},
}
path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
PY
}

emit_summary() {
    local version="$1" method="$2" command_path="$3" mcp_command="$4"
    "$PYTHON_CMD" - "$version" "$method" "$STATE_ROOT" "$command_path" "$mcp_command" <<'PY'
import json, sys
summary = {
    "version": sys.argv[1],
    "install_method": sys.argv[2],
    "state_root": sys.argv[3],
    "command": sys.argv[4],
    "mcp_command": sys.argv[5],
    "next": ["pexo doctor", "pexo connect all --scope user", "pexo"],
}
print("PEXO_INSTALL_SUMMARY_JSON=" + json.dumps(summary, separators=(",", ":")))
PY
}

PYTHON_CMD="$(resolve_python_command)" || { echo "Python 3.11 or newer is required." >&2; exit 1; }
print_step 5 "Preparing install bundle"
WHEEL_PATH="$(find_wheel)"
VERSION="$(wheel_version "$WHEEL_PATH")"
verify_wheel_checksum "$WHEEL_PATH"
WHEEL_SHA256="$("$PYTHON_CMD" - "$MANIFEST_PATH" <<'PY'
import json, sys
from pathlib import Path
manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(manifest["wheel"]["sha256"])
PY
)"
DEPENDENCY_FINGERPRINT="$("$PYTHON_CMD" - "$MANIFEST_PATH" <<'PY'
import json, sys
from pathlib import Path
manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(manifest["dependency_fingerprint"])
PY
)"

INSTALL_METHOD=""
COMMAND_PATH=""
MCP_COMMAND=""
UNINSTALL_GUIDANCE=""
UPDATE_GUIDANCE="pexo --update"

if command -v pipx >/dev/null 2>&1; then
    INSTALL_METHOD="release_bundle_pipx"
    print_step 20 "Installing Pexo with pipx"
    pipx install --force "$WHEEL_PATH"
    pipx ensurepath >/dev/null 2>&1 || true
    PIPX_BIN_DIR="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
    if [ -z "$PIPX_BIN_DIR" ]; then PIPX_BIN_DIR="$HOME/.local/bin"; fi
    append_session_path "$PIPX_BIN_DIR"
    append_shell_path "$PIPX_BIN_DIR"
    COMMAND_PATH="pexo"
    MCP_COMMAND="$(command -v pexo-mcp || true)"
    if [ -z "$MCP_COMMAND" ]; then MCP_COMMAND="pexo-mcp"; fi
    UNINSTALL_GUIDANCE="pexo uninstall"
else
    INSTALL_METHOD="release_bundle_managed_venv"
    VENV_PATH="$STATE_ROOT/venv"
    VENV_BIN="$VENV_PATH/bin"
    COMMAND_PATH="$VENV_BIN/pexo"
    MCP_COMMAND="$VENV_BIN/pexo-mcp"
    mkdir -p "$STATE_ROOT"
    if [ -d "$VENV_PATH" ]; then
        print_step 16 "Resetting managed runtime environment"
        rm -rf "$VENV_PATH"
    fi
    rm -f "$INSTALL_METADATA_PATH" "$STATE_ROOT/.pexo-deps-profile"
    print_step 20 "Creating isolated Python environment"
    "$PYTHON_CMD" -m venv "$VENV_PATH"
    print_step 35 "Ensuring pip is available"
    "$VENV_BIN/python" -m ensurepip --upgrade
    print_step 50 "Installing the Pexo wheel"
    "$VENV_BIN/python" -m pip install --disable-pip-version-check --force-reinstall "$WHEEL_PATH"
    append_session_path "$VENV_BIN"
    append_shell_path "$VENV_BIN"
    UNINSTALL_GUIDANCE="pexo uninstall"
fi

write_install_metadata "$VERSION" "$INSTALL_METHOD" "$COMMAND_PATH" "$MCP_COMMAND" "$UNINSTALL_GUIDANCE" "$UPDATE_GUIDANCE" "$WHEEL_SHA256" "$DEPENDENCY_FINGERPRINT"

print_step 72 "Running headless setup"
SETUP_ARGS=(headless-setup --preset "$PRESET" --name "$PROFILE_NAME")
if [ -n "$BACKUP_PATH" ]; then SETUP_ARGS+=(--backup-path "$BACKUP_PATH"); fi
"$COMMAND_PATH" "${SETUP_ARGS[@]}"
print_step 78 "Installing full local runtime"
"$COMMAND_PATH" promote full

if [ "$CONNECT_CLIENTS" != "none" ]; then
    print_step 84 "Connecting supported AI clients"
    "$COMMAND_PATH" connect "$CONNECT_CLIENTS" --scope user
fi

print_step 92 "Priming local runtime"
"$COMMAND_PATH" warmup --quiet

if [ "$SKIP_DOCTOR" -eq 0 ]; then
    print_step 97 "Running Pexo doctor"
    "$COMMAND_PATH" doctor
fi

print_step 100 "Pexo install completed"
emit_summary "$VERSION" "$INSTALL_METHOD" "$COMMAND_PATH" "$MCP_COMMAND"
echo "To uninstall later:"
echo "  pexo uninstall"
echo "To keep local memory and artifacts while removing the install:"
echo "  pexo uninstall --keep-state"
