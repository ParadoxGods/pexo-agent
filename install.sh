#!/bin/bash
set -euo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_PEXO_DIR="$HOME/.pexo"
PEXO_DIR="$DEFAULT_PEXO_DIR"
HEADLESS_SETUP=0
PRESET="efficient_operator"
PROFILE_NAME="default_user"
BACKUP_PATH=""
REPOSITORY="ParadoxGods/pexo-agent"
INSTALL_DIR=""
REPO_PATH=""
USE_CURRENT_CHECKOUT=0
ALLOW_REPO_INSTALL=0
INSTALL_PROFILE="auto"
SKIP_UPDATE=0
OFFLINE=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --headless-setup)
            HEADLESS_SETUP=1
            shift
            ;;
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
            echo "Unknown installer option: $1" >&2
            exit 1
            ;;
    esac
done

print_progress() {
    local percent="$1"
    local status="$2"
    printf '[%3s%%] %s\n' "$percent" "$status"
}

require_command() {
    local command_name="$1"
    local help_text="$2"
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "$help_text" >&2
        exit 1
    fi
}

is_windows_shell() {
    case "${OSTYPE:-}" in
        msys*|cygwin*|win32*) return 0 ;;
    esac
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*) return 0 ;;
    esac
    return 1
}

resolve_python_command() {
    if is_windows_shell && command -v python >/dev/null 2>&1; then
        echo "python"
        return
    fi
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
    elif command -v python >/dev/null 2>&1; then
        echo "python"
    else
        return 1
    fi
}

resolve_full_path() {
    local input_path="$1"
    local python_cmd="$2"
    "$python_cmd" - "$input_path" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
}

run_tracked() {
    local percent="$1"
    local start_message="$2"
    local heartbeat_message="$3"
    shift 3

    print_progress "$percent" "$start_message"
    "$@" &
    local pid=$!

    while kill -0 "$pid" 2>/dev/null; do
        sleep 5
        if kill -0 "$pid" 2>/dev/null; then
            print_progress "$percent" "$heartbeat_message"
        fi
    done

    wait "$pid"
}

assert_preflight() {
    local install_target="$1"
    local using_existing_checkout="$2"

    print_progress 2 "Running installer preflight checks"
    require_command git "Git is required to install Pexo. Install Git and rerun the installer."

    local python_cmd
    python_cmd=$(resolve_python_command) || {
        echo "Python 3.11 or newer is required to install Pexo." >&2
        exit 1
    }

    if ! "$python_cmd" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
        local python_version
        python_version=$("$python_cmd" - <<'PY'
import sys
print(".".join(map(str, sys.version_info[:3])))
PY
)
        echo "Python 3.11 or newer is required. Detected Python $python_version." >&2
        exit 1
    fi

    local probe_target probe_file
    if [ "$using_existing_checkout" -eq 1 ]; then
        if [ ! -d "$install_target" ]; then
            echo "The existing checkout path $install_target does not exist." >&2
            exit 1
        fi
        probe_target="$install_target"
    else
        probe_target=$(dirname "$install_target")
    fi
    probe_file="$probe_target/.pexo-install-write-test"
    : > "$probe_file"
    rm -f "$probe_file"

    if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1 && ! command -v clang >/dev/null 2>&1; then
        echo "[NOTE] Native build tools were not detected. Install them only if you later enable the optional vector-memory runtime and pip needs to build native wheels."
    fi
}

gh_auth_available() {
    command -v gh >/dev/null 2>&1 && gh auth status -h github.com >/dev/null 2>&1
}

