param(
    [string]$Preset = "efficient_operator",
    [string]$ProfileName = "default_user",
    [string]$BackupPath = "",
    [string]$Repository = "ParadoxGods/pexo-agent",
    [string]$Ref = "v1.1.0",
    [string]$InstallDir = "",
    [string]$RepoPath = "",
    [switch]$UseCurrentCheckout,
    [switch]$AllowRepoInstall,
    [ValidateSet("auto", "core", "mcp", "full", "vector")]
    [string]$InstallProfile = "auto",
    [ValidateSet("all", "codex", "claude", "gemini", "none")]
    [string]$ConnectClients = "all",
    [switch]$SkipUpdate,
    [switch]$Offline
)

$ErrorActionPreference = "Stop"
$scriptRoot = [System.IO.Path]::GetFullPath((Split-Path -Path $MyInvocation.MyCommand.Path -Parent))

function Write-BootstrapProgress {
    param(
        [int]$Percent,
        [string]$Status
    )

    Write-Host ("[{0,3}%] {1}" -f $Percent, $Status)
}

function Test-CommandAvailable {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
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

function Get-PackagedInstallTool {
    if (Test-CommandAvailable "uv") {
        return "uv"
    }
    if (Test-CommandAvailable "pipx") {
        return "pipx"
    }
    return ""
}

function Get-UvBinDirectory {
    if (-not (Test-CommandAvailable "uv")) {
        return ""
    }

    try {
        $binDir = [string](& uv tool dir --bin 2>$null | Out-String)
        if ($null -ne $binDir) {
            $binDir = $binDir.Trim()
        }
        if (-not [string]::IsNullOrWhiteSpace($binDir)) {
            return $binDir
        }
    }
    catch {
    }

    return ""
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

    $entries = @()
    if (-not [string]::IsNullOrWhiteSpace($env:Path)) {
        $entries = $env:Path.Split(";") | Where-Object { $_ }
    }
    if ($entries -notcontains $Entry) {
        $env:Path = (($entries + $Entry) -join ";")
    }
}

function Resolve-PackageSource {
    param(
        [string]$Repository,
        [string]$Ref
    )

    if ($Repository -match "^git\+") {
        $source = $Repository
    }
    elseif ($Repository -match "^https?://") {
        $source = "git+$Repository"
    }
    elseif ($Repository -match "^git@") {
        $source = $Repository
    }
    elseif ($Repository -match "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$") {
        $source = "git+https://github.com/$Repository.git"
    }
    else {
        throw "Unsupported repository source '$Repository'."
    }

    if (-not [string]::IsNullOrWhiteSpace($Ref) -and $source -notmatch "@[^/]+$") {
        $source = "$source@$Ref"
    }
    return $source
}

function Resolve-CloneSource {
    param([string]$Repository)

    if ($Repository -match "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$") {
        return "https://github.com/$Repository.git"
    }
    if ($Repository -match "^git\+") {
        return $Repository.Substring(4)
    }
    if ($Repository -match "^(https?|git@)") {
        return $Repository
    }

    throw "Unsupported repository source '$Repository'."
}

function Invoke-External {
    param(
        [int]$Percent,
        [string]$Message,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory = ""
    )

    Write-BootstrapProgress -Percent $Percent -Status $Message

    $previousLocation = $null
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $previousLocation = Get-Location
        Set-Location $WorkingDirectory
    }

    try {
        & $FilePath @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
        }
    }
    finally {
        if ($null -ne $previousLocation) {
            Set-Location $previousLocation
        }
    }
}

function Write-InstallSummaryJson {
    param([hashtable]$Summary)

    $json = ($Summary | ConvertTo-Json -Compress -Depth 6)
    Write-Host "PEXO_INSTALL_SUMMARY_JSON=$json"
}

