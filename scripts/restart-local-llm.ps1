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
    # Restart everything in compose. Includes reranker and sd-webui so they
    # survive a manual restart (otherwise the prior `chroma rag-server open-webui`
    # whitelist would leave them in their previous state — fine on subsequent
    # boots but inconsistent with user intent of "restart all").
    $wslCmd = "cd '$wslRepoRoot'; $pickDc; `$DC restart; `$DC ps"
}

& "C:\Windows\System32\wsl.exe" -e bash -lc $wslCmd