resolve_package_source() {
    local repository="$1"
    if [[ "$repository" =~ ^git\+ ]]; then
        printf '%s\n' "$repository"
        return
    fi
    if [[ "$repository" =~ ^https?:// ]]; then
        printf 'git+%s\n' "$repository"
        return
    fi
    if [[ "$repository" =~ ^git@ ]]; then
        printf '%s\n' "$repository"
        return
    fi
    if [[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        printf 'git+https://github.com/%s.git\n' "$repository"
        return
    fi
    echo "Unsupported repository source: $repository" >&2
    exit 1
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

should_use_packaged_install() {
    if [ "$USING_EXISTING_CHECKOUT" -eq 1 ]; then
        return 1
    fi
    if [ -n "$INSTALL_DIR" ]; then
        return 1
    fi
    [ -n "$(packaged_install_tool)" ]
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

emit_install_summary_json() {
    python - "$@" <<'PY'
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

print_packaged_mcp_snippet() {
    cat <<EOF
{
  "mcpServers": {
    "pexo": {
      "command": "pexo-mcp",
      "args": []
    }
  }
}
EOF
}

profile_rank() {
    case "$1" in
        core) echo 1 ;;
        mcp) echo 2 ;;
        full) echo 3 ;;
        vector) echo 4 ;;
        *) echo 0 ;;
    esac
}

requested_profile() {
    if [ "$INSTALL_PROFILE" != "auto" ]; then
        echo "$INSTALL_PROFILE"
        return
    fi

    echo "core"
}

requirements_file_for_profile() {
    case "$1" in
        core) echo "requirements-core.txt" ;;
        mcp) echo "requirements-mcp.txt" ;;
        full) echo "requirements-full.txt" ;;
        vector) echo "requirements-vector.txt" ;;
        *)
            echo "Unsupported dependency profile: $1" >&2
            exit 1
            ;;
    esac
}

dependency_marker_path() {
    printf '%s/.pexo-deps-profile\n' "$PEXO_DIR"
}

current_profile() {
    local marker
    marker=$(dependency_marker_path)
    if [ -f "$marker" ]; then
        tr -d '[:space:]' < "$marker"
    fi
}

set_current_profile() {
    printf '%s' "$1" > "$(dependency_marker_path)"
}

gh_or_git_clone() {
    local target_dir="$1"
    if [[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] && gh_auth_available; then
        CLONE_METHOD_SUMMARY="gh repo clone $REPOSITORY $target_dir"
        run_tracked 20 "Cloning repository to $target_dir..." "Cloning repository... still working" gh repo clone "$REPOSITORY" "$target_dir"
        return
    fi

    local clone_source="$REPOSITORY"
    if [[ ! "$clone_source" =~ ^(https?|git@) ]]; then
        clone_source="https://github.com/$REPOSITORY.git"
    fi
    CLONE_METHOD_SUMMARY="git clone $clone_source $target_dir"
    run_tracked 20 "Cloning repository to $target_dir..." "Cloning repository... still working" git clone "$clone_source" "$target_dir"
}

venv_python() {
    if [ -x "venv/bin/python3" ]; then
        echo "venv/bin/python3"
    else
        echo "venv/bin/python"
    fi
}

venv_python_usable() {
    if [ -x "venv/bin/python3" ]; then
        venv/bin/python3 -c "import sys" >/dev/null 2>&1
        return $?
    fi
    if [ -x "venv/bin/python" ]; then
        venv/bin/python -c "import sys" >/dev/null 2>&1
        return $?
    fi
    return 1
}

venv_pip_usable() {
    if ! venv_python_usable; then
        return 1
    fi
    "$(venv_python)" -m pip --version >/dev/null 2>&1
}

dependency_import_smoke() {
    case "$1" in
        core) echo "import fastapi, pydantic, sqlalchemy" ;;
        mcp) echo "import fastapi, pydantic, sqlalchemy, mcp" ;;
        full) echo "import fastapi, pydantic, sqlalchemy, mcp, uvicorn, langgraph" ;;
        vector) echo "import fastapi, pydantic, sqlalchemy, mcp, uvicorn, langgraph, chromadb" ;;
        *)
            echo "Unsupported dependency profile: $1" >&2
            return 1
            ;;
    esac
}

profile_ready() {
    local profile="$1"
    if ! venv_pip_usable; then
        return 1
    fi
    "$(venv_python)" -c "$(dependency_import_smoke "$profile")" >/dev/null 2>&1
}