function Invoke-LocalInstaller {
    param([string]$InstallerPath)

    $installArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $InstallerPath,
        "-HeadlessSetup",
        "-Preset", $Preset,
        "-ProfileName", $ProfileName,
        "-Repository", $Repository,
        "-InstallProfile", $InstallProfile
    )

    if (-not [string]::IsNullOrWhiteSpace($BackupPath)) {
        $installArgs += @("-BackupPath", $BackupPath)
    }
    if (-not [string]::IsNullOrWhiteSpace($InstallDir)) {
        $installArgs += @("-InstallDir", $InstallDir)
    }
    if (-not [string]::IsNullOrWhiteSpace($RepoPath)) {
        $installArgs += @("-RepoPath", $RepoPath)
    }
    if ($UseCurrentCheckout) {
        $installArgs += "-UseCurrentCheckout"
    }
    if ($AllowRepoInstall) {
        $installArgs += "-AllowRepoInstall"
    }
    if ($SkipUpdate) {
        $installArgs += "-SkipUpdate"
    }
    if ($Offline) {
        $installArgs += "-Offline"
    }

    Invoke-External -Percent 15 -Message "Running local Pexo installer" -FilePath "powershell" -ArgumentList $installArgs
}

function Invoke-DoctorCommand {
    param(
        [int]$Percent,
        [string]$CommandPath,
        [string]$WorkingDirectory = ""
    )

    Invoke-External -Percent $Percent -Message "Running Pexo doctor" -FilePath $CommandPath -ArgumentList @("doctor") -WorkingDirectory $WorkingDirectory
}

function Invoke-ConnectCommand {
    param(
        [int]$Percent,
        [string]$CommandPath,
        [string]$ClientTarget,
        [string]$WorkingDirectory = ""
    )

    if ($ClientTarget -eq "none") {
        return
    }

    Invoke-External -Percent $Percent -Message "Connecting AI clients to Pexo MCP" -FilePath $CommandPath -ArgumentList @("connect", $ClientTarget, "--scope", "user") -WorkingDirectory $WorkingDirectory
}

$localInstaller = Join-Path $scriptRoot "install.ps1"
$localAppDir = Join-Path $scriptRoot "app"
if ((Test-Path $localInstaller) -and (Test-Path $localAppDir)) {
    Invoke-LocalInstaller -InstallerPath $localInstaller
    $localLauncher = Join-Path $scriptRoot "pexo.bat"
    if (Test-Path $localLauncher) {
        Invoke-DoctorCommand -Percent 92 -CommandPath $localLauncher
        Invoke-ConnectCommand -Percent 97 -CommandPath $localLauncher -ClientTarget $ConnectClients
    }
    Write-BootstrapProgress -Percent 100 -Status "Bootstrap complete"
    exit 0
}

if ($UseCurrentCheckout -or -not [string]::IsNullOrWhiteSpace($RepoPath)) {
    throw "Standalone bootstrap does not support repo-local install. Clone the repository first, then run the local bootstrap or install wrapper from that checkout."
}

$packagedTool = Get-PackagedInstallTool
$requestedProfile = if ($InstallProfile -eq "auto") { "full" } else { $InstallProfile }
$stateRoot = Join-Path $env:USERPROFILE ".pexo"
$packageSource = Resolve-PackageSource -Repository $Repository -Ref $Ref

