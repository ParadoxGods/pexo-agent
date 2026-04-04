param(
    [switch]$HeadlessSetup,
    [string]$Preset = "efficient_operator",
    [string]$ProfileName = "default_user",
    [string]$BackupPath = "",
    [string]$Repository = "ParadoxGods/pexo-agent",
    [string]$InstallDir = "",
    [string]$RepoPath = "",
    [switch]$UseCurrentCheckout,
    [switch]$AllowRepoInstall,
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

function Test-UsableVenvPip {
    param([string]$PythonPath)

    if (-not (Test-UsableVenvPython $PythonPath)) {
        return $false
    }

    & $PythonPath -m pip --version *> $null
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

function Get-DefaultInstallDirectory {
    return [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".pexo"))
}

function Test-SamePath {
    param(
        [string]$LeftPath,
        [string]$RightPath
    )

    if ([string]::IsNullOrWhiteSpace($LeftPath) -or [string]::IsNullOrWhiteSpace($RightPath)) {
        return $false
    }

    $normalizedLeft = [System.IO.Path]::GetFullPath($LeftPath).TrimEnd('\', '/')
    $normalizedRight = [System.IO.Path]::GetFullPath($RightPath).TrimEnd('\', '/')
    return [string]::Equals($normalizedLeft, $normalizedRight, [System.StringComparison]::OrdinalIgnoreCase)
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

    return Get-DefaultInstallDirectory
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

        $stdoutText = [string](Get-Content -LiteralPath $stdoutFile -Raw -ErrorAction SilentlyContinue)
        if ($null -eq $stdoutText) {
            $stdoutText = ""
        }
        $stdoutText = $stdoutText.Trim()

        $stderrText = [string](Get-Content -LiteralPath $stderrFile -Raw -ErrorAction SilentlyContinue)
        if ($null -eq $stderrText) {
            $stderrText = ""
        }
        $stderrText = $stderrText.Trim()

        $exitCode = $process.ExitCode
        if ($null -eq $exitCode) {
            $exitCode = 0
        }

        if ($exitCode -ne 0) {
            $errorText = if ($stderrText) { $stderrText } elseif ($stdoutText) { $stdoutText } else { "No process output was captured." }
            throw "Command failed with exit code $exitCode: $FilePath $($ArgumentList -join ' ')`n$errorText"
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

function Get-GitBranchName {
    param([string]$RepositoryPath)

    $branchText = [string](& git -C $RepositoryPath rev-parse --abbrev-ref HEAD 2>$null | Out-String)
    if ($LASTEXITCODE -ne 0 -or $null -eq $branchText) {
        return ""
    }
    return $branchText.Trim()
}

function Test-GitDetachedHead {
    param([string]$RepositoryPath)

    $branchName = Get-GitBranchName -RepositoryPath $RepositoryPath
    return [string]::IsNullOrWhiteSpace($branchName) -or $branchName -eq "HEAD"
}

function Resolve-PackageSource {
    param([string]$Repository)

    if ($Repository -match "^git\+") {
        return $Repository
    }
    if ($Repository -match "^https?://") {
        return "git+$Repository"
    }
    if ($Repository -match "^git@") {
        return $Repository
    }
    if ($Repository -match "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$") {
        return "git+https://github.com/$Repository.git"
    }

    throw "Unsupported repository source '$Repository' for packaged installation."
}

function Get-PackagedInstallTool {
    if (Test-CommandAvailable "uv") {
        return "uv"
    }
    if (Test-CommandAvailable "pipx") {
        return "pipx"
    }
    return ""
}

function Should-UsePackagedInstall {
    param([bool]$UsingExistingCheckout)

    if ($UsingExistingCheckout) {
        return $false
    }
    if (-not [string]::IsNullOrWhiteSpace($InstallDir)) {
        return $false
    }
    return -not [string]::IsNullOrWhiteSpace((Get-PackagedInstallTool))
}

function Get-PipxBinDirectory {
    if (-not (Test-CommandAvailable "pipx")) {
        return ""
    }

    try {
        $binDir = [string](& pipx environment --value PIPX_BIN_DIR 2>$null | Out-String)
        if ($null -ne $binDir) {
            $binDir = $binDir.Trim()
        }
        if (-not [string]::IsNullOrWhiteSpace($binDir)) {
            return $binDir
        }
    }
    catch {
    }

    return [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".local\bin"))
}

function Add-ToSessionPath {
    param([string]$Entry)

    if ([string]::IsNullOrWhiteSpace($Entry)) {
        return
    }

    $sessionEntries = @()
    if (-not [string]::IsNullOrWhiteSpace($env:Path)) {
        $sessionEntries = $env:Path.Split(";") | Where-Object { $_ }
    }
    if ($sessionEntries -notcontains $Entry) {
        $env:Path = (($sessionEntries + $Entry) -join ";")
    }
}

function Write-InstallSummaryJson {
    param([hashtable]$Summary)

    $json = ($Summary | ConvertTo-Json -Compress -Depth 6)
    Write-Host "PEXO_INSTALL_SUMMARY_JSON=$json"
}

function Format-PackagedWindowsMcpSnippet {
    return @"
{
  "mcpServers": {
    "pexo": {
      "command": "pexo-mcp",
      "args": []
    }
  }
}
"@
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

function Get-DependencyImportCommand {
    param([string]$Profile)

    switch ($Profile) {
        "core" { return "import fastapi, pydantic, sqlalchemy" }
        "mcp" { return "import fastapi, pydantic, sqlalchemy, mcp" }
        "full" { return "import fastapi, pydantic, sqlalchemy, mcp, uvicorn, langgraph" }
        "vector" { return "import fastapi, pydantic, sqlalchemy, mcp, uvicorn, langgraph, chromadb" }
        default { throw "Unsupported dependency profile '$Profile'." }
    }
}

function Test-DependencyProfileReady {
    param(
        [string]$PythonPath,
        [string]$Profile
    )

    if (-not (Test-UsableVenvPip $PythonPath)) {
        return $false
    }

    & $PythonPath -c (Get-DependencyImportCommand -Profile $Profile) *> $null
    return $LASTEXITCODE -eq 0
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

    $profileText = [string](Get-Content -LiteralPath $markerPath -Raw -ErrorAction SilentlyContinue)
    if ($null -eq $profileText) {
        return ""
    }
    return $profileText.Trim().ToLowerInvariant()
}

function Clear-CurrentDependencyProfile {
    param([string]$ResolvedInstallDir)

    $markerPath = Get-DependencyMarkerPath -ResolvedInstallDir $ResolvedInstallDir
    Remove-Item -LiteralPath $markerPath -Force -ErrorAction SilentlyContinue
}

function Set-CurrentDependencyProfile {
    param(
        [string]$ResolvedInstallDir,
        [string]$Profile
    )

    $markerPath = Get-DependencyMarkerPath -ResolvedInstallDir $ResolvedInstallDir
    Set-Content -LiteralPath $markerPath -Value $Profile -Encoding Ascii
}

function Ensure-VenvPip {
    param(
        [string]$ResolvedInstallDir,
        [int]$Percent,
        [string]$StartMessage,
        [string]$HeartbeatMessage
    )

    $venvPython = Join-Path $ResolvedInstallDir "venv\Scripts\python.exe"
    if (Test-UsableVenvPip $venvPython) {
        return
    }

    Invoke-TrackedProcess -Percent $Percent -StartMessage $StartMessage -HeartbeatMessage $HeartbeatMessage -FilePath $venvPython -ArgumentList @("-m", "ensurepip", "--upgrade") -WorkingDirectory $ResolvedInstallDir

    if (-not (Test-UsableVenvPip $venvPython)) {
        throw "The Python virtual environment at '$ResolvedInstallDir' does not have a working pip module after ensurepip repair."
    }
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

    Ensure-VenvPip -ResolvedInstallDir $ResolvedInstallDir -Percent ([Math]::Max($Percent - 5, 50)) -StartMessage "Ensuring pip is available in the virtual environment..." -HeartbeatMessage "Repairing pip in the virtual environment... still working"

    if (Test-CommandAvailable "uv") {
        Invoke-TrackedProcess -Percent $Percent -StartMessage $StartMessage -HeartbeatMessage $HeartbeatMessage -FilePath "uv" -ArgumentList @("pip", "install", "--python", $venvPython, "-r", $requirementsFile, "-c", $constraintsFile) -WorkingDirectory $ResolvedInstallDir
    }
    else {
        Invoke-TrackedProcess -Percent $Percent -StartMessage $StartMessage -HeartbeatMessage $HeartbeatMessage -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "-r", $requirementsFile, "-c", $constraintsFile) -WorkingDirectory $ResolvedInstallDir
    }

    if (-not (Test-DependencyProfileReady -PythonPath $venvPython -Profile $Profile)) {
        throw "The '$Profile' runtime marker could not be verified after dependency installation."
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
$DefaultPexoDir = Get-DefaultInstallDirectory
$UsingExistingCheckout = $UseCurrentCheckout -or -not [string]::IsNullOrWhiteSpace($RepoPath)
$RequestedProfile = Get-RequestedDependencyProfile
$CloneMethodSummary = "pending"
$ProtectedCheckoutPath = ""

if (-not $AllowRepoInstall -and (Test-Path (Join-Path $PexoDir ".git")) -and -not (Test-SamePath -LeftPath $PexoDir -RightPath $DefaultPexoDir)) {
    $ProtectedCheckoutPath = $PexoDir
    $PexoDir = $DefaultPexoDir
    $UsingExistingCheckout = $false
}

Write-Host "=================================================="
Write-Host "Installing Pexo (The OpenClaw Killer) ..."
Write-Host "=================================================="

Assert-Preflight -ResolvedInstallDir $PexoDir -UsingExistingCheckout $UsingExistingCheckout

if (Should-UsePackagedInstall -UsingExistingCheckout:$UsingExistingCheckout) {
    $packageSource = Resolve-PackageSource -Repository $Repository
    $packagedTool = Get-PackagedInstallTool
    $packagedModeLabel = ""

    if ($packagedTool -eq "uv") {
        Invoke-TrackedProcess -Percent 20 -StartMessage "Installing packaged Pexo tool from GitHub..." -HeartbeatMessage "Installing packaged Pexo tool... still working" -FilePath "uv" -ArgumentList @("tool", "install", "--reinstall", $packageSource)
        try {
            & uv tool update-shell *> $null
        }
        catch {
        }

        $uvBinDir = ""
        try {
            $uvBinDir = (& uv tool dir --bin 2>$null | Out-String).Trim()
        }
        catch {
            $uvBinDir = ""
        }
        Add-ToSessionPath -Entry $uvBinDir
        $packagedModeLabel = "packaged GitHub tool via uv"
        $uninstallCommand = "uv tool uninstall pexo-agent"
    }
    else {
        Invoke-TrackedProcess -Percent 20 -StartMessage "Installing packaged Pexo tool from GitHub..." -HeartbeatMessage "Installing packaged Pexo tool... still working" -FilePath "pipx" -ArgumentList @("install", "--force", $packageSource)
        try {
            & pipx ensurepath *> $null
        }
        catch {
        }
        Add-ToSessionPath -Entry (Get-PipxBinDirectory)
        $packagedModeLabel = "packaged GitHub tool via pipx"
        $uninstallCommand = "pipx uninstall pexo-agent"
    }

    if (-not (Get-Command pexo -ErrorAction SilentlyContinue)) {
        throw "Packaged install completed, but the 'pexo' command is not visible in this shell. Reopen the terminal or invoke the tool from the packaged tool bin directory directly."
    }

    $finalProfile = "mcp"
    if ($RequestedProfile -eq "full" -or $RequestedProfile -eq "vector") {
        Invoke-TrackedProcess -Percent 85 -StartMessage "Promoting packaged install to the $RequestedProfile runtime..." -HeartbeatMessage "Promoting packaged install... still working" -FilePath "pexo" -ArgumentList @("promote", $RequestedProfile)
        $finalProfile = $RequestedProfile
    }

    if ($HeadlessSetup) {
        $headlessArgs = @("headless-setup", "--preset", $Preset, "--name", $ProfileName)
        if (-not [string]::IsNullOrWhiteSpace($BackupPath)) {
            $headlessArgs += @("--backup-path", $BackupPath)
        }
        Invoke-TrackedProcess -Percent 95 -StartMessage "Applying headless profile setup..." -HeartbeatMessage "Applying headless profile setup... still working" -FilePath "pexo" -ArgumentList $headlessArgs
    }

    $stateRoot = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".pexo"))
    $databasePath = Join-Path $stateRoot "pexo.db"
    $memoryStorePath = Join-Path $stateRoot "chroma_db"
    $artifactPath = Join-Path $stateRoot "artifacts"
    $toolsPath = Join-Path $stateRoot "dynamic_tools"
    $effectiveBackupPath = if ($HeadlessSetup -and -not [string]::IsNullOrWhiteSpace($BackupPath)) { $BackupPath } elseif ($HeadlessSetup) { "not set" } else { "not configured during install" }
    $profileSummary = if ($HeadlessSetup) { $ProfileName } else { "not initialized" }

    Show-InstallProgress -Percent 100 -Status "Installation complete"
    Write-Progress -Activity "Installing Pexo" -Completed -Status "Installation complete"
    Write-Host "=================================================="
    Write-Host "Pexo installed successfully!"
    Write-Host "Install mode: $packagedModeLabel"
    Write-Host "Package source: $packageSource"
    if (-not [string]::IsNullOrWhiteSpace($ProtectedCheckoutPath)) {
        Write-Host "Protected checkout left untouched: $ProtectedCheckoutPath"
    }
    Write-Host "State directory: $stateRoot"
    Write-Host "Dependency profile ready now: $finalProfile"
    Write-Host "Profile initialized: $profileSummary"
    Write-Host "Backup path: $effectiveBackupPath"
    Write-Host "Local database path: $databasePath"
    Write-Host "Local vector store path: $memoryStorePath"
    Write-Host "Local artifacts path: $artifactPath"
    Write-Host "Local dynamic tools path: $toolsPath"
    Write-Host "Works now in this shell via bare command:"
    Write-Host "  pexo --version"
    Write-Host "Ready-to-paste Windows MCP config:"
    Write-Host (Format-PackagedWindowsMcpSnippet)
    Write-Host "To connect supported AI clients automatically:"
    Write-Host "  pexo connect all --scope user"
    Write-Host "To uninstall the packaged tool later:"
    Write-Host "  $uninstallCommand"
    Write-Host "To remove local state as well:"
    Write-Host "  Remove-Item -Recurse -Force `"$stateRoot`""
    if ($HeadlessSetup) {
        Write-Host "Headless profile setup completed during install."
        Write-Host "Run 'pexo' later when the user wants the local dashboard for memory, agents, and configuration."
    }
    else {
        Write-Host "Preferred terminal-first setup path:"
        Write-Host "  pexo headless-setup --preset $Preset"
        Write-Host "Run 'pexo' later only when the user wants the local dashboard at http://127.0.0.1:9999."
    }
    Write-InstallSummaryJson -Summary @{
        status = "ok"
        install_mode = "packaged"
        packaged_tool = $packagedTool
        package_source = $packageSource
        install_directory = $null
        state_directory = $stateRoot
        active_profile = $finalProfile
        profile_initialized = $profileSummary
        backup_path = $effectiveBackupPath
        launcher_command = "pexo"
        mcp_command = "pexo-mcp"
        uninstall_command = $uninstallCommand
        next = @("pexo connect all --scope user", "pexo doctor", "pexo")
    }
    Write-Host "=================================================="
    return
}

Show-InstallProgress -Percent 5 -Status "Validating install target at $PexoDir"
if (-not [string]::IsNullOrWhiteSpace($ProtectedCheckoutPath)) {
    Write-Host "[SAFE] Existing checkout protection is enabled. Leaving '$ProtectedCheckoutPath' untouched and installing to '$PexoDir' instead. Pass -AllowRepoInstall only when you intentionally want a repo-local install."
}
if ($UsingExistingCheckout) {
    if (-not (Test-Path (Join-Path $PexoDir ".git"))) {
        throw "The checkout at '$PexoDir' is missing a .git directory. Use -InstallDir to clone a new copy or point -RepoPath at an existing checkout."
    }

    if ($SkipUpdate -or $Offline) {
        Show-InstallProgress -Percent 20 -Status "Using existing checkout. Skipping repository update."
        $CloneMethodSummary = "existing checkout ($PexoDir), update skipped"
    }
    elseif (Test-GitDetachedHead -RepositoryPath $PexoDir) {
        Show-InstallProgress -Percent 20 -Status "Existing checkout is pinned to a detached git HEAD. Skipping repository update."
        $CloneMethodSummary = "existing checkout ($PexoDir), update skipped (detached HEAD)"
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
    elseif (Test-GitDetachedHead -RepositoryPath $PexoDir) {
        Show-InstallProgress -Percent 20 -Status "Existing installation is pinned to a detached git HEAD. Skipping repository update."
        $CloneMethodSummary = "existing installation at $PexoDir, update skipped (detached HEAD)"
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
if (-not [string]::IsNullOrWhiteSpace($currentProfile) -and (Get-DependencyRank -Profile $currentProfile) -eq 0) {
    Show-InstallProgress -Percent 68 -Status "Dependency marker '$currentProfile' is invalid. Reinstalling runtime dependencies."
    Clear-CurrentDependencyProfile -ResolvedInstallDir $PexoDir
    $currentProfile = ""
}
elseif (-not [string]::IsNullOrWhiteSpace($currentProfile) -and -not (Test-DependencyProfileReady -PythonPath $venvPythonPath -Profile $currentProfile)) {
    Show-InstallProgress -Percent 68 -Status "Dependency marker '$currentProfile' is stale. Reinstalling runtime dependencies."
    Clear-CurrentDependencyProfile -ResolvedInstallDir $PexoDir
    $currentProfile = ""
}
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

Invoke-TrackedProcess -Percent 97 -StartMessage "Priming local runtime..." -HeartbeatMessage "Priming local runtime... still working" -FilePath $launcherPath -ArgumentList @("warmup", "--quiet") -WorkingDirectory $PexoDir

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
if (-not [string]::IsNullOrWhiteSpace($ProtectedCheckoutPath)) {
    Write-Host "Protected checkout left untouched: $ProtectedCheckoutPath"
}
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
Write-Host "To connect supported AI clients automatically:"
Write-Host "  & `"$launcherPath`" connect all --scope user"
if ($HeadlessSetup) {
    Write-Host "Headless profile setup completed during install."
    Write-Host "Run 'pexo' later when the user wants the local dashboard for memory, agents, and configuration."
}
else {
    Write-Host "Run 'pexo' later only when the user wants the local dashboard at http://127.0.0.1:9999."
}
Write-InstallSummaryJson -Summary @{
    status = "ok"
    install_mode = "checkout"
    packaged_tool = $null
    package_source = $null
    install_directory = $PexoDir
    state_directory = $PexoDir
    active_profile = $finalProfile
    profile_initialized = $profileSummary
    backup_path = $effectiveBackupPath
    launcher_command = $launcherPath
    mcp_command = "$launcherPath --mcp"
    uninstall_command = "pexo uninstall"
    next = @("& `"$launcherPath`" connect all --scope user", "& `"$launcherPath`" --version", "& `"$launcherPath`" doctor")
}
Write-Host "=================================================="
