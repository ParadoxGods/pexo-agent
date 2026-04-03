#!/bin/bash
set -euo pipefail

PEXO_DIR="$HOME/.pexo"
HEADLESS_SETUP=0
PRESET="efficient_operator"
PROFILE_NAME="default_user"
BACKUP_PATH=""
REPOSITORY="ParadoxGods/pexo-agent"
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

resolve_python_command() {
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
    elif command -v python >/dev/null 2>&1; then
        echo "python"
    else
        return 1
    fi
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
    print_progress 2 "Running installer preflight checks"
    require_command git "Git is required to install Pexo. Install Git and rerun the installer."

    local python_cmd
    python_cmd=$(resolve_python_command) || {
        echo "Python 3.11 or newer is required to install Pexo." >&2
        exit 1
    }

    "$python_cmd" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    if [ $? -ne 0 ]; then
        local python_version
        python_version=$("$python_cmd" - <<'PY'
import sys
print(".".join(map(str, sys.version_info[:3])))
PY
)
        echo "Python 3.11 or newer is required. Detected Python $python_version." >&2
        exit 1
    fi

    local install_parent probe_file
    install_parent=$(dirname "$PEXO_DIR")
    probe_file="$install_parent/.pexo-install-write-test"
    : > "$probe_file"
    rm -f "$probe_file"
}

gh_auth_available() {
    command -v gh >/dev/null 2>&1 && gh auth status -h github.com >/dev/null 2>&1
}

clone_repository() {
    local target_dir="$1"
    if [[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] && gh_auth_available; then
        run_tracked 20 "Cloning repository to $target_dir..." "Cloning repository... still working" gh repo clone "$REPOSITORY" "$target_dir"
        return
    fi

    local clone_source="$REPOSITORY"
    if [[ ! "$clone_source" =~ ^(https?|git@) ]]; then
        clone_source="https://github.com/$REPOSITORY.git"
    fi
    run_tracked 20 "Cloning repository to $target_dir..." "Cloning repository... still working" git clone "$clone_source" "$target_dir"
}

echo "=================================================="
echo "Installing Pexo (The OpenClaw Killer) Globally..."
echo "=================================================="

assert_preflight

print_progress 5 "Validating install target at $PEXO_DIR"
if [ -d "$PEXO_DIR/.git" ]; then
    if [ "$SKIP_UPDATE" -eq 1 ] || [ "$OFFLINE" -eq 1 ]; then
        print_progress 20 "Existing installation found. Skipping repository update."
    else
        run_tracked 20 "Existing installation found. Updating repository in place..." "Updating repository... still working" git -C "$PEXO_DIR" pull --ff-only
    fi
elif [ -e "$PEXO_DIR" ]; then
    echo "The directory $PEXO_DIR already exists but is not a Pexo git checkout. Move or remove it and rerun the installer." >&2
    exit 1
else
    clone_repository "$PEXO_DIR"
fi

cd "$PEXO_DIR"

print_progress 40 "Preparing isolated Python environment"
PYTHON_CMD=$(resolve_python_command)
CREATED_VENV=0
if [ ! -x "venv/bin/python3" ] && [ ! -x "venv/bin/python" ]; then
    CREATED_VENV=1
    run_tracked 45 "Creating Python virtual environment..." "Creating Python virtual environment... still working" "$PYTHON_CMD" -m venv venv
fi
if [ -x "venv/bin/python3" ]; then
    VENV_PYTHON="venv/bin/python3"
else
    VENV_PYTHON="venv/bin/python"
fi

if [ "$CREATED_VENV" -eq 1 ]; then
    DEPENDENCY_MESSAGE="Installing Python dependencies..."
else
    DEPENDENCY_MESSAGE="Syncing Python dependencies..."
fi
run_tracked 70 "$DEPENDENCY_MESSAGE" "$DEPENDENCY_MESSAGE still working" "$VENV_PYTHON" -m pip install --disable-pip-version-check -r requirements.txt

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
    run_tracked 95 "Applying headless profile setup..." "Applying headless profile setup... still working" "$VENV_PYTHON" "${HEADLESS_ARGS[@]}"
fi

print_progress 100 "Installation complete"
echo "=================================================="
echo "Pexo installed successfully!"
if [ "$SKIP_UPDATE" -eq 1 ] || [ "$OFFLINE" -eq 1 ]; then
    echo "Repository update was skipped for this install."
fi
echo "AI AGENT: Restart the terminal only if the user needs the refreshed PATH in a brand new shell."
if [ "$HEADLESS_SETUP" -eq 1 ]; then
    echo "Headless profile setup completed during install."
    echo "Run 'pexo' later when the user wants the local dashboard for memory, agents, and configuration."
else
    echo "Preferred same-shell setup path:"
    echo "  \"$PEXO_DIR/pexo\" --headless-setup --preset $PRESET"
    echo "After the terminal is restarted, the same command also works as:"
    echo "  pexo --headless-setup --preset $PRESET"
    echo "Run 'pexo' later only when the user wants the local dashboard at http://127.0.0.1:9999."
fi
echo "=================================================="
