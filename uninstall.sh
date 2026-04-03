#!/bin/bash
PEXO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=================================================="
echo "Uninstalling Pexo (Primary EXecution Operator)..."
echo "=================================================="

# 1. Terminate running processes
echo "Terminating Pexo processes..."
pkill -f "app.main:app" 2>/dev/null

# 2. Remove from PATH
echo "Removing Pexo from shell profiles..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "\|$PEXO_DIR|d" ~/.bashrc ~/.zshrc 2>/dev/null
else
    sed -i "\|$PEXO_DIR|d" ~/.bashrc ~/.zshrc 2>/dev/null
fi

# 3. Delete files
if [ -d "$PEXO_DIR" ]; then
    echo "Deleting Pexo files at $PEXO_DIR..."
    rm -rf "$PEXO_DIR"
fi

echo "=================================================="
echo "Pexo has been successfully uninstalled."
echo "=================================================="
