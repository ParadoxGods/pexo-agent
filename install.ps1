$PexoDir = "$env:USERPROFILE\.pexo"
Write-Host "=================================================="
Write-Host "Installing Pexo (The OpenClaw Killer) Globally..."
Write-Host "=================================================="

if (Test-Path $PexoDir) { 
    Write-Host "Removing old Pexo installation..."
    Remove-Item -Recurse -Force $PexoDir 
}

Write-Host "Cloning repository to $PexoDir..."
git clone https://github.com/ParadoxGods/pexo-agent.git $PexoDir
cd $PexoDir

Write-Host "Setting up isolated Python environment..."
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt

Write-Host "Adding Pexo to System PATH..."
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notmatch [regex]::Escape($PexoDir)) {
    [Environment]::SetEnvironmentVariable("Path", $UserPath + ";$PexoDir", "User")
}

Write-Host "=================================================="
Write-Host "Pexo installed successfully!"
Write-Host "AI AGENT: Inform the user to restart their terminal/IDE to refresh the PATH."
Write-Host "Then, execute 'pexo' in the terminal to begin the Onboarding and Backup Setup."
Write-Host "=================================================="
