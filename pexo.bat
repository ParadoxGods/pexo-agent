@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

if "%~1"=="--version" goto version
if "%~1"=="--help" goto help
if "%~1"=="--mcp" goto mcp
if "%~1"=="--uninstall" goto uninstall
if /I "%~1"=="uninstall" goto uninstall

:: Auto-Update: Pull latest changes from GitHub silently
echo Checking for updates...
git pull --quiet

echo   ____  _____ __  __ ___  
echo  ^|  _ ^\^| ____^|^\ ^\/ // _ ^\ 
echo  ^| ^|_) ^|  _^|   ^\  /^| ^| ^| ^|
echo  ^|  __/^| ^|___  /  ^\^| ^|_^| ^|
echo  ^|_^|   ^|_____^|/_/^\_\\___/ 
echo.
echo ==================================================
echo Starting Pexo (Primary EXecution Operator)
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

IF NOT EXIST "venv\Scripts\python.exe" (
    echo Virtual environment not found. Creating one...
    python -m venv venv
    echo Installing dependencies...
    venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo Starting Pexo API...
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 9999 --workers 1
pause
exit /b %ERRORLEVEL%

:mcp
IF NOT EXIST "venv\Scripts\python.exe" (
    python -m venv venv 1>&2
    venv\Scripts\python.exe -m pip install -r requirements.txt 1>&2
)
venv\Scripts\python.exe -c "from app.mcp_server import start_mcp_server; start_mcp_server()"
exit /b %ERRORLEVEL%

:version
echo Pexo v1.0.0-stable
exit /b 0

:help
echo Pexo: Primary EXecution Operator
echo.
echo Usage:
echo   pexo           Starts the Pexo API and Control Panel
echo   pexo --mcp     Starts Pexo as a native MCP server (stdio)
echo   pexo --uninstall ^| pexo uninstall
echo                  Removes the local Pexo installation and saved state
echo   pexo --version Displays the current version
echo   pexo --help    Displays this help menu
exit /b 0

:uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
exit /b %ERRORLEVEL%