ensure_venv_pip() {
    if venv_pip_usable; then
        return 0
    fi
    echo "pip is missing from the virtual environment. Repairing it..." >&2
    "$(venv_python)" -m ensurepip --upgrade 1>&2
    if ! venv_pip_usable; then
        echo "Failed to repair pip in the local virtual environment." >&2
        return 1
    fi
}

git_checkout_branch_at() {
    local repo_dir="$1"
    git -C "$repo_dir" rev-parse --abbrev-ref HEAD 2>/dev/null | tr -d '[:space:]'
}

git_checkout_detached_at() {
    local repo_dir="$1"
    local branch_name
    branch_name=$(git_checkout_branch_at "$repo_dir")
    [ -z "$branch_name" ] || [ "$branch_name" = "HEAD" ]
}

install_dependency_profile() {
    local profile="$1"
    local start_message="$2"
    local heartbeat_message="$3"
    local requirements_file constraints_file python_path
    requirements_file=$(requirements_file_for_profile "$profile")
    constraints_file="constraints.txt"
    python_path="$(venv_python)"
    ensure_venv_pip

    if command -v uv >/dev/null 2>&1; then
        run_tracked 70 "$start_message" "$heartbeat_message" uv pip install --python "$python_path" -r "$requirements_file" -c "$constraints_file"
    else
        run_tracked 70 "$start_message" "$heartbeat_message" "$python_path" -m pip install --disable-pip-version-check -r "$requirements_file" -c "$constraints_file"
    fi
    if ! profile_ready "$profile"; then
        echo "The '$profile' runtime marker could not be verified after dependency installation." >&2
        exit 1
    fi
    set_current_profile "$profile"
}

print_mcp_snippet() {
    local launcher_path="$1"
    cat <<EOF
{
  "mcpServers": {
    "pexo": {
      "command": "bash",
      "args": ["-c", "\"$launcher_path\" --mcp"]
    }
  }
}
EOF
}

if [ "$USE_CURRENT_CHECKOUT" -eq 1 ] && [ -n "$REPO_PATH" ]; then
    echo "Use either --use-current-checkout or --repo-path, not both." >&2
    exit 1
fi

PYTHON_FOR_PATHS=$(resolve_python_command) || {
    echo "Python 3.11 or newer is required to install Pexo." >&2
    exit 1
}
DEFAULT_PEXO_DIR=$(resolve_full_path "$DEFAULT_PEXO_DIR" "$PYTHON_FOR_PATHS")

if [ "$USE_CURRENT_CHECKOUT" -eq 1 ]; then
    PEXO_DIR="$SCRIPT_ROOT"
elif [ -n "$REPO_PATH" ]; then
    PEXO_DIR=$(resolve_full_path "$REPO_PATH" "$PYTHON_FOR_PATHS")
elif [ -n "$INSTALL_DIR" ]; then
    PEXO_DIR=$(resolve_full_path "$INSTALL_DIR" "$PYTHON_FOR_PATHS")
fi

USING_EXISTING_CHECKOUT=0
if [ "$USE_CURRENT_CHECKOUT" -eq 1 ] || [ -n "$REPO_PATH" ]; then
    USING_EXISTING_CHECKOUT=1
fi

REQUESTED_PROFILE=$(requested_profile)
CLONE_METHOD_SUMMARY="pending"
PROTECTED_CHECKOUT_PATH=""

if [ "$ALLOW_REPO_INSTALL" -ne 1 ] && [ -e "$PEXO_DIR/.git" ] && [ "$PEXO_DIR" != "$DEFAULT_PEXO_DIR" ]; then
    PROTECTED_CHECKOUT_PATH="$PEXO_DIR"
    PEXO_DIR="$DEFAULT_PEXO_DIR"
    USING_EXISTING_CHECKOUT=0
fi

echo "=================================================="
echo "Installing Pexo (The OpenClaw Killer) ..."
echo "=================================================="

assert_preflight "$PEXO_DIR" "$USING_EXISTING_CHECKOUT"

