# Run any bash command inside the repo root in WSL, regardless of where the repo
# is cloned on this machine.
#
# Usage:  .\scripts\wsl-run.ps1 "sudo docker compose logs --tail=150 open-webui"

$ErrorActionPreference = 'Stop'

if ($args.Count -eq 0) {
    Write-Error "Usage: .\scripts\wsl-run.ps1 <bash-command>"
    exit 1
}

$repoRoot     = Split-Path -Parent $PSScriptRoot
$driveLetter  = ($repoRoot -replace '^([A-Za-z]):.*', '$1').ToLower()
$wslRepoRoot  = ($repoRoot -replace '^[A-Za-z]:', "/mnt/$driveLetter").Replace('\', '/')
$wslCmd       = "cd '$wslRepoRoot'; $($args -join ' ')"

& "C:\Windows\System32\wsl.exe" -e bash -lc $wslCmd
