$ErrorActionPreference = 'Stop'

$taskName = 'LocalLLM-Autostart'
$scriptPath = Join-Path $PSScriptRoot 'start-local-llm.ps1'

if (-not (Test-Path $scriptPath)) {
  throw "Startup script not found at $scriptPath"
}

$taskCommand = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""

schtasks /Create /F /SC ONLOGON /TN $taskName /TR $taskCommand /RL LIMITED | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "schtasks failed with exit code $LASTEXITCODE"
}

Write-Output "Scheduled task '$taskName' has been registered with ONLOGON trigger."
