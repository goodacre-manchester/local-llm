$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$driveLetter = ($repoRoot -replace '^([A-Za-z]):.*', '$1').ToLower()
$wslRepoRoot = ($repoRoot -replace '^[A-Za-z]:', "/mnt/$driveLetter").Replace('\', '/')
$wslCmd = "cd '$wslRepoRoot'; chmod +x scripts/ensure-services.sh; ./scripts/ensure-services.sh"

# Start services, wait for all health checks, then block indefinitely.
# ensure-services.sh ends with 'exec sleep infinity', which keeps this wsl.exe
# session — and therefore WSL itself — alive so containers remain reachable.
& "C:\Windows\System32\wsl.exe" -e bash -lc $wslCmd
