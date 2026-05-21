# Extract every PDF under data/ into page-tagged JSON sidecars
# (data/<collection>/.rag-cache/<pdf>.json) that the RAG server ingests for
# table-aware, page-cited retrieval.
#
# Runs in WSL against the repo's data/ folder (the rag-server container mounts
# the same folder read-only and reads the sidecars). A dedicated venv at
# scripts/extract/.venv keeps system Python clean.
#
# Usage:
#   .\scripts\extract-pdfs.ps1                 # all collections
#   .\scripts\extract-pdfs.ps1 amd             # one collection
#   .\scripts\extract-pdfs.ps1 amd -Force      # re-extract even if unchanged

param(
    [string]$Collection = "",
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$repoRoot    = Split-Path -Parent $PSScriptRoot
$driveLetter = ($repoRoot -replace '^([A-Za-z]):.*', '$1').ToLower()
$wslRepoRoot = ($repoRoot -replace '^[A-Za-z]:', "/mnt/$driveLetter").Replace('\', '/')

$forceFlag = if ($Force) { "--force" } else { "" }
$collArg   = if ($Collection) { " '$Collection'" } else { "" }

# Bootstrap a venv, install the lightweight backends, then extract.
$bash = @"
set -e
cd '$wslRepoRoot/scripts/extract'
if [ ! -d .venv ]; then
  if ! python3 -m venv .venv 2>/dev/null; then
    echo 'python venv unavailable. In WSL run:  sudo apt-get install -y python3-venv' >&2
    exit 1
  fi
fi
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
python extract.py '$wslRepoRoot/data'$collArg $forceFlag
"@

& "C:\Windows\System32\wsl.exe" -e bash -lc $bash
