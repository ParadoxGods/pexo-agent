param(
    [string]$Preset = "efficient_operator",
    [string]$ProfileName = "default_user",
    [string]$BackupPath = "",
    [ValidateSet("all", "codex", "claude", "gemini", "none")]
    [string]$ConnectClients = "all",
    [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"
$BundleRoot = [System.IO.Path]::GetFullPath((Split-Path -Path $MyInvocation.MyCommand.Path -Parent))
$StateRoot = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".pexo"))
$InstallMetadataPath = Join-Path $StateRoot ".pexo-install.json"

function Write-Step {
    param([int]$Percent, [string]$Status)
    Write-Host ("[{0,3}%] {1}" -f $Percent, $Status)
}

function Test-CommandAvailable {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Resolve-PythonCommand {
    if (Test-CommandAvailable "py") { return @("py", "-3") }
    if (Test-CommandAvailable "python") { return @("python") }
    throw "Python 3.11 or newer is required."
}

function Invoke-Checked {
    param([int]$Percent, [string]$Status, [string]$FilePath, [string[]]$ArgumentList)
    Write-Step -Percent $Percent -Status $Status
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
}

function Add-ToSessionPath {
    param([string]$Entry)
    if ([string]::IsNullOrWhiteSpace($Entry)) { return }
    $entries = @()
    if (-not [string]::IsNullOrWhiteSpace($env:Path)) {
        $entries = $env:Path.Split(";") | Where-Object { $_ }
    }
    if ($entries -notcontains $Entry) {
        $env:Path = (($entries + $Entry) -join ";")
    }
}

function Add-ToUserPath {
    param([string]$Entry)
    if ([string]::IsNullOrWhiteSpace($Entry)) { return }
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @()
    if (-not [string]::IsNullOrWhiteSpace($current)) {
        $entries = $current.Split(";") | Where-Object { $_ }
    }
    if ($entries -notcontains $Entry) {
        [Environment]::SetEnvironmentVariable("Path", (($entries + $Entry) -join ";"), "User")
    }
}

function Get-PipxBinDirectory {
    try {
        $binDir = [string](& pipx environment --value PIPX_BIN_DIR 2>$null | Out-String)
        if ($null -ne $binDir) { $binDir = $binDir.Trim() }
        if (-not [string]::IsNullOrWhiteSpace($binDir)) { return $binDir }
    }
    catch {}
    return [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".local\bin"))
}

function Get-WheelPath {
    $wheel = Get-ChildItem -LiteralPath $BundleRoot -Filter "pexo_agent-*-py3-none-any.whl" | Sort-Object Name | Select-Object -First 1
    if ($null -eq $wheel) { throw "No wheel asset was found in $BundleRoot." }
    return $wheel.FullName
}

function Get-WheelVersion {
    param([string]$WheelPath)
    return [System.IO.Path]::GetFileName($WheelPath).Replace("pexo_agent-", "").Replace("-py3-none-any.whl", "")
}

function Test-WheelChecksum {
    param([string]$WheelPath)
    $checksumFile = Join-Path $BundleRoot "SHA256SUMS.txt"
    if (-not (Test-Path $checksumFile)) { throw "SHA256SUMS.txt is missing from the install bundle." }
    $wheelName = [System.IO.Path]::GetFileName($WheelPath)
    $expectedLine = Select-String -LiteralPath $checksumFile -Pattern ([regex]::Escape($wheelName)) | Select-Object -First 1
    if ($null -eq $expectedLine) { throw "The checksum file does not contain an entry for $wheelName." }
    $expectedHash = ($expectedLine.Line -split '\s+')[0].Trim().ToLowerInvariant()
    $actualHash = (Get-FileHash -LiteralPath $WheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) { throw "SHA256 mismatch for $wheelName." }
}

function Write-InstallMetadata {
    param([string]$Version, [string]$Method, [string]$CommandPath, [string]$McpCommand, [string]$UninstallCommand, [string]$UpdateCommand)
    New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
    $payload = @{
        version = $Version
        method = $Method
        release = "https://github.com/ParadoxGods/pexo-agent/releases/tag/v$Version"
        command_path = $CommandPath
        mcp_command = $McpCommand
        guidance = @{
            uninstall = $UninstallCommand
            update = $UpdateCommand
        }
    }
    $json = $payload | ConvertTo-Json -Depth 6
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($InstallMetadataPath, $json, $utf8NoBom)
}

