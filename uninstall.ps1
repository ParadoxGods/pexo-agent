$PexoDir = [System.IO.Path]::GetFullPath((Split-Path -Path $MyInvocation.MyCommand.Path -Parent))
Write-Host "=================================================="
Write-Host "Uninstalling Pexo (Primary EXecution Operator)..."
Write-Host "=================================================="

# 1. Terminate running processes
Write-Host "Terminating Pexo processes..."
Get-Process | Where-Object { $_.Path -like "$PexoDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
taskkill /F /IM python.exe /T /FI "WINDOWTITLE eq Pexo*" 2>$null

# 2. Remove from PATH
Write-Host "Removing Pexo from System PATH..."
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$CleanPath = @()
if (-not [string]::IsNullOrWhiteSpace($UserPath)) {
    $CleanPath = $UserPath.Split(';') | Where-Object { $_ -ne $PexoDir -and $_ -ne "" }
}
$NewPath = $CleanPath -join ';'
[Environment]::SetEnvironmentVariable("Path", $NewPath, "User")

# 3. Delete files
if (Test-Path $PexoDir) {
    Write-Host "Deleting Pexo files at $PexoDir..."
    # Attempt rename if delete fails due to locks, to be handled on next reboot
    try {
        Remove-Item -Recurse -Force $PexoDir -ErrorAction Stop
    } catch {
        $DeletedPath = "$PexoDir`_deleted"
        if (Test-Path $DeletedPath) { Remove-Item -Recurse -Force $DeletedPath -ErrorAction SilentlyContinue }
        Rename-Item -Path $PexoDir -NewName ".pexo_deleted" -ErrorAction SilentlyContinue
        Write-Host "Note: Some files were locked. They have been marked for deletion and will be cleared fully after a restart."
    }
}

Write-Host "=================================================="
Write-Host "Pexo has been successfully uninstalled."
Write-Host "=================================================="