if (-not [string]::IsNullOrWhiteSpace($packagedTool)) {
    Write-BootstrapProgress -Percent 5 -Status "Using packaged GitHub install via $packagedTool"
    if ($packagedTool -eq "uv") {
        Invoke-External -Percent 20 -Message "Installing packaged Pexo tool" -FilePath "uv" -ArgumentList @("tool", "install", "--reinstall", $packageSource)
        Invoke-External -Percent 35 -Message "Updating shell integration" -FilePath "uv" -ArgumentList @("tool", "update-shell")
        Add-ToSessionPath -Entry (Get-UvBinDirectory)
    }
    else {
        Invoke-External -Percent 20 -Message "Installing packaged Pexo tool" -FilePath "pipx" -ArgumentList @("install", "--force", $packageSource)
        Invoke-External -Percent 35 -Message "Updating shell integration" -FilePath "pipx" -ArgumentList @("ensurepath")
        Add-ToSessionPath -Entry (Get-PipxBinDirectory)
    }

    if (-not (Get-Command pexo -ErrorAction SilentlyContinue)) {
        throw "Packaged install completed, but the 'pexo' command is not visible in this shell."
    }

    Invoke-External -Percent 60 -Message "Promoting runtime to full" -FilePath "pexo" -ArgumentList @("promote", "full")

    $headlessArgs = @("headless-setup", "--preset", $Preset, "--name", $ProfileName)
    if (-not [string]::IsNullOrWhiteSpace($BackupPath)) {
        $headlessArgs += @("--backup-path", $BackupPath)
    }
    Invoke-External -Percent 80 -Message "Applying headless setup" -FilePath "pexo" -ArgumentList $headlessArgs
    Invoke-DoctorCommand -Percent 92 -CommandPath "pexo"
    Invoke-ConnectCommand -Percent 97 -CommandPath "pexo" -ClientTarget $ConnectClients
    Write-BootstrapProgress -Percent 100 -Status "Bootstrap complete"
    Write-InstallSummaryJson -Summary @{
        status = "success"
        install_mode = "bootstrap_packaged"
        packaged_tool = $packagedTool
        package_source = $packageSource
        install_directory = "managed by $packagedTool"
        state_directory = $stateRoot
        active_profile = if ($requestedProfile -eq "vector") { "vector" } else { "full" }
        profile_initialized = $ProfileName
        backup_path = if ([string]::IsNullOrWhiteSpace($BackupPath)) { "not set" } else { $BackupPath }
        connected_clients = $ConnectClients
        launcher_command = "pexo"
        mcp_command = "pexo-mcp"
        uninstall_command = if ($packagedTool -eq "uv") { "uv tool uninstall pexo-agent" } else { "pipx uninstall pexo-agent" }
        next = @("pexo connect all --scope user", "pexo", "pexo --mcp")
    }
    exit 0
}

$targetDir = if (-not [string]::IsNullOrWhiteSpace($InstallDir)) { Resolve-FullPath -PathValue $InstallDir } else { Get-DefaultInstallDirectory }
$cloneSource = Resolve-CloneSource -Repository $Repository

if (-not (Test-CommandAvailable "git")) {
    throw "Git is required for the checkout fallback path."
}
if (-not (Test-CommandAvailable "python")) {
    throw "Python is required for the checkout fallback path."
}

if (-not (Test-Path $targetDir)) {
    Invoke-External -Percent 20 -Message "Cloning Pexo checkout fallback" -FilePath "git" -ArgumentList @("clone", $cloneSource, $targetDir)
}

$targetInstaller = Join-Path $targetDir "install.ps1"
if (-not (Test-Path $targetInstaller)) {
    throw "Checkout fallback directory '$targetDir' does not contain install.ps1."
}

if (-not [string]::IsNullOrWhiteSpace($Ref)) {
    Invoke-External -Percent 35 -Message "Checking out $Ref" -FilePath "git" -ArgumentList @("-C", $targetDir, "fetch", "--tags", "--quiet")
    Invoke-External -Percent 45 -Message "Pinning checkout to $Ref" -FilePath "git" -ArgumentList @("-C", $targetDir, "checkout", $Ref)
}

$previousInstallDir = $InstallDir
$previousRepoPath = $RepoPath
$previousAllowRepoInstall = $AllowRepoInstall
$InstallDir = ""
$RepoPath = $targetDir
$AllowRepoInstall = $true
$SkipUpdate = $true
Invoke-LocalInstaller -InstallerPath $targetInstaller
$InstallDir = $previousInstallDir
$RepoPath = $previousRepoPath
$AllowRepoInstall = $previousAllowRepoInstall

$launcherPath = Join-Path $targetDir "pexo.bat"
Invoke-DoctorCommand -Percent 92 -CommandPath $launcherPath
Invoke-ConnectCommand -Percent 97 -CommandPath $launcherPath -ClientTarget $ConnectClients
Write-BootstrapProgress -Percent 100 -Status "Bootstrap complete"