if should_use_packaged_install; then
    PACKAGE_SOURCE=$(resolve_package_source "$REPOSITORY")
    PACKAGED_TOOL=$(packaged_install_tool)
    PACKAGED_MODE_LABEL=""
    UNINSTALL_COMMAND=""
    if [ "$PACKAGED_TOOL" = "uv" ]; then
        run_tracked 20 "Installing packaged Pexo tool from GitHub..." "Installing packaged Pexo tool... still working" uv tool install --reinstall "$PACKAGE_SOURCE"
        uv tool update-shell >/dev/null 2>&1 || true

        UV_BIN_DIR=""
        if uv tool dir --bin >/dev/null 2>&1; then
            UV_BIN_DIR=$(uv tool dir --bin)
        fi
        append_session_path "$UV_BIN_DIR"
        PACKAGED_MODE_LABEL="packaged GitHub tool via uv"
        UNINSTALL_COMMAND="uv tool uninstall pexo-agent"
    else
        run_tracked 20 "Installing packaged Pexo tool from GitHub..." "Installing packaged Pexo tool... still working" pipx install --force "$PACKAGE_SOURCE"
        pipx ensurepath >/dev/null 2>&1 || true
        append_session_path "$(pipx_bin_dir)"
        PACKAGED_MODE_LABEL="packaged GitHub tool via pipx"
        UNINSTALL_COMMAND="pipx uninstall pexo-agent"
    fi

    if ! command -v pexo >/dev/null 2>&1; then
        echo "Packaged install completed, but the 'pexo' command is not visible in this shell. Reopen the terminal or invoke the tool from the packaged tool bin directory directly." >&2
        exit 1
    fi

    FINAL_PROFILE="mcp"
    if [ "$REQUESTED_PROFILE" = "full" ] || [ "$REQUESTED_PROFILE" = "vector" ]; then
        run_tracked 85 "Promoting packaged install to the $REQUESTED_PROFILE runtime..." "Promoting packaged install... still working" pexo promote "$REQUESTED_PROFILE"
        FINAL_PROFILE="$REQUESTED_PROFILE"
    fi

    if [ "$HEADLESS_SETUP" -eq 1 ]; then
        HEADLESS_ARGS=(headless-setup --preset "$PRESET" --name "$PROFILE_NAME")
        if [ -n "$BACKUP_PATH" ]; then
            HEADLESS_ARGS+=(--backup-path "$BACKUP_PATH")
        fi
        run_tracked 95 "Applying headless profile setup..." "Applying headless profile setup... still working" pexo "${HEADLESS_ARGS[@]}"
    fi

    STATE_ROOT="$HOME/.pexo"
    DATABASE_PATH="$STATE_ROOT/pexo.db"
    VECTOR_STORE_PATH="$STATE_ROOT/chroma_db"
    ARTIFACT_PATH="$STATE_ROOT/artifacts"
    TOOLS_PATH="$STATE_ROOT/dynamic_tools"
    if [ "$HEADLESS_SETUP" -eq 1 ]; then
        PROFILE_SUMMARY="$PROFILE_NAME"
        if [ -n "$BACKUP_PATH" ]; then
            BACKUP_SUMMARY="$BACKUP_PATH"
        else
            BACKUP_SUMMARY="not set"
        fi
    else
        PROFILE_SUMMARY="not initialized"
        BACKUP_SUMMARY="not configured during install"
    fi

    print_progress 100 "Installation complete"
    echo "=================================================="
    echo "Pexo installed successfully!"
    echo "Install mode: $PACKAGED_MODE_LABEL"
    echo "Package source: $PACKAGE_SOURCE"
    if [ -n "$PROTECTED_CHECKOUT_PATH" ]; then
        echo "Protected checkout left untouched: $PROTECTED_CHECKOUT_PATH"
    fi
    echo "State directory: $STATE_ROOT"
    echo "Dependency profile ready now: $FINAL_PROFILE"
    echo "Profile initialized: $PROFILE_SUMMARY"
    echo "Backup path: $BACKUP_SUMMARY"
    echo "Local database path: $DATABASE_PATH"
    echo "Local vector store path: $VECTOR_STORE_PATH"
    echo "Local artifacts path: $ARTIFACT_PATH"
    echo "Local dynamic tools path: $TOOLS_PATH"
    echo "Works now in this shell via bare command:"
    echo "  pexo --version"
    echo "Ready-to-paste MCP config:"
    print_packaged_mcp_snippet
    echo "To connect supported AI clients automatically:"
    echo "  pexo connect all --scope user"
    echo "To uninstall the packaged tool later:"
    echo "  $UNINSTALL_COMMAND"
    echo "To remove local state as well:"
    echo "  rm -rf \"$STATE_ROOT\""
    if [ "$HEADLESS_SETUP" -eq 1 ]; then
        echo "Headless profile setup completed during install."
        echo "Run 'pexo' later when the user wants the local dashboard for memory, agents, and configuration."
    else
        echo "Preferred terminal-first setup path:"
        echo "  pexo headless-setup --preset $PRESET"
        echo "Run 'pexo' later only when the user wants the local dashboard at http://127.0.0.1:9999."
    fi
    emit_install_summary_json \
      "status=ok" \
      "install_mode=packaged" \
      "packaged_tool=$PACKAGED_TOOL" \
      "package_source=$PACKAGE_SOURCE" \
      "install_directory=__NULL__" \
      "state_directory=$STATE_ROOT" \
      "active_profile=$FINAL_PROFILE" \
      "profile_initialized=$PROFILE_SUMMARY" \
      "backup_path=$BACKUP_SUMMARY" \
      "launcher_command=pexo" \
      "mcp_command=pexo-mcp" \
      "uninstall_command=$UNINSTALL_COMMAND" \
      "next=[pexo connect all --scope user||pexo doctor||pexo]"
    echo "=================================================="
    exit 0
