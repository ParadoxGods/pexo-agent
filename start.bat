@echo off
echo ==================================================
echo Starting Pexo (Primary EXecution Officer)
echo ==================================================

IF NOT EXIST "venv" (
    echo Virtual environment not found. Creating one...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo Installing dependencies...
    pip install -r requirements.txt
) ELSE (
    call venv\Scripts\activate.bat
)

echo Starting Pexo API...
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
pause
