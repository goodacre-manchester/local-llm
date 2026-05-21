$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$driveLetter = ($repoRoot -replace '^([A-Za-z]):.*', '$1').ToLower()
$wslRepoRoot = ($repoRoot -replace '^[A-Za-z]:', "/mnt/$driveLetter").Replace('\', '/')

# Use sudo only when passwordless sudo is available; otherwise fall back to
# plain docker. A bare `sudo` here would block on a password prompt because
# `wsl -e bash -lc` is non-interactive.
$wslCmd = "cd '$wslRepoRoot'; if sudo -n true 2>/dev/null; then DC='sudo docker compose'; else DC='docker compose'; fi; `$DC down; echo 'All local LLM containers stopped.'"

& "C:\Windows\System32\wsl.exe" -e bash -lc $wslCmd
