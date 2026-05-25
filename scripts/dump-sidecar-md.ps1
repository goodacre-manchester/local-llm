# Render data/<collection>/.rag-cache/*.json sidecars as readable Markdown
# under data/<collection>/.rag-md/, for VS Code preview (Ctrl+Shift+V).
#
# Pure stdlib; no GPU, no torch — safe to run while extract-nemo.ps1 is
# extracting other PDFs. Mtime-idempotent: regenerates only when the .md
# is missing or older than the .json.
#
# Usage:
#   .\scripts\dump-sidecar-md.ps1                    # every collection
#   .\scripts\dump-sidecar-md.ps1 ieee               # one collection
#   .\scripts\dump-sidecar-md.ps1 ieee -Force        # rebuild even if up to date

param(
    [string]$Collection = "",
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$repoRoot    = Split-Path -Parent $PSScriptRoot
$driveLetter = ($repoRoot -replace '^([A-Za-z]):.*', '$1').ToLower()
$wslRepoRoot = ($repoRoot -replace '^[A-Za-z]:', "/mnt/$driveLetter").Replace('\', '/')

$collArg   = if ($Collection) { " '$Collection'" } else { "" }
$forceFlag = if ($Force)      { " --force"        } else { "" }

# Pure-stdlib python3 — use the system interpreter (no venv needed).
$bash = "python3 '$wslRepoRoot/scripts/extract/dump-sidecar-md.py' '$wslRepoRoot/data'$collArg$forceFlag"

& "C:\Windows\System32\wsl.exe" -e bash -lc $bash
