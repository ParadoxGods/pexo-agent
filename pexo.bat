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
if "%~1"=="--doctor" goto doctor
if /I "%~1"=="doctor" goto doctor
if "%~1"=="--connect" goto connect
if /I "%~1"=="connect" goto connect
if "%~1"=="--chat" goto chat
if /I "%~1"=="chat" goto chat
if "%~1"=="--warmup" goto warmup
if /I "%~1"=="warmup" goto warmup
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
    if not errorlevel 1 goto ensure_venv_has_pip
    echo Existing virtual environment is unusable. Recreating it...
    rmdir /s /q venv 2>nul
)
echo Virtual environment not found. Creating one...
python -m venv venv
if errorlevel 1 exit /b %ERRORLEVEL%
:ensure_venv_has_pip
call :ensure_venv_pip
if errorlevel 1 exit /b %ERRORLEVEL%
exit /b 0

:venv_pip_usable
venv\Scripts\python.exe -m pip --version >nul 2>nul
exit /b %ERRORLEVEL%

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

:get_import_code
set "%~2="
if /I "%~1"=="core" set "%~2=import fastapi, pydantic, sqlalchemy"
if /I "%~1"=="mcp" set "%~2=import fastapi, pydantic, sqlalchemy, mcp"
if /I "%~1"=="full" set "%~2=import fastapi, pydantic, sqlalchemy, mcp, uvicorn, langgraph"
if /I "%~1"=="vector" set "%~2=import fastapi, pydantic, sqlalchemy, mcp, uvicorn, langgraph, chromadb"
if not defined %~2 (
    echo Unsupported dependency profile: %~1
    exit /b 1
)
exit /b 0

:profile_ready
call :venv_pip_usable
if errorlevel 1 exit /b 1
call :get_import_code "%~1" PROFILE_IMPORTS
if errorlevel 1 exit /b %ERRORLEVEL%
venv\Scripts\python.exe -c "!PROFILE_IMPORTS!" >nul 2>nul
exit /b %ERRORLEVEL%

:ensure_venv_pip
call :venv_pip_usable
if not errorlevel 1 exit /b 0
echo pip is missing from the virtual environment. Repairing it...
venv\Scripts\python.exe -m ensurepip --upgrade
if errorlevel 1 exit /b %ERRORLEVEL%
call :venv_pip_usable
if errorlevel 1 (
    echo Failed to repair pip in the local virtual environment.
    exit /b 1
)
exit /b 0

:install_profile
call :get_requirements_file "%~1" REQUIREMENTS_FILE
if errorlevel 1 exit /b %ERRORLEVEL%
call :ensure_venv_pip
if errorlevel 1 exit /b %ERRORLEVEL%
where uv >nul 2>nul
if errorlevel 1 (
    venv\Scripts\python.exe -m pip install --disable-pip-version-check -r "!REQUIREMENTS_FILE!" -c constraints.txt
    if errorlevel 1 exit /b %ERRORLEVEL%
) else (
    uv pip install --python venv\Scripts\python.exe -r "!REQUIREMENTS_FILE!" -c constraints.txt
    if errorlevel 1 exit /b %ERRORLEVEL%
)
call :profile_ready "%~1"
if errorlevel 1 (
    echo The '%~1' runtime marker could not be verified after dependency installation.
    exit /b 1
)
> "%DEPENDENCY_MARKER%" echo %~1
exit /b 0

:ensure_runtime_profile
call :ensure_venv
if errorlevel 1 exit /b %ERRORLEVEL%
call :get_current_profile CURRENT_PROFILE
if defined CURRENT_PROFILE (
    call :get_profile_rank "!CURRENT_PROFILE!" CURRENT_RANK
    if !CURRENT_RANK! equ 0 (
        echo Dependency marker '!CURRENT_PROFILE!' is invalid. Reinstalling runtime dependencies...
        del /f /q "%DEPENDENCY_MARKER%" >nul 2>nul
        set "CURRENT_PROFILE="
    ) else (
        call :profile_ready "!CURRENT_PROFILE!"
        if errorlevel 1 (
            echo Dependency marker '!CURRENT_PROFILE!' is stale. Reinstalling runtime dependencies...
            del /f /q "%DEPENDENCY_MARKER%" >nul 2>nul
            set "CURRENT_PROFILE="
        )
    )
)
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

