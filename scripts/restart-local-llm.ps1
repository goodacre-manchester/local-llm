$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$driveLetter = ($repoRoot -replace '^([A-Za-z]):.*', '$1').ToLower()
$wslRepoRoot = ($repoRoot -replace '^[A-Za-z]:', "/mnt/$driveLetter").Replace('\', '/')

$service = $args[0]   # optional: name of a single service to restart

# Use sudo only when passwordless sudo is available; otherwise fall back to
# plain docker, so a non-interactive `wsl -e bash -lc` never hangs on a
# password prompt.
$pickDc = "if sudo -n true 2>/dev/null; then DC='sudo docker compose'; else DC='docker compose'; fi"

if ($service) {
    $wslCmd = "cd '$wslRepoRoot'; $pickDc; `$DC restart $service; `$DC ps"
} else {
    $wslCmd = "cd '$wslRepoRoot'; $pickDc; `$DC restart chroma rag-server open-webui; `$DC ps"
}

& "C:\Windows\System32\wsl.exe" -e bash -lc $wslCmd
