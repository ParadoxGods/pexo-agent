#!/bin/bash
set -euo pipefail
PEXO_DIR="$HOME/.pexo"

echo "=================================================="
echo "Installing Pexo (The OpenClaw Killer) Globally..."
echo "=================================================="

if [ -d "$PEXO_DIR/.git" ]; then
    echo "Existing Pexo installation detected. Updating repository in place..."
    git -C "$PEXO_DIR" pull --ff-only
elif [ -e "$PEXO_DIR" ]; then
    echo "The directory $PEXO_DIR already exists but is not a Pexo git checkout. Move or remove it and rerun the installer." >&2
    exit 1
else
    echo "Cloning repository to $PEXO_DIR..."
    git clone https://github.com/ParadoxGods/pexo-agent.git "$PEXO_DIR"
fi

cd "$PEXO_DIR"

echo "Setting up isolated Python environment..."
if [ ! -x "venv/bin/python3" ] && [ ! -x "venv/bin/python" ]; then
    python3 -m venv venv
fi
if [ -x "venv/bin/python3" ]; then
    VENV_PYTHON="venv/bin/python3"
else
    VENV_PYTHON="venv/bin/python"
fi
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r requirements.txt

echo "Adding Pexo to System PATH..."
if [[ ":$PATH:" != *":$PEXO_DIR:"* ]]; then
    touch ~/.bashrc ~/.zshrc
    grep -Fqx "export PATH=\"\$PATH:$PEXO_DIR\"" ~/.bashrc || echo "export PATH=\"\$PATH:$PEXO_DIR\"" >> ~/.bashrc
    grep -Fqx "export PATH=\"\$PATH:$PEXO_DIR\"" ~/.zshrc || echo "export PATH=\"\$PATH:$PEXO_DIR\"" >> ~/.zshrc
fi

echo "=================================================="
echo "Pexo installed successfully!"
echo "AI AGENT: Inform the user to restart their terminal/IDE to refresh the PATH."
echo "Then, execute 'pexo' in the terminal to boot the local server."
echo "Finally, tell the user to open http://127.0.0.1:9999 in their browser to access their private local Control Panel."
echo "=================================================="