fi

print_progress 5 "Validating install target at $PEXO_DIR"
if [ -n "$PROTECTED_CHECKOUT_PATH" ]; then
    echo "[SAFE] Existing checkout protection is enabled. Leaving $PROTECTED_CHECKOUT_PATH untouched and installing to $PEXO_DIR instead. Pass --allow-repo-install only when you intentionally want a repo-local install."
fi
if [ "$USING_EXISTING_CHECKOUT" -eq 1 ]; then
    if [ ! -e "$PEXO_DIR/.git" ]; then
        echo "The checkout at $PEXO_DIR is missing a .git directory. Use --install-dir to clone a new copy or point --repo-path at an existing checkout." >&2
        exit 1
    fi

    if [ "$SKIP_UPDATE" -eq 1 ] || [ "$OFFLINE" -eq 1 ]; then
        print_progress 20 "Using existing checkout. Skipping repository update."
        CLONE_METHOD_SUMMARY="existing checkout ($PEXO_DIR), update skipped"
    elif git_checkout_detached_at "$PEXO_DIR"; then
        print_progress 20 "Existing checkout is pinned to a detached git HEAD. Skipping repository update."
        CLONE_METHOD_SUMMARY="existing checkout ($PEXO_DIR), update skipped (detached HEAD)"
    else
        run_tracked 20 "Using existing checkout. Updating repository in place..." "Updating repository... still working" git -C "$PEXO_DIR" pull --ff-only
        CLONE_METHOD_SUMMARY="existing checkout ($PEXO_DIR), updated via git pull"
    fi
elif [ -e "$PEXO_DIR/.git" ]; then
    if [ "$SKIP_UPDATE" -eq 1 ] || [ "$OFFLINE" -eq 1 ]; then
        print_progress 20 "Existing installation found. Skipping repository update."
        CLONE_METHOD_SUMMARY="existing installation at $PEXO_DIR, update skipped"
    elif git_checkout_detached_at "$PEXO_DIR"; then
        print_progress 20 "Existing installation is pinned to a detached git HEAD. Skipping repository update."
        CLONE_METHOD_SUMMARY="existing installation at $PEXO_DIR, update skipped (detached HEAD)"
    else
        run_tracked 20 "Existing installation found. Updating repository in place..." "Updating repository... still working" git -C "$PEXO_DIR" pull --ff-only
        CLONE_METHOD_SUMMARY="existing installation at $PEXO_DIR, updated via git pull"
    fi
