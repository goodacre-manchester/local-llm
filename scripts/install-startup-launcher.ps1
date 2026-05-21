$ErrorActionPreference = 'Stop'

$startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$launcherPath = Join-Path $startupDir 'start-local-llm.cmd'
$scriptPath = Join-Path $PSScriptRoot 'start-local-llm.ps1'

if (-not (Test-Path $startupDir)) {
  New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
}

if (-not (Test-Path $scriptPath)) {
  throw "Startup script not found at $scriptPath"
}

$content = "@echo off`r`n" +
           "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`"`r`n"

Set-Content -Path $launcherPath -Value $content -Encoding ASCII -Force

Write-Output "Startup launcher installed at: $launcherPath"
