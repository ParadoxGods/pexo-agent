@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"
set "UPDATE_STAMP=%CD%\.pexo-update-check"
set "NO_BROWSER=0"

:parseflags
if "%~1"=="--no-browser" (
    set "NO_BROWSER=1"
    shift
    goto parseflags
)
if "%~1"=="--offline" (
    set "PEXO_SKIP_UPDATE=1"
    shift
    goto parseflags
)
if "%~1"=="--skip-update" (
    set "PEXO_SKIP_UPDATE=1"
    shift
    goto parseflags
)

if "%~1"=="--version" goto version
if "%~1"=="--help" goto help
if "%~1"=="--mcp" goto mcp
if "%~1"=="--update" goto update
if /I "%~1"=="update" goto update
if "%~1"=="--list-presets" goto listpresets
if /I "%~1"=="list-presets" goto listpresets
if "%~1"=="--headless-setup" goto headlesssetup
if /I "%~1"=="headless-setup" goto headlesssetup
if "%~1"=="--uninstall" goto uninstall
if /I "%~1"=="uninstall" goto uninstall
call :maybeupdate

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
        set "PATH=%PATH%;%CD%"
        echo Added %CD% to PATH. Please restart your terminal after this session for it to take effect globally.
    )
)

IF NOT EXIST "venv\Scripts\python.exe" (
    echo Virtual environment not found. Creating one...
    python -m venv venv
    echo Installing dependencies...
    venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
)

echo Starting Pexo API...
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 9999 --workers 1
pause
exit /b %ERRORLEVEL%

:mcp
IF NOT EXIST "venv\Scripts\python.exe" (
    python -m venv venv 1>&2
    venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt 1>&2
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
echo   pexo --list-presets ^| pexo list-presets
echo                  Lists available profile presets for terminal-first setup
echo   pexo --headless-setup ^| pexo headless-setup [--preset PRESET] [--name NAME] [--backup-path PATH]
echo                  Initializes the local profile without opening the web UI
echo   pexo --update ^| pexo update
echo                  Pulls the latest repository changes immediately
echo   pexo --no-browser
echo                  Starts the API without opening the dashboard automatically
echo   pexo --offline ^| pexo --skip-update
echo                  Starts Pexo without attempting an update check
echo   pexo --mcp     Starts Pexo as a native MCP server (stdio)
echo   pexo --uninstall ^| pexo uninstall
echo                  Removes the local Pexo installation and saved state
echo   pexo --version Displays the current version
echo   pexo --help    Displays this help menu
exit /b 0

:update
echo Checking for updates...
git pull --ff-only
if errorlevel 1 exit /b %ERRORLEVEL%
powershell -NoProfile -Command "Set-Content -LiteralPath '%UPDATE_STAMP%' -Value ([DateTime]::UtcNow.Ticks) -Encoding Ascii"
echo Pexo is up to date.
exit /b 0

:listpresets
IF NOT EXIST "venv\Scripts\python.exe" (
    python -m venv venv 1>&2
    venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt 1>&2
)
venv\Scripts\python.exe -m app.cli list-presets %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:headlesssetup
IF NOT EXIST "venv\Scripts\python.exe" (
    python -m venv venv 1>&2
    venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt 1>&2
)
venv\Scripts\python.exe -m app.cli headless-setup %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:maybeupdate
if /I "%PEXO_SKIP_UPDATE%"=="1" exit /b 0
powershell -NoProfile -Command "$stamp = '%UPDATE_STAMP%'; if (Test-Path $stamp) { $last = [Int64](Get-Content -LiteralPath $stamp -Raw); if (([DateTime]::UtcNow.Ticks - $last) -lt [TimeSpan]::FromHours(12).Ticks) { exit 10 } }; exit 0"
if "%ERRORLEVEL%"=="10" exit /b 0
echo Checking for updates...
git pull --ff-only --quiet >nul 2>nul
if errorlevel 1 (
    echo Update check failed. Continuing with the local version.
    echo Run 'pexo update' for full git or auth output. If this repo is private, ensure git or gh authentication is configured.
) else (
    powershell -NoProfile -Command "Set-Content -LiteralPath '%UPDATE_STAMP%' -Value ([DateTime]::UtcNow.Ticks) -Encoding Ascii"
)
exit /b 0

:uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
exit /b %ERRORLEVEL%