elif [ -e "$PEXO_DIR" ]; then
    echo "The directory $PEXO_DIR already exists but is not a Pexo git checkout. Move or remove it, or use --repo-path to target an existing checkout." >&2
    exit 1
else
    gh_or_git_clone "$PEXO_DIR"
fi

cd "$PEXO_DIR"

print_progress 40 "Preparing isolated Python environment"
PYTHON_CMD=$(resolve_python_command)
CREATED_VENV=0
if ! venv_python_usable; then
    if [ -d "venv" ]; then
        print_progress 43 "Existing virtual environment is unusable. Recreating it..."
        rm -rf venv
    fi
    CREATED_VENV=1
    run_tracked 45 "Creating Python virtual environment..." "Creating Python virtual environment... still working" "$PYTHON_CMD" -m venv venv
fi
ensure_venv_pip

CURRENT_PROFILE=$(current_profile)
if [ -n "$CURRENT_PROFILE" ] && [ "$(profile_rank "$CURRENT_PROFILE")" -eq 0 ]; then
    print_progress 68 "Dependency marker '$CURRENT_PROFILE' is invalid. Reinstalling runtime dependencies."
    rm -f "$(dependency_marker_path)"
    CURRENT_PROFILE=""
elif [ -n "$CURRENT_PROFILE" ] && ! profile_ready "$CURRENT_PROFILE"; then
    print_progress 68 "Dependency marker '$CURRENT_PROFILE' is stale. Reinstalling runtime dependencies."
    rm -f "$(dependency_marker_path)"
    CURRENT_PROFILE=""
fi
if [ "$(profile_rank "$CURRENT_PROFILE")" -lt "$(profile_rank "$REQUESTED_PROFILE")" ]; then
    if [ "$CREATED_VENV" -eq 1 ] || [ -z "$CURRENT_PROFILE" ]; then
        DEPENDENCY_MESSAGE="Installing Python dependencies ($REQUESTED_PROFILE runtime)..."
    else
        DEPENDENCY_MESSAGE="Promoting Python runtime to the $REQUESTED_PROFILE profile..."
    fi
    install_dependency_profile "$REQUESTED_PROFILE" "$DEPENDENCY_MESSAGE" "$DEPENDENCY_MESSAGE still working"
    FINAL_PROFILE="$REQUESTED_PROFILE"
else
    FINAL_PROFILE="$CURRENT_PROFILE"
    print_progress 70 "Python dependency profile '$FINAL_PROFILE' already satisfies the requested runtime."
fi

print_progress 90 "Adding Pexo to your shell PATH"
export PATH="$PATH:$PEXO_DIR"
touch "$HOME/.bashrc" "$HOME/.zshrc"
grep -Fqx "export PATH=\"\$PATH:$PEXO_DIR\"" "$HOME/.bashrc" || echo "export PATH=\"\$PATH:$PEXO_DIR\"" >> "$HOME/.bashrc"
grep -Fqx "export PATH=\"\$PATH:$PEXO_DIR\"" "$HOME/.zshrc" || echo "export PATH=\"\$PATH:$PEXO_DIR\"" >> "$HOME/.zshrc"

if command -v pexo >/dev/null 2>&1; then
    echo "[ 90%] Same-shell PATH activation verified."
else
    echo "[ 90%] Same-shell PATH activation could not be verified. Use \"$PEXO_DIR/pexo\" directly in this shell if needed."
fi

