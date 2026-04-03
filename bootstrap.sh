#!/bin/bash
set -euo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRESET="efficient_operator"
PROFILE_NAME="default_user"
BACKUP_PATH=""
REPOSITORY="ParadoxGods/pexo-agent"
REF="v1.0.6"
INSTALL_DIR=""
REPO_PATH=""
USE_CURRENT_CHECKOUT=0
ALLOW_REPO_INSTALL=0
INSTALL_PROFILE="auto"
CONNECT_CLIENTS="all"
SKIP_UPDATE=0
OFFLINE=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --preset)
            PRESET="$2"
            shift 2
            ;;
        --name)
            PROFILE_NAME="$2"
            shift 2
            ;;
        --backup-path)
            BACKUP_PATH="$2"
            shift 2
            ;;
        --repository)
            REPOSITORY="$2"
            shift 2
            ;;
        --ref)
            REF="$2"
            shift 2
            ;;
        --install-dir)
            INSTALL_DIR="$2"
            shift 2
            ;;
        --repo-path)
            REPO_PATH="$2"
            shift 2
            ;;
        --use-current-checkout)
            USE_CURRENT_CHECKOUT=1
            shift
            ;;
        --allow-repo-install)
            ALLOW_REPO_INSTALL=1
            shift
            ;;
        --install-profile)
            INSTALL_PROFILE="$2"
            shift 2
            ;;
        --connect-clients)
            CONNECT_CLIENTS="$2"
            shift 2
            ;;
        --skip-update)
            SKIP_UPDATE=1
            shift
            ;;
        --offline)
            SKIP_UPDATE=1
            OFFLINE=1
            shift
            ;;
        *)
            echo "Unknown bootstrap option: $1" >&2
            exit 1
            ;;
    esac
done

print_progress() {
    local percent="$1"
    local status="$2"
    printf '[%3s%%] %s\n' "$percent" "$status"
}

resolve_python_command() {
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
        return
    fi
    if command -v python >/dev/null 2>&1; then
        echo "python"
        return
    fi
    return 1
}

append_session_path() {
    local entry="$1"
    if [ -z "$entry" ]; then
        return
    fi
    case ":$PATH:" in
        *":$entry:"*) ;;
        *) export PATH="$PATH:$entry" ;;
    esac
}

packaged_install_tool() {
    if command -v uv >/dev/null 2>&1; then
        echo "uv"
        return
    fi
    if command -v pipx >/dev/null 2>&1; then
        echo "pipx"
        return
    fi
}

uv_bin_dir() {
    if ! command -v uv >/dev/null 2>&1; then
        return
    fi
    if uv tool dir --bin >/dev/null 2>&1; then
        uv tool dir --bin
    fi
}

pipx_bin_dir() {
    if ! command -v pipx >/dev/null 2>&1; then
        return
    fi
    if pipx environment --value PIPX_BIN_DIR >/dev/null 2>&1; then
        pipx environment --value PIPX_BIN_DIR
        return
    fi
    printf '%s\n' "$HOME/.local/bin"
}

emit_install_summary_json() {
    "$PYTHON_BOOTSTRAP_BIN" - "$@" <<'PY'
import json
import sys

payload = {}
for item in sys.argv[1:]:
    key, value = item.split("=", 1)
    if value == "__NULL__":
        payload[key] = None
    elif value.startswith("[") and value.endswith("]"):
        payload[key] = [part for part in value[1:-1].split("||") if part]
    else:
        payload[key] = value

print("PEXO_INSTALL_SUMMARY_JSON=" + json.dumps(payload, separators=(",", ":")))
PY
}

