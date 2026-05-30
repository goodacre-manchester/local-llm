# Extract source trees under data/ into JSON sidecars
# (data/<collection>/.rag-cache/<encoded-path>.json) that the RAG server
# ingests for file:line-cited retrieval. Two modes per collection,
# chosen by the extractor:
#
#   - Link mode: data/<collection>/.git-source.yaml exists. The
#     extractor clones (shallow + filtered) into
#     storage/code-cache/<collection>/ at the configured ref and walks
#     that. github_url metadata is built per chunk for clickable
#     citations. Repo refresh = re-run this script (does git fetch +
#     reset --hard).
#
#   - In-place mode: no yaml; the extractor walks data/<collection>/
#     directly (e.g. a manual checkout or a small vendored snapshot).
#
# Uses the shared scripts/extract/.venv (same venv as extract-pdfs.ps1).
# Tree-sitter + PyYAML are CPU-only and small (~50 MB additional).
#
# Usage:
#   .\scripts\extract-code.ps1                 # all code collections
#   .\scripts\extract-code.ps1 nginx           # one collection
#   .\scripts\extract-code.ps1 nginx -Force    # re-extract every file

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

# Shared venv: install lightweight base deps + code-extractor deps
# (tree_sitter, tree_sitter_language_pack, PyYAML). Both requirements
# files are idempotent — pip skips already-satisfied installs fast.
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
pip install -q -r requirements-code.txt
python extract-code.py '$wslRepoRoot/data'$collArg $forceFlag
"@

& "C:\Windows\System32\wsl.exe" -e bash -lc $bash
