@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo   ____  _____ __  __ ___  
echo  ^|  _ ^\^| ____^|^\ ^\/ // _ ^\ 
echo  ^| ^|_) ^|  _^|   ^\  /^| ^| ^| ^|
echo  ^|  __/^| ^|___  /  ^\^| ^|_^| ^|
echo  ^|_^|   ^|_____^|/_/^\_\\___/ 
echo.
echo ==================================================
echo Starting Pexo (Primary EXecution Officer)
echo ==================================================

:: Check if current directory is in PATH
echo !PATH! | findstr /i /c:"%CD%" >nul
if !errorlevel! neq 0 (
    set /p add_path="Pexo is not in your system PATH. Would you like to add it now? (Y/N): "
    if /i "!add_path!"=="Y" (
        setx PATH "%PATH%;%CD%"
        echo Added %CD% to PATH. Please restart your terminal after this session for it to take effect globally.
    )
)

IF NOT EXIST "venv" (
    echo Virtual environment not found. Creating one...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo Installing dependencies...
    pip install -r requirements.txt
) ELSE (
    call venv\Scripts\activate.bat
)

if "%~1"=="--mcp" (
    :: Run the FastMCP server over stdio (Silent stdout, only MCP protocol output allowed)
    python -c "from app.database import init_db; init_db(); from app.mcp_server import start_mcp_server; start_mcp_server()"
) else (
    echo Starting Pexo API...
    python -m uvicorn app.main:app --host 127.0.0.1 --port 9999 --workers 1
    pause
)