resolve_package_source() {
    local repository="$1"
    local ref="$2"
    local source

    if [[ "$repository" =~ ^git\+ ]]; then
        source="$repository"
    elif [[ "$repository" =~ ^https?:// ]]; then
        source="git+$repository"
    elif [[ "$repository" =~ ^git@ ]]; then
        source="$repository"
    elif [[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        source="git+https://github.com/$repository.git"
    else
        echo "Unsupported repository source: $repository" >&2
        exit 1
    fi

    if [ -n "$ref" ] && [[ "$source" != *@* ]]; then
        source="$source@$ref"
    fi
    printf '%s\n' "$source"
}

resolve_clone_source() {
    local repository="$1"
    if [[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        printf 'https://github.com/%s.git\n' "$repository"
        return
    fi
    if [[ "$repository" =~ ^git\+ ]]; then
        printf '%s\n' "${repository#git+}"
        return
    fi
    if [[ "$repository" =~ ^https?:// ]] || [[ "$repository" =~ ^git@ ]]; then
        printf '%s\n' "$repository"
        return
    fi
    echo "Unsupported repository source: $repository" >&2
    exit 1
}

resolve_full_path() {
    local input_path="$1"
    "$PYTHON_BOOTSTRAP_BIN" - "$input_path" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
}

run_checked() {
    local percent="$1"
    local status="$2"
    shift 2
    print_progress "$percent" "$status"
    "$@"
}

invoke_local_install() {
    local installer_path="$1"
    local args=("$installer_path" --headless-setup --preset "$PRESET" --name "$PROFILE_NAME" --repository "$REPOSITORY" --install-profile "$INSTALL_PROFILE")
    if [ -n "$BACKUP_PATH" ]; then
        args+=(--backup-path "$BACKUP_PATH")
    fi
    if [ -n "$INSTALL_DIR" ]; then
        args+=(--install-dir "$INSTALL_DIR")
    fi
    if [ -n "$REPO_PATH" ]; then
        args+=(--repo-path "$REPO_PATH")
    fi
    if [ "$USE_CURRENT_CHECKOUT" -eq 1 ]; then
        args+=(--use-current-checkout)
    fi
    if [ "$ALLOW_REPO_INSTALL" -eq 1 ]; then
        args+=(--allow-repo-install)
    fi
    if [ "$SKIP_UPDATE" -eq 1 ]; then
        args+=(--skip-update)
    fi
    if [ "$OFFLINE" -eq 1 ]; then
        args+=(--offline)
    fi

    run_checked 15 "Running local Pexo installer" bash "${args[@]}"
}

run_doctor() {
    local percent="$1"
    shift
    run_checked "$percent" "Running Pexo doctor" "$@" doctor
}

run_connect() {
    local percent="$1"
    local target="$2"
    shift 2
    if [ "$target" = "none" ]; then
        return
    fi
    run_checked "$percent" "Connecting AI clients to Pexo MCP" "$@" connect "$target" --scope user
}

PYTHON_BOOTSTRAP_BIN="$(resolve_python_command || true)"

if [ -f "$SCRIPT_ROOT/install.sh" ] && [ -d "$SCRIPT_ROOT/app" ]; then
    invoke_local_install "$SCRIPT_ROOT/install.sh"
    if [ -x "$SCRIPT_ROOT/pexo" ]; then
        run_doctor 92 "$SCRIPT_ROOT/pexo"
        run_connect 97 "$CONNECT_CLIENTS" "$SCRIPT_ROOT/pexo"
    fi
    print_progress 100 "Bootstrap complete"
    exit 0
fi

if [ "$USE_CURRENT_CHECKOUT" -eq 1 ] || [ -n "$REPO_PATH" ]; then
    echo "Standalone bootstrap does not support repo-local install. Clone the repository first, then run the local bootstrap or install wrapper from that checkout." >&2
    exit 1
fi

PACKAGED_TOOL="$(packaged_install_tool)"
REQUESTED_PROFILE="$INSTALL_PROFILE"
if [ "$REQUESTED_PROFILE" = "auto" ]; then
    REQUESTED_PROFILE="mcp"
fi
STATE_ROOT="$HOME/.pexo"
PACKAGE_SOURCE="$(resolve_package_source "$REPOSITORY" "$REF")"

if [ -n "$PACKAGED_TOOL" ]; then
    print_progress 5 "Using packaged GitHub install via $PACKAGED_TOOL"
    if [ "$PACKAGED_TOOL" = "uv" ]; then
        run_checked 20 "Installing packaged Pexo tool" uv tool install --reinstall "$PACKAGE_SOURCE"
        run_checked 35 "Updating shell integration" uv tool update-shell
        append_session_path "$(uv_bin_dir)"
    else
        run_checked 20 "Installing packaged Pexo tool" pipx install --force "$PACKAGE_SOURCE"
        run_checked 35 "Updating shell integration" pipx ensurepath
        append_session_path "$(pipx_bin_dir)"
    fi

    if ! command -v pexo >/dev/null 2>&1; then
        echo "Packaged install completed, but the 'pexo' command is not visible in this shell." >&2
        exit 1
    fi

    if [ "$REQUESTED_PROFILE" = "full" ] || [ "$REQUESTED_PROFILE" = "vector" ]; then
        run_checked 60 "Promoting runtime to $REQUESTED_PROFILE" pexo promote "$REQUESTED_PROFILE"
    fi

    HEADLESS_ARGS=(headless-setup --preset "$PRESET" --name "$PROFILE_NAME")
    if [ -n "$BACKUP_PATH" ]; then
        HEADLESS_ARGS+=(--backup-path "$BACKUP_PATH")
    fi
    run_checked 80 "Applying headless setup" pexo "${HEADLESS_ARGS[@]}"
    run_doctor 92 pexo
    run_connect 97 "$CONNECT_CLIENTS" pexo
    print_progress 100 "Bootstrap complete"
    emit_install_summary_json \
      "status=success" \
      "install_mode=bootstrap_packaged" \
      "packaged_tool=$PACKAGED_TOOL" \
      "package_source=$PACKAGE_SOURCE" \
      "install_directory=managed by $PACKAGED_TOOL" \
      "state_directory=$STATE_ROOT" \
      "active_profile=$( [ "$REQUESTED_PROFILE" = "full" ] || [ "$REQUESTED_PROFILE" = "vector" ] && printf '%s' "$REQUESTED_PROFILE" || printf 'mcp' )" \
      "profile_initialized=$PROFILE_NAME" \
      "backup_path=$( [ -n "$BACKUP_PATH" ] && printf '%s' "$BACKUP_PATH" || printf 'not set' )" \
      "connected_clients=$CONNECT_CLIENTS" \
      "launcher_command=pexo" \
      "mcp_command=pexo-mcp" \
      "uninstall_command=$( [ "$PACKAGED_TOOL" = "uv" ] && printf 'uv tool uninstall pexo-agent' || printf 'pipx uninstall pexo-agent' )" \
      "next=[pexo connect all --scope user||pexo||pexo --mcp]"
    exit 0
fi

if ! command -v git >/dev/null 2>&1; then
    echo "Git is required for the checkout fallback path." >&2
    exit 1
fi
if [ -z "$PYTHON_BOOTSTRAP_BIN" ]; then
    echo "Python 3.11 or newer is required for bootstrap fallback and summary generation." >&2
    exit 1
fi

TARGET_DIR="$HOME/.pexo"
if [ -n "$INSTALL_DIR" ]; then
    TARGET_DIR="$(resolve_full_path "$INSTALL_DIR")"
fi
CLONE_SOURCE="$(resolve_clone_source "$REPOSITORY")"

if [ ! -d "$TARGET_DIR" ]; then
    run_checked 20 "Cloning Pexo checkout fallback" git clone "$CLONE_SOURCE" "$TARGET_DIR"
fi

if [ ! -f "$TARGET_DIR/install.sh" ]; then
    echo "Checkout fallback directory '$TARGET_DIR' does not contain install.sh." >&2
    exit 1
fi

if [ -n "$REF" ]; then
    run_checked 35 "Fetching checkout tags" git -C "$TARGET_DIR" fetch --tags --quiet
    run_checked 45 "Pinning checkout to $REF" git -C "$TARGET_DIR" checkout "$REF"
fi

INSTALL_DIR=""
REPO_PATH="$TARGET_DIR"
ALLOW_REPO_INSTALL=1
USE_CURRENT_CHECKOUT=0
SKIP_UPDATE=1
invoke_local_install "$TARGET_DIR/install.sh"
run_doctor 92 "$TARGET_DIR/pexo"
run_connect 97 "$CONNECT_CLIENTS" "$TARGET_DIR/pexo"
print_progress 100 "Bootstrap complete"
