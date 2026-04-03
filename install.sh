#!/bin/bash
set -euo pipefail
PEXO_DIR="$HOME/.pexo"
HEADLESS_SETUP=0
PRESET="efficient_operator"
PROFILE_NAME="default_user"
BACKUP_PATH=""

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

echo "=================================================="
echo "Installing Pexo (The OpenClaw Killer) Globally..."
echo "=================================================="

print_progress 5 "Validating install target at $PEXO_DIR"
if [ -d "$PEXO_DIR/.git" ]; then
    run_tracked 20 "Existing installation found. Updating repository in place..." "Updating repository... still working" git -C "$PEXO_DIR" pull --ff-only
elif [ -e "$PEXO_DIR" ]; then
    echo "The directory $PEXO_DIR already exists but is not a Pexo git checkout. Move or remove it and rerun the installer." >&2
    exit 1
else
    run_tracked 20 "Cloning repository to $PEXO_DIR..." "Cloning repository... still working" git clone https://github.com/ParadoxGods/pexo-agent.git "$PEXO_DIR"
fi

cd "$PEXO_DIR"

print_progress 40 "Preparing isolated Python environment"
if [ ! -x "venv/bin/python3" ] && [ ! -x "venv/bin/python" ]; then
    run_tracked 45 "Creating Python virtual environment..." "Creating Python virtual environment... still working" python3 -m venv venv
fi
if [ -x "venv/bin/python3" ]; then
    VENV_PYTHON="venv/bin/python3"
else
    VENV_PYTHON="venv/bin/python"
fi
run_tracked 60 "Upgrading pip..." "Upgrading pip... still working" "$VENV_PYTHON" -m pip install --upgrade pip
run_tracked 75 "Installing Python dependencies (this can take a while)..." "Installing Python dependencies... still working" "$VENV_PYTHON" -m pip install -r requirements.txt

print_progress 90 "Adding Pexo to your shell PATH"
if [[ ":$PATH:" != *":$PEXO_DIR:"* ]]; then
    touch ~/.bashrc ~/.zshrc
    grep -Fqx "export PATH=\"\$PATH:$PEXO_DIR\"" ~/.bashrc || echo "export PATH=\"\$PATH:$PEXO_DIR\"" >> ~/.bashrc
    grep -Fqx "export PATH=\"\$PATH:$PEXO_DIR\"" ~/.zshrc || echo "export PATH=\"\$PATH:$PEXO_DIR\"" >> ~/.zshrc
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
echo "AI AGENT: Restart the terminal only if the user needs the refreshed PATH in a new shell."
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
