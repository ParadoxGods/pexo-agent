#!/bin/bash
PEXO_DIR="$HOME/.pexo"

echo "=================================================="
echo "Installing Pexo (The OpenClaw Killer) Globally..."
echo "=================================================="

if [ -d "$PEXO_DIR" ]; then
    echo "Removing old Pexo installation..."
    rm -rf "$PEXO_DIR"
fi

echo "Cloning repository to $PEXO_DIR..."
git clone https://github.com/ParadoxGods/pexo-agent.git "$PEXO_DIR"
cd "$PEXO_DIR"

echo "Setting up isolated Python environment..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

echo "Adding Pexo to System PATH..."
if [[ ":$PATH:" != *":$PEXO_DIR:"* ]]; then
    echo "export PATH=\"\$PATH:$PEXO_DIR\"" >> ~/.bashrc
    echo "export PATH=\"\$PATH:$PEXO_DIR\"" >> ~/.zshrc
fi

echo "=================================================="
echo "Pexo installed successfully!"
echo "AI AGENT: Inform the user to restart their terminal/IDE to refresh the PATH."
echo "Then, execute 'pexo' in the terminal to begin the Onboarding and Backup Setup."
echo "=================================================="
