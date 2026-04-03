#!/bin/bash
echo "=================================================="
echo "Starting Pexo (Primary EXecution Officer)"
echo "=================================================="

if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Creating one..."
    python3 -m venv venv
    source venv/bin/activate
    echo "Installing dependencies..."
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo "Starting Pexo API..."
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
