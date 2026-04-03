param(
    [switch]$HeadlessSetup,
    [string]$Preset = "efficient_operator",
    [string]$ProfileName = "default_user",
    [string]$BackupPath = ""
)

$ErrorActionPreference = "Stop"
$PexoDir = "$env:USERPROFILE\.pexo"

function Show-InstallProgress {
    param(
        [int]$Percent,
        [string]$Status
    )

    Write-Progress -Activity "Installing Pexo" -Status $Status -PercentComplete $Percent
    Write-Host ("[{0,3}%] {1}" -f $Percent, $Status)
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

    $processArgs = @{
        FilePath = $FilePath
        ArgumentList = $ArgumentList
        PassThru = $true
        NoNewWindow = $true
    }
    if ($WorkingDirectory) {
        $processArgs.WorkingDirectory = $WorkingDirectory
    }

    $process = Start-Process @processArgs
    while (-not $process.HasExited) {
        Start-Sleep -Seconds 5
        if (-not $process.HasExited) {
            Write-Progress -Activity "Installing Pexo" -Status $HeartbeatMessage -PercentComplete $Percent
            Write-Host ("[{0,3}%] {1}" -f $Percent, $HeartbeatMessage)
        }
    }

    if ($process.ExitCode -ne 0) {
        throw "Command failed with exit code $($process.ExitCode): $FilePath $($ArgumentList -join ' ')"
    }
}

Write-Host "=================================================="
Write-Host "Installing Pexo (The OpenClaw Killer) Globally..."
Write-Host "=================================================="

Show-InstallProgress -Percent 5 -Status "Validating install target at $PexoDir"
if (Test-Path "$PexoDir\.git") {
    Invoke-TrackedProcess -Percent 20 -StartMessage "Existing installation found. Updating repository in place..." -HeartbeatMessage "Updating repository... still working" -FilePath "git" -ArgumentList @("-C", $PexoDir, "pull", "--ff-only")
}
elseif (Test-Path $PexoDir) {
    throw "The directory '$PexoDir' already exists but is not a Pexo git checkout. Move or remove it and rerun the installer."
}
else {
    Invoke-TrackedProcess -Percent 20 -StartMessage "Cloning repository to $PexoDir..." -HeartbeatMessage "Cloning repository... still working" -FilePath "git" -ArgumentList @("clone", "https://github.com/ParadoxGods/pexo-agent.git", $PexoDir)
}

Set-Location $PexoDir

Show-InstallProgress -Percent 40 -Status "Preparing isolated Python environment"
if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Invoke-TrackedProcess -Percent 45 -StartMessage "Creating Python virtual environment..." -HeartbeatMessage "Creating Python virtual environment... still working" -FilePath "python" -ArgumentList @("-m", "venv", "venv") -WorkingDirectory $PexoDir
}
Invoke-TrackedProcess -Percent 60 -StartMessage "Upgrading pip..." -HeartbeatMessage "Upgrading pip... still working" -FilePath ".\venv\Scripts\python.exe" -ArgumentList @("-m", "pip", "install", "--upgrade", "pip") -WorkingDirectory $PexoDir
Invoke-TrackedProcess -Percent 75 -StartMessage "Installing Python dependencies (this can take a while)..." -HeartbeatMessage "Installing Python dependencies... still working" -FilePath ".\venv\Scripts\python.exe" -ArgumentList @("-m", "pip", "install", "-r", "requirements.txt") -WorkingDirectory $PexoDir

Show-InstallProgress -Percent 90 -Status "Adding Pexo to your user PATH"
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathEntries = @()
if (-not [string]::IsNullOrWhiteSpace($UserPath)) {
    $PathEntries = $UserPath.Split(";") | Where-Object { $_ }
}
if ($PathEntries -notcontains $PexoDir) {
    [Environment]::SetEnvironmentVariable("Path", (($PathEntries + $PexoDir) -join ";"), "User")
}

if ($HeadlessSetup) {
    $HeadlessArgs = @("-m", "app.cli", "headless-setup", "--preset", $Preset, "--name", $ProfileName)
    if (-not [string]::IsNullOrWhiteSpace($BackupPath)) {
        $HeadlessArgs += @("--backup-path", $BackupPath)
    }
    Invoke-TrackedProcess -Percent 95 -StartMessage "Applying headless profile setup..." -HeartbeatMessage "Applying headless profile setup... still working" -FilePath ".\venv\Scripts\python.exe" -ArgumentList $HeadlessArgs -WorkingDirectory $PexoDir
}

Show-InstallProgress -Percent 100 -Status "Installation complete"
Write-Progress -Activity "Installing Pexo" -Completed -Status "Installation complete"
Write-Host "=================================================="
Write-Host "Pexo installed successfully!"
Write-Host "AI AGENT: Restart the terminal only if the user needs the refreshed PATH in a new shell."
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