:doctor
shift
call :ensure_runtime_profile core
if errorlevel 1 exit /b %ERRORLEVEL%
venv\Scripts\python.exe -m app.launcher doctor %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:connect
if "%~1"=="--connect" shift
call :ensure_runtime_profile core
if errorlevel 1 exit /b %ERRORLEVEL%
venv\Scripts\python.exe -m app.launcher connect %1 %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:version
echo Pexo v1.1.1
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
echo   pexo --doctor ^| pexo doctor [--json]
echo                  Prints local installation and runtime diagnostics
echo   pexo --connect ^| pexo connect [all^|codex^|claude^|gemini] [--scope user^|project]
echo                  Connects supported AI clients to Pexo MCP
echo   pexo --chat ^| pexo chat [--backend auto^|codex^|claude^|gemini] [--workspace PATH]
echo                  Starts a direct terminal chat with Pexo
echo   pexo warmup
echo                  Primes local state after install or update
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
call :checkout_detached DETACHED_HEAD
if errorlevel 1 exit /b %ERRORLEVEL%
if "!DETACHED_HEAD!"=="1" (
    echo Update skipped because this checkout is pinned to a detached git HEAD. Checkout a branch before pulling updates.
    exit /b 0
)
echo Checking for updates...
git pull --ff-only
if errorlevel 1 exit /b %ERRORLEVEL%
powershell -NoProfile -Command "Set-Content -LiteralPath '%UPDATE_STAMP%' -Value ([DateTime]::UtcNow.Ticks) -Encoding Ascii"
echo Pexo is up to date.
exit /b 0

:chat
if "%~1"=="--chat" shift
call :ensure_venv
if errorlevel 1 exit /b %ERRORLEVEL%
venv\Scripts\python.exe -m app.launcher chat %*
exit /b %ERRORLEVEL%

:warmup
if "%~1"=="--warmup" shift
call :ensure_runtime_profile core
if errorlevel 1 exit /b %ERRORLEVEL%
venv\Scripts\python.exe -m app.launcher warmup %*
exit /b %ERRORLEVEL%

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
call :checkout_detached DETACHED_HEAD
if errorlevel 1 exit /b %ERRORLEVEL%
if "!DETACHED_HEAD!"=="1" (
    echo Update check skipped because this checkout is pinned to a detached git HEAD.
    powershell -NoProfile -Command "Set-Content -LiteralPath '%UPDATE_STAMP%' -Value ([DateTime]::UtcNow.Ticks) -Encoding Ascii"
    exit /b 0
)
echo Checking for updates...
git pull --ff-only --quiet >nul 2>nul
if errorlevel 1 (
    echo Update check failed. Continuing with the local version.
    echo Run 'pexo update' for full git or auth output. If this repo is private, detached, or access-controlled, verify authentication and branch state.
) else (
    powershell -NoProfile -Command "Set-Content -LiteralPath '%UPDATE_STAMP%' -Value ([DateTime]::UtcNow.Ticks) -Encoding Ascii"
)
exit /b 0

:checkout_detached
set "%~1=0"
set "CURRENT_BRANCH="
for /f "usebackq delims=" %%I in (`git rev-parse --abbrev-ref HEAD 2^>nul`) do set "CURRENT_BRANCH=%%I"
if not defined CURRENT_BRANCH (
    set "%~1=1"
    exit /b 0
)
if /I "!CURRENT_BRANCH!"=="HEAD" set "%~1=1"
exit /b 0

:uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
exit /b %ERRORLEVEL%
