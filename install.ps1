param(
    [switch]$HeadlessSetup,
    [string]$Preset = "efficient_operator",
    [string]$ProfileName = "default_user",
    [string]$BackupPath = "",
    [string]$Repository = "ParadoxGods/pexo-agent",
    [string]$InstallDir = "",
    [string]$RepoPath = "",
    [switch]$UseCurrentCheckout,
    [ValidateSet("auto", "core", "mcp", "full", "vector")]
    [string]$InstallProfile = "auto",
    [switch]$SkipUpdate,
    [switch]$Offline
)

$ErrorActionPreference = "Stop"
$ScriptRoot = [System.IO.Path]::GetFullPath((Split-Path -Path $MyInvocation.MyCommand.Path -Parent))

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

function Test-UsableVenvPython {
    param([string]$PythonPath)

    if (-not (Test-Path $PythonPath)) {
        return $false
    }

    & $PythonPath -c "import sys" *> $null
    return $LASTEXITCODE -eq 0
}

function Resolve-FullPath {
    param([string]$PathValue)

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return ""
    }

    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }

    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $PathValue))
}

function Get-InstallDirectory {
    if ($UseCurrentCheckout -and -not [string]::IsNullOrWhiteSpace($RepoPath)) {
        throw "Use either -UseCurrentCheckout or -RepoPath, not both."
    }

    if ($UseCurrentCheckout) {
        return $ScriptRoot
    }

    if (-not [string]::IsNullOrWhiteSpace($RepoPath)) {
        return Resolve-FullPath -PathValue $RepoPath
    }

    if (-not [string]::IsNullOrWhiteSpace($InstallDir)) {
        return Resolve-FullPath -PathValue $InstallDir
    }

    return [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".pexo"))
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

        $stdoutText = ([string](Get-Content -LiteralPath $stdoutFile -Raw -ErrorAction SilentlyContinue)).Trim()
        $stderrText = ([string](Get-Content -LiteralPath $stderrFile -Raw -ErrorAction SilentlyContinue)).Trim()

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
            Method = "gh"
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
        Method = "git"
    }
}

function Assert-Preflight {
    param(
        [string]$ResolvedInstallDir,
        [bool]$UsingExistingCheckout
    )

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

    $probeTarget = if ($UsingExistingCheckout) {
        if (-not (Test-Path $ResolvedInstallDir)) {
            throw "The existing checkout path '$ResolvedInstallDir' does not exist."
        }
        $ResolvedInstallDir
    }
    else {
        Split-Path -Path $ResolvedInstallDir -Parent
    }

    $probePath = Join-Path $probeTarget ".pexo-install-write-test"
    try {
        Set-Content -LiteralPath $probePath -Value "ok" -Encoding Ascii
        Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
    }
    catch {
        throw "The installer cannot write to '$probeTarget'. Check permissions and rerun."
    }

    if (-not (Test-CommandAvailable "cl.exe")) {
        Write-Host "[NOTE] Microsoft C++ Build Tools were not detected. Install them only if you later enable the optional vector-memory runtime and pip needs to build native wheels."
    }
}

function Get-DependencyRank {
    param([string]$Profile)

    switch ($Profile) {
        "core" { return 1 }
        "mcp" { return 2 }
        "full" { return 3 }
        "vector" { return 4 }
        default { return 0 }
    }
}

function Get-RequestedDependencyProfile {
    if ($InstallProfile -ne "auto") {
        return $InstallProfile
    }

    return "core"
}

function Get-RequirementsFile {
    param([string]$Profile)

    switch ($Profile) {
        "core" { return "requirements-core.txt" }
        "mcp" { return "requirements-mcp.txt" }
        "full" { return "requirements-full.txt" }
        "vector" { return "requirements-vector.txt" }
        default { throw "Unsupported dependency profile '$Profile'." }
    }
}

function Get-DependencyMarkerPath {
    param([string]$ResolvedInstallDir)
    return Join-Path $ResolvedInstallDir ".pexo-deps-profile"
}

function Get-CurrentDependencyProfile {
    param([string]$ResolvedInstallDir)

    $markerPath = Get-DependencyMarkerPath -ResolvedInstallDir $ResolvedInstallDir
    if (-not (Test-Path $markerPath)) {
        return ""
    }

    return ([string](Get-Content -LiteralPath $markerPath -Raw -ErrorAction SilentlyContinue)).Trim().ToLowerInvariant()
}

function Set-CurrentDependencyProfile {
    param(
        [string]$ResolvedInstallDir,
        [string]$Profile
    )

    $markerPath = Get-DependencyMarkerPath -ResolvedInstallDir $ResolvedInstallDir
    Set-Content -LiteralPath $markerPath -Value $Profile -Encoding Ascii
}

