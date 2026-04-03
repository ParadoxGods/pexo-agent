param(
    [switch]$HeadlessSetup,
    [string]$Preset = "efficient_operator",
    [string]$ProfileName = "default_user",
    [string]$BackupPath = "",
    [string]$Repository = "ParadoxGods/pexo-agent",
    [switch]$SkipUpdate,
    [switch]$Offline
)

$ErrorActionPreference = "Stop"
$PexoDir = Join-Path $env:USERPROFILE ".pexo"

function Show-InstallProgress {
    param(
        [int]$Percent,
        [string]$Status
    )

    Write-Progress -Activity "Installing Pexo" -Status $Status -PercentComplete $Percent
    Write-Host ("[{0,3}%] {1}" -f $Percent, $Status)
}

function Test-CommandAvailable {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-TrackedProcess {
    param(
        [int]$Percent,
        [string]$StartMessage,
        [string]$HeartbeatMessage,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory = $null
    )

    Show-InstallProgress -Percent $Percent -Status $StartMessage

    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()

    try {
        $processArgs = @{
            FilePath = $FilePath
            ArgumentList = $ArgumentList
            PassThru = $true
            NoNewWindow = $true
            RedirectStandardOutput = $stdoutFile
            RedirectStandardError = $stderrFile
        }
        if ($WorkingDirectory) {
            $processArgs.WorkingDirectory = $WorkingDirectory
        }

        $process = Start-Process @processArgs
        while (-not $process.WaitForExit(5000)) {
            Write-Progress -Activity "Installing Pexo" -Status $HeartbeatMessage -PercentComplete $Percent
            Write-Host ("[{0,3}%] {1}" -f $Percent, $HeartbeatMessage)
        }
        $process.WaitForExit()
        $process.Refresh()

        $stdoutText = (Get-Content -LiteralPath $stdoutFile -Raw -ErrorAction SilentlyContinue).Trim()
        $stderrText = (Get-Content -LiteralPath $stderrFile -Raw -ErrorAction SilentlyContinue).Trim()

        if ($process.ExitCode -ne 0) {
            $errorText = if ($stderrText) { $stderrText } elseif ($stdoutText) { $stdoutText } else { "No process output was captured." }
            throw "Command failed with exit code $($process.ExitCode): $FilePath $($ArgumentList -join ' ')`n$errorText"
        }

        if ($stdoutText) {
            Write-Host $stdoutText
        }
        if ($stderrText) {
            Write-Host $stderrText
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

function Test-GhAuthentication {
    if (-not (Test-CommandAvailable "gh")) {
        return $false
    }

    & gh auth status -h github.com *> $null
    return $LASTEXITCODE -eq 0
}

function Get-CloneInvocation {
    param(
        [string]$Repository,
        [string]$TargetDir
    )

    if ($Repository -match "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$" -and (Test-GhAuthentication)) {
        return @{
            FilePath = "gh"
            ArgumentList = @("repo", "clone", $Repository, $TargetDir)
            Display = "gh repo clone $Repository $TargetDir"
        }
    }

    $cloneSource = if ($Repository -match "^(https?|git@)") {
        $Repository
    }
    else {
        "https://github.com/$Repository.git"
    }

    return @{
        FilePath = "git"
        ArgumentList = @("clone", $cloneSource, $TargetDir)
        Display = "git clone $cloneSource $TargetDir"
    }
}

function Assert-Preflight {
    Show-InstallProgress -Percent 2 -Status "Running installer preflight checks"

    if (-not (Test-CommandAvailable "git")) {
        throw "Git is required to install Pexo. Install Git and rerun the installer."
    }
    if (-not (Test-CommandAvailable "python")) {
        throw "Python 3.11 or newer is required to install Pexo. Ensure 'python' resolves in this shell and rerun the installer."
    }

    & python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
    if ($LASTEXITCODE -ne 0) {
        $pythonVersion = (& python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
        throw "Python 3.11 or newer is required. Detected Python $pythonVersion."
    }

    $installParent = Split-Path -Path $PexoDir -Parent
    $probePath = Join-Path $installParent ".pexo-install-write-test"
    try {
        Set-Content -LiteralPath $probePath -Value "ok" -Encoding Ascii
        Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
    }
    catch {
        throw "The installer cannot write to '$installParent'. Check permissions and rerun."
    }

    if (-not (Test-CommandAvailable "cl.exe")) {
        Write-Host "[NOTE] Microsoft C++ Build Tools were not detected. Install them only if pip later needs to build native wheels."
    }
}

Write-Host "=================================================="
Write-Host "Installing Pexo (The OpenClaw Killer) Globally..."
Write-Host "=================================================="

Assert-Preflight

Show-InstallProgress -Percent 5 -Status "Validating install target at $PexoDir"
if (Test-Path "$PexoDir\.git") {
    if ($SkipUpdate -or $Offline) {
        Show-InstallProgress -Percent 20 -Status "Existing installation found. Skipping repository update."
    }
    else {
        Invoke-TrackedProcess -Percent 20 -StartMessage "Existing installation found. Updating repository in place..." -HeartbeatMessage "Updating repository... still working" -FilePath "git" -ArgumentList @("-C", $PexoDir, "pull", "--ff-only")
    }
}
elseif (Test-Path $PexoDir) {
    throw "The directory '$PexoDir' already exists but is not a Pexo git checkout. Move or remove it and rerun the installer."
}
else {
    $clone = Get-CloneInvocation -Repository $Repository -TargetDir $PexoDir
    Invoke-TrackedProcess -Percent 20 -StartMessage "Cloning repository to $PexoDir..." -HeartbeatMessage "Cloning repository... still working" -FilePath $clone.FilePath -ArgumentList $clone.ArgumentList
}

Set-Location $PexoDir

Show-InstallProgress -Percent 40 -Status "Preparing isolated Python environment"
$createdVenv = $false
if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    $createdVenv = $true
    Invoke-TrackedProcess -Percent 45 -StartMessage "Creating Python virtual environment..." -HeartbeatMessage "Creating Python virtual environment... still working" -FilePath "python" -ArgumentList @("-m", "venv", "venv") -WorkingDirectory $PexoDir
}

$dependencyMessage = if ($createdVenv) {
    "Installing Python dependencies..."
}
else {
    "Syncing Python dependencies..."
}
Invoke-TrackedProcess -Percent 70 -StartMessage $dependencyMessage -HeartbeatMessage "$dependencyMessage still working" -FilePath ".\venv\Scripts\python.exe" -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt") -WorkingDirectory $PexoDir

Show-InstallProgress -Percent 90 -Status "Adding Pexo to your user PATH"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$userEntries = @()
if (-not [string]::IsNullOrWhiteSpace($userPath)) {
    $userEntries = $userPath.Split(";") | Where-Object { $_ }
}
if ($userEntries -notcontains $PexoDir) {
    [Environment]::SetEnvironmentVariable("Path", (($userEntries + $PexoDir) -join ";"), "User")
}

$sessionEntries = @()
if (-not [string]::IsNullOrWhiteSpace($env:Path)) {
    $sessionEntries = $env:Path.Split(";") | Where-Object { $_ }
}
if ($sessionEntries -notcontains $PexoDir) {
    $env:Path = (($sessionEntries + $PexoDir) -join ";")
}

if (Get-Command pexo -ErrorAction SilentlyContinue) {
    Write-Host "[ 90%] Same-shell PATH activation verified."
}
else {
    Write-Host "[ 90%] Same-shell PATH activation could not be verified. Use `"$PexoDir\pexo.bat`" directly in this shell if needed."
}

if ($HeadlessSetup) {
    $headlessArgs = @("-m", "app.cli", "headless-setup", "--preset", $Preset, "--name", $ProfileName)
    if (-not [string]::IsNullOrWhiteSpace($BackupPath)) {
        $headlessArgs += @("--backup-path", $BackupPath)
    }
    Invoke-TrackedProcess -Percent 95 -StartMessage "Applying headless profile setup..." -HeartbeatMessage "Applying headless profile setup... still working" -FilePath ".\venv\Scripts\python.exe" -ArgumentList $headlessArgs -WorkingDirectory $PexoDir
}

Show-InstallProgress -Percent 100 -Status "Installation complete"
Write-Progress -Activity "Installing Pexo" -Completed -Status "Installation complete"
Write-Host "=================================================="
Write-Host "Pexo installed successfully!"
if ($SkipUpdate -or $Offline) {
    Write-Host "Repository update was skipped for this install."
}
Write-Host "AI AGENT: Restart the terminal only if the user needs the refreshed PATH in a brand new shell."
if ($HeadlessSetup) {
    Write-Host "Headless profile setup completed during install."
    Write-Host "Run 'pexo' later when the user wants the local dashboard for memory, agents, and configuration."
}
else {
    Write-Host "Preferred same-shell setup path:"
    Write-Host "  & `"$PexoDir\pexo.bat`" --headless-setup --preset $Preset"
    Write-Host "After the terminal is restarted, the same command also works as:"
    Write-Host "  pexo --headless-setup --preset $Preset"
    Write-Host "Run 'pexo' later only when the user wants the local dashboard at http://127.0.0.1:9999."
}
Write-Host "=================================================="
