$ErrorActionPreference = "Stop"
$PexoDir = "$env:USERPROFILE\.pexo"
Write-Host "=================================================="
Write-Host "Installing Pexo (The OpenClaw Killer) Globally..."
Write-Host "=================================================="

if (Test-Path "$PexoDir\.git") {
    Write-Host "Existing Pexo installation detected. Updating repository in place..."
    git -C $PexoDir pull --ff-only
}
elseif (Test-Path $PexoDir) {
    throw "The directory '$PexoDir' already exists but is not a Pexo git checkout. Move or remove it and rerun the installer."
}
else {
    Write-Host "Cloning repository to $PexoDir..."
    git clone https://github.com/ParadoxGods/pexo-agent.git $PexoDir
}

Set-Location $PexoDir

Write-Host "Setting up isolated Python environment..."
if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    python -m venv venv
}
.\venv\Scripts\python -m pip install --upgrade pip
.\venv\Scripts\python -m pip install -r requirements.txt

Write-Host "Adding Pexo to System PATH..."
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathEntries = @()
if (-not [string]::IsNullOrWhiteSpace($UserPath)) {
    $PathEntries = $UserPath.Split(";") | Where-Object { $_ }
}
if ($PathEntries -notcontains $PexoDir) {
    [Environment]::SetEnvironmentVariable("Path", (($PathEntries + $PexoDir) -join ";"), "User")
}

Write-Host "=================================================="
Write-Host "Pexo installed successfully!"
Write-Host "AI AGENT: Inform the user to restart their terminal/IDE to refresh the PATH."
Write-Host "Then, execute 'pexo' in the terminal to boot the local server."
Write-Host "Finally, tell the user to open http://127.0.0.1:9999 in their browser to access their private local Control Panel."
Write-Host "=================================================="