if [ "$HEADLESS_SETUP" -eq 1 ]; then
    HEADLESS_ARGS=(-m app.cli headless-setup --preset "$PRESET" --name "$PROFILE_NAME")
    if [ -n "$BACKUP_PATH" ]; then
        HEADLESS_ARGS+=(--backup-path "$BACKUP_PATH")
    fi
    run_tracked 95 "Applying headless profile setup..." "Applying headless profile setup... still working" "$(venv_python)" "${HEADLESS_ARGS[@]}"
fi

run_tracked 97 "Priming local runtime..." "Priming local runtime... still working" "$PEXO_DIR/pexo" warmup --quiet

DATABASE_PATH="$PEXO_DIR/pexo.db"
VECTOR_STORE_PATH="$PEXO_DIR/chroma_db"
if [ "$HEADLESS_SETUP" -eq 1 ]; then
    PROFILE_SUMMARY="$PROFILE_NAME"
    if [ -n "$BACKUP_PATH" ]; then
        BACKUP_SUMMARY="$BACKUP_PATH"
    else
        BACKUP_SUMMARY="not set"
    fi
else
    PROFILE_SUMMARY="not initialized"
    BACKUP_SUMMARY="not configured during install"
fi

print_progress 100 "Installation complete"
echo "=================================================="
echo "Pexo installed successfully!"
if [ "$SKIP_UPDATE" -eq 1 ] || [ "$OFFLINE" -eq 1 ]; then
    echo "Repository update was skipped for this install."
fi
echo "Clone method: $CLONE_METHOD_SUMMARY"
echo "Install directory: $PEXO_DIR"
if [ -n "$PROTECTED_CHECKOUT_PATH" ]; then
    echo "Protected checkout left untouched: $PROTECTED_CHECKOUT_PATH"
fi
echo "Dependency profile ready now: $FINAL_PROFILE"
echo "Profile initialized: $PROFILE_SUMMARY"
echo "Backup path: $BACKUP_SUMMARY"
echo "Local database path: $DATABASE_PATH"
echo "Local vector store path: $VECTOR_STORE_PATH"
echo "Works now in this shell via absolute path:"
echo "  \"$PEXO_DIR/pexo\" --version"
echo "Works after opening a new shell via bare command:"
echo "  pexo --version"
if [ "$HEADLESS_SETUP" -eq 1 ]; then
    echo "Headless profile setup completed during install."
    echo "Run 'pexo' later when the user wants the local dashboard for memory, agents, and configuration."
else
    echo "Preferred same-shell setup path:"
    echo "  \"$PEXO_DIR/pexo\" --headless-setup --preset $PRESET"
    echo "After reopening a shell, the same setup command also works as:"
    echo "  pexo --headless-setup --preset $PRESET"
    echo "Run 'pexo' later only when the user wants the local dashboard at http://127.0.0.1:9999."
fi
echo "If you want the full browser UI and LangGraph runtime installed ahead of first launch:"
echo "  \"$PEXO_DIR/pexo\" --promote full"
echo "If you want native Chroma vector embeddings installed as well:"
echo "  \"$PEXO_DIR/pexo\" --promote vector"
echo "Ready-to-paste MCP config:"
print_mcp_snippet "$PEXO_DIR/pexo"
echo "To connect supported AI clients automatically:"
echo "  \"$PEXO_DIR/pexo\" connect all --scope user"
emit_install_summary_json \
  "status=ok" \
  "install_mode=checkout" \
  "packaged_tool=__NULL__" \
  "package_source=__NULL__" \
  "install_directory=$PEXO_DIR" \
  "state_directory=$PEXO_DIR" \
  "active_profile=$FINAL_PROFILE" \
  "profile_initialized=$PROFILE_SUMMARY" \
  "backup_path=$BACKUP_SUMMARY" \
  "launcher_command=$PEXO_DIR/pexo" \
  "mcp_command=$PEXO_DIR/pexo --mcp" \
  "uninstall_command=pexo uninstall" \
  "next=[\"$PEXO_DIR/pexo\" connect all --scope user||\"$PEXO_DIR/pexo\" --version||\"$PEXO_DIR/pexo\" doctor]"
echo "=================================================="