function Install-DependencyProfile {
    param(
        [string]$ResolvedInstallDir,
        [string]$Profile,
        [int]$Percent,
        [string]$StartMessage,
        [string]$HeartbeatMessage
    )

    $venvPython = Join-Path $ResolvedInstallDir "venv\Scripts\python.exe"
    $requirementsFile = Join-Path $ResolvedInstallDir (Get-RequirementsFile -Profile $Profile)
    $constraintsFile = Join-Path $ResolvedInstallDir "constraints.txt"

    if (Test-CommandAvailable "uv") {
        Invoke-TrackedProcess -Percent $Percent -StartMessage $StartMessage -HeartbeatMessage $HeartbeatMessage -FilePath "uv" -ArgumentList @("pip", "install", "--python", $venvPython, "-r", $requirementsFile, "-c", $constraintsFile) -WorkingDirectory $ResolvedInstallDir
    }
    else {
        Invoke-TrackedProcess -Percent $Percent -StartMessage $StartMessage -HeartbeatMessage $HeartbeatMessage -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "-r", $requirementsFile, "-c", $constraintsFile) -WorkingDirectory $ResolvedInstallDir
    }

    Set-CurrentDependencyProfile -ResolvedInstallDir $ResolvedInstallDir -Profile $Profile
}

function Format-WindowsMcpSnippet {
    param([string]$LauncherPath)

    $escapedPath = $LauncherPath.Replace("\", "\\")
    return @"
{
  "mcpServers": {
    "pexo": {
      "command": "cmd.exe",
      "args": ["/c", "$escapedPath", "--mcp"]
    }
  }
}
"@
}

$PexoDir = Get-InstallDirectory
$UsingExistingCheckout = $UseCurrentCheckout -or -not [string]::IsNullOrWhiteSpace($RepoPath)
$RequestedProfile = Get-RequestedDependencyProfile
$CloneMethodSummary = "pending"

Write-Host "=================================================="
Write-Host "Installing Pexo (The OpenClaw Killer) ..."
Write-Host "=================================================="

Assert-Preflight -ResolvedInstallDir $PexoDir -UsingExistingCheckout $UsingExistingCheckout

Show-InstallProgress -Percent 5 -Status "Validating install target at $PexoDir"
if ($UsingExistingCheckout) {
    if (-not (Test-Path (Join-Path $PexoDir ".git"))) {
        throw "The checkout at '$PexoDir' is missing a .git directory. Use -InstallDir to clone a new copy or point -RepoPath at an existing checkout."
    }

    if ($SkipUpdate -or $Offline) {
        Show-InstallProgress -Percent 20 -Status "Using existing checkout. Skipping repository update."
        $CloneMethodSummary = "existing checkout ($PexoDir), update skipped"
    }
    else {
        Invoke-TrackedProcess -Percent 20 -StartMessage "Using existing checkout. Updating repository in place..." -HeartbeatMessage "Updating repository... still working" -FilePath "git" -ArgumentList @("-C", $PexoDir, "pull", "--ff-only")
        $CloneMethodSummary = "existing checkout ($PexoDir), updated via git pull"
    }
}
elseif (Test-Path (Join-Path $PexoDir ".git")) {
    if ($SkipUpdate -or $Offline) {
        Show-InstallProgress -Percent 20 -Status "Existing installation found. Skipping repository update."
        $CloneMethodSummary = "existing installation at $PexoDir, update skipped"
    }
    else {
        Invoke-TrackedProcess -Percent 20 -StartMessage "Existing installation found. Updating repository in place..." -HeartbeatMessage "Updating repository... still working" -FilePath "git" -ArgumentList @("-C", $PexoDir, "pull", "--ff-only")
        $CloneMethodSummary = "existing installation at $PexoDir, updated via git pull"
    }
}
elseif (Test-Path $PexoDir) {
    throw "The directory '$PexoDir' already exists but is not a Pexo git checkout. Move or remove it, or use -RepoPath to target an existing checkout."
}
else {
    $clone = Get-CloneInvocation -Repository $Repository -TargetDir $PexoDir
    Invoke-TrackedProcess -Percent 20 -StartMessage "Cloning repository to $PexoDir..." -HeartbeatMessage "Cloning repository... still working" -FilePath $clone.FilePath -ArgumentList $clone.ArgumentList
    $CloneMethodSummary = $clone.Display
}

Set-Location $PexoDir

Show-InstallProgress -Percent 40 -Status "Preparing isolated Python environment"
$createdVenv = $false
$venvPythonPath = Join-Path $PexoDir "venv\Scripts\python.exe"
if (-not (Test-UsableVenvPython $venvPythonPath)) {
    if (Test-Path (Join-Path $PexoDir "venv")) {
        Show-InstallProgress -Percent 43 -Status "Existing virtual environment is unusable. Recreating it..."
        Remove-Item -LiteralPath (Join-Path $PexoDir "venv") -Recurse -Force -ErrorAction SilentlyContinue
    }
    $createdVenv = $true
    Invoke-TrackedProcess -Percent 45 -StartMessage "Creating Python virtual environment..." -HeartbeatMessage "Creating Python virtual environment... still working" -FilePath "python" -ArgumentList @("-m", "venv", "venv") -WorkingDirectory $PexoDir
}

$currentProfile = Get-CurrentDependencyProfile -ResolvedInstallDir $PexoDir
if ((Get-DependencyRank -Profile $currentProfile) -lt (Get-DependencyRank -Profile $RequestedProfile)) {
    $dependencyMessage = if ($createdVenv -or [string]::IsNullOrWhiteSpace($currentProfile)) {
        "Installing Python dependencies ($RequestedProfile runtime)..."
    }
    else {
        "Promoting Python runtime to the $RequestedProfile profile..."
    }
    Install-DependencyProfile -ResolvedInstallDir $PexoDir -Profile $RequestedProfile -Percent 70 -StartMessage $dependencyMessage -HeartbeatMessage "$dependencyMessage still working"
    $finalProfile = $RequestedProfile
}
else {
    $finalProfile = $currentProfile
    Show-InstallProgress -Percent 70 -Status "Python dependency profile '$finalProfile' already satisfies the requested runtime."
}

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

$launcherPath = Join-Path $PexoDir "pexo.bat"
if (Get-Command pexo -ErrorAction SilentlyContinue) {
    Write-Host "[ 90%] Same-shell PATH activation verified."
}
else {
    Write-Host "[ 90%] Same-shell PATH activation could not be verified. Use `"$launcherPath`" directly in this shell if needed."
}

if ($HeadlessSetup) {
    $headlessArgs = @("-m", "app.cli", "headless-setup", "--preset", $Preset, "--name", $ProfileName)
    if (-not [string]::IsNullOrWhiteSpace($BackupPath)) {
        $headlessArgs += @("--backup-path", $BackupPath)
    }
    Invoke-TrackedProcess -Percent 95 -StartMessage "Applying headless profile setup..." -HeartbeatMessage "Applying headless profile setup... still working" -FilePath ".\venv\Scripts\python.exe" -ArgumentList $headlessArgs -WorkingDirectory $PexoDir
}

$databasePath = Join-Path $PexoDir "pexo.db"
$memoryStorePath = Join-Path $PexoDir "chroma_db"
$effectiveBackupPath = if ($HeadlessSetup -and -not [string]::IsNullOrWhiteSpace($BackupPath)) { $BackupPath } elseif ($HeadlessSetup) { "not set" } else { "not configured during install" }
$profileSummary = if ($HeadlessSetup) { $ProfileName } else { "not initialized" }

Show-InstallProgress -Percent 100 -Status "Installation complete"
Write-Progress -Activity "Installing Pexo" -Completed -Status "Installation complete"
Write-Host "=================================================="
Write-Host "Pexo installed successfully!"
if ($SkipUpdate -or $Offline) {
    Write-Host "Repository update was skipped for this install."
}
Write-Host "Clone method: $CloneMethodSummary"
Write-Host "Install directory: $PexoDir"
Write-Host "Dependency profile ready now: $finalProfile"
Write-Host "Profile initialized: $profileSummary"
Write-Host "Backup path: $effectiveBackupPath"
Write-Host "Local database path: $databasePath"
Write-Host "Local vector store path: $memoryStorePath"
Write-Host "Works now in this shell via absolute path:"
Write-Host "  & `"$launcherPath`" --version"
Write-Host "Works after opening a new shell via bare command:"
Write-Host "  pexo --version"
if (-not $HeadlessSetup) {
    Write-Host "Preferred same-shell setup path:"
    Write-Host "  & `"$launcherPath`" --headless-setup --preset $Preset"
    Write-Host "After reopening a shell, the same setup command also works as:"
    Write-Host "  pexo --headless-setup --preset $Preset"
}
Write-Host "If you want the full browser UI and LangGraph runtime installed ahead of first launch:"
Write-Host "  & `"$launcherPath`" --promote full"
Write-Host "If you want native Chroma vector embeddings installed as well:"
Write-Host "  & `"$launcherPath`" --promote vector"
Write-Host "Ready-to-paste Windows MCP config:"
Write-Host (Format-WindowsMcpSnippet -LauncherPath $launcherPath)
if ($HeadlessSetup) {
    Write-Host "Headless profile setup completed during install."
    Write-Host "Run 'pexo' later when the user wants the local dashboard for memory, agents, and configuration."
}
else {
    Write-Host "Run 'pexo' later only when the user wants the local dashboard at http://127.0.0.1:9999."
}
Write-Host "=================================================="