function Write-Summary {
    param([string]$Version, [string]$Method, [string]$CommandPath, [string]$McpCommand)
    $summary = @{
        version = $Version
        install_method = $Method
        state_root = $StateRoot
        command = $CommandPath
        mcp_command = $McpCommand
        next = @("pexo doctor", "pexo connect all --scope user", "pexo")
    }
    $json = $summary | ConvertTo-Json -Compress -Depth 6
    Write-Host "PEXO_INSTALL_SUMMARY_JSON=$json"
}

$wheelPath = Get-WheelPath
$version = Get-WheelVersion -WheelPath $wheelPath
Test-WheelChecksum -WheelPath $wheelPath

$commandPath = ""
$mcpCommand = ""
$installMethod = ""
$uninstallGuidance = ""
$updateGuidance = "Download the latest Pexo release bundle and run install.cmd again."

if (Test-CommandAvailable "pipx") {
    $installMethod = "release_bundle_pipx"
    Invoke-Checked -Percent 20 -Status "Installing Pexo with pipx" -FilePath "pipx" -ArgumentList @("install", "--force", $wheelPath)
    & pipx ensurepath *> $null
    $pipxBin = Get-PipxBinDirectory
    Add-ToSessionPath -Entry $pipxBin
    Add-ToUserPath -Entry $pipxBin
    $commandPath = "pexo"
    $mcpCommand = [string]((Get-Command "pexo-mcp" -ErrorAction SilentlyContinue).Source)
    if ([string]::IsNullOrWhiteSpace($mcpCommand)) { $mcpCommand = "pexo-mcp" }
    $uninstallGuidance = "pipx uninstall pexo-agent; Remove-Item -Recurse -Force `"$StateRoot`""
}
else {
    $installMethod = "release_bundle_managed_venv"
    $pythonCommand = Resolve-PythonCommand
    $pythonExe = $pythonCommand[0]
    $pythonArgs = @()
    if ($pythonCommand.Length -gt 1) { $pythonArgs = $pythonCommand[1..($pythonCommand.Length - 1)] }
    $venvPath = Join-Path $StateRoot "venv"
    $venvPython = Join-Path $venvPath "Scripts\python.exe"
    $venvBin = Join-Path $venvPath "Scripts"
    $venvPexo = Join-Path $venvBin "pexo.exe"
    $venvMcp = Join-Path $venvBin "pexo-mcp.exe"
    New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
    Invoke-Checked -Percent 20 -Status "Creating isolated Python environment" -FilePath $pythonExe -ArgumentList ($pythonArgs + @("-m", "venv", $venvPath))
    Invoke-Checked -Percent 35 -Status "Ensuring pip is available" -FilePath $venvPython -ArgumentList @("-m", "ensurepip", "--upgrade")
    Invoke-Checked -Percent 50 -Status "Installing the Pexo wheel" -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall", $wheelPath)
    Add-ToSessionPath -Entry $venvBin
    Add-ToUserPath -Entry $venvBin
    $commandPath = $venvPexo
    $mcpCommand = $venvMcp
    $uninstallGuidance = "Remove-Item -Recurse -Force `"$venvPath`"; Remove-Item -Recurse -Force `"$StateRoot`""
}

Write-InstallMetadata -Version $version -Method $installMethod -CommandPath $commandPath -McpCommand $mcpCommand -UninstallCommand $uninstallGuidance -UpdateCommand $updateGuidance

$setupArgs = @("headless-setup", "--preset", $Preset, "--name", $ProfileName)
if (-not [string]::IsNullOrWhiteSpace($BackupPath)) { $setupArgs += @("--backup-path", $BackupPath) }
Invoke-Checked -Percent 72 -Status "Running headless setup" -FilePath $commandPath -ArgumentList $setupArgs

if ($ConnectClients -ne "none") {
    Invoke-Checked -Percent 88 -Status "Connecting supported AI clients" -FilePath $commandPath -ArgumentList @("connect", $ConnectClients, "--scope", "user")
}

if (-not $SkipDoctor) {
    Invoke-Checked -Percent 96 -Status "Running Pexo doctor" -FilePath $commandPath -ArgumentList @("doctor")
}

Write-Step -Percent 100 -Status "Pexo install completed"
Write-Summary -Version $version -Method $installMethod -CommandPath $commandPath -McpCommand $mcpCommand
