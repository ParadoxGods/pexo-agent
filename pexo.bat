@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"
set "UPDATE_STAMP=%CD%\.pexo-update-check"
set "DEPENDENCY_MARKER=%CD%\.pexo-deps-profile"
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
if "%~1"=="--promote" goto promote
if /I "%~1"=="promote" goto promote
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

echo !PATH! | findstr /i /c:"%CD%" >nul
if !errorlevel! neq 0 (
    set /p add_path="Pexo is not in your system PATH. Would you like to add it now? (Y/N): "
    if /i "!add_path!"=="Y" (
        setx PATH "%PATH%;%CD%"
        set "PATH=%PATH%;%CD%"
        echo Added %CD% to PATH. Please restart your terminal after this session for it to take effect globally.
    )
)

call :ensure_runtime_profile full
if errorlevel 1 exit /b %ERRORLEVEL%

echo Starting Pexo API...
if "%NO_BROWSER%"=="1" (
    set "PEXO_NO_BROWSER=1"
)
venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 9999 --workers 1
pause
exit /b %ERRORLEVEL%

:ensure_venv
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe -c "import sys" >nul 2>nul
    if not errorlevel 1 exit /b 0
    echo Existing virtual environment is unusable. Recreating it...
    rmdir /s /q venv 2>nul
)
echo Virtual environment not found. Creating one...
python -m venv venv
if errorlevel 1 exit /b %ERRORLEVEL%
exit /b 0

:get_profile_rank
set "%~2=0"
if /I "%~1"=="core" set "%~2=1"
if /I "%~1"=="mcp" set "%~2=2"
if /I "%~1"=="full" set "%~2=3"
if /I "%~1"=="vector" set "%~2=4"
exit /b 0

:get_current_profile
set "%~1="
if exist "%DEPENDENCY_MARKER%" (
    set /p current_profile_value=<"%DEPENDENCY_MARKER%"
    set "%~1=!current_profile_value!"
)
exit /b 0

:get_requirements_file
set "%~2="
if /I "%~1"=="core" set "%~2=requirements-core.txt"
if /I "%~1"=="mcp" set "%~2=requirements-mcp.txt"
if /I "%~1"=="full" set "%~2=requirements-full.txt"
if /I "%~1"=="vector" set "%~2=requirements-vector.txt"
if not defined %~2 (
    echo Unsupported dependency profile: %~1
    exit /b 1
)
exit /b 0

:install_profile
call :get_requirements_file "%~1" REQUIREMENTS_FILE
if errorlevel 1 exit /b %ERRORLEVEL%
where uv >nul 2>nul
if errorlevel 1 (
    venv\Scripts\python.exe -m pip install --disable-pip-version-check -r "!REQUIREMENTS_FILE!" -c constraints.txt
    if errorlevel 1 exit /b %ERRORLEVEL%
) else (
    uv pip install --python venv\Scripts\python.exe -r "!REQUIREMENTS_FILE!" -c constraints.txt
    if errorlevel 1 exit /b %ERRORLEVEL%
)
> "%DEPENDENCY_MARKER%" echo %~1
exit /b 0

:ensure_runtime_profile
call :ensure_venv
if errorlevel 1 exit /b %ERRORLEVEL%
call :get_current_profile CURRENT_PROFILE
call :get_profile_rank "%~1" REQUESTED_RANK
call :get_profile_rank "!CURRENT_PROFILE!" CURRENT_RANK
if !CURRENT_RANK! lss !REQUESTED_RANK! (
    echo Preparing the %~1 runtime...
    call :install_profile "%~1"
    if errorlevel 1 exit /b %ERRORLEVEL%
)
exit /b 0

:mcp
call :ensure_runtime_profile mcp
if errorlevel 1 exit /b %ERRORLEVEL%
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
echo   pexo --promote ^| pexo promote [core^|mcp^|full^|vector]
echo                  Installs or upgrades the local runtime dependency profile
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

:promote
shift
if "%~1"=="" (
    set "TARGET_PROFILE=full"
) else (
    set "TARGET_PROFILE=%~1"
)
call :ensure_runtime_profile "%TARGET_PROFILE%"
if errorlevel 1 exit /b %ERRORLEVEL%
echo Pexo runtime is ready at profile '%TARGET_PROFILE%'.
exit /b 0

:listpresets
call :ensure_runtime_profile core
if errorlevel 1 exit /b %ERRORLEVEL%
venv\Scripts\python.exe -m app.cli list-presets %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:headlesssetup
call :ensure_runtime_profile core
if errorlevel 1 exit /b %ERRORLEVEL%
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
