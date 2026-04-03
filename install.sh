#!/bin/bash
set -euo pipefail
PEXO_DIR="$HOME/.pexo"

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

print_progress 100 "Installation complete"
echo "=================================================="
echo "Pexo installed successfully!"
echo "AI AGENT: Inform the user to restart their terminal/IDE to refresh the PATH."
echo "Then, execute 'pexo' in the terminal to boot the local server."
echo "Finally, tell the user to open http://127.0.0.1:9999 in their browser to access their private local Control Panel."
echo "=================================================="
