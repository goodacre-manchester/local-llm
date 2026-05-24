# Extract PDFs under data/ via NVIDIA Nemotron Parse v1.2 (in-process HF
# transformers; bypasses vLLM serving). Writes sidecars in the same shape
# as extract-pdfs.ps1 but with backend="nemotron-parse-v1.2".
#
# Used by the Phase 3 RAG benchmark plan. Free the GPU first if other
# services are using it:
#   wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose stop sd-webui nemo-parse"
# Parse loads ~3.75 GB on GPU; combined with HF/torch overhead ~5-6 GB total.
# Long-running -- ~30-60 min/page on a 2000-page PDF.
#
# First run bootstraps a dedicated .venv-nemo (separate from extract.py's
# lightweight .venv) and installs torch 2.x+cu128 (Blackwell sm_120 support)
# + transformers + open_clip_torch + albumentations. Subsequent runs are fast.
#
# Usage:
#   .\scripts\extract-nemo.ps1                       # all collections
#   .\scripts\extract-nemo.ps1 ieee-nemo-parse-tas   # one collection
#   .\scripts\extract-nemo.ps1 ieee-nemo-parse-tas -Force

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

# Heavy ML deps go in a SEPARATE venv so extract.py's lightweight venv stays
# small. First run takes ~10 min (downloads torch + transformers + Parse model
# weights from HF, total ~6 GB); subsequent runs are fast.
$bash = @"
set -e
cd '$wslRepoRoot/scripts/extract'
if [ ! -d .venv-nemo ]; then
  if ! python3 -m venv .venv-nemo 2>/dev/null; then
    echo 'python venv unavailable. In WSL run:  sudo apt-get install -y python3-venv' >&2
    exit 1
  fi
fi
. .venv-nemo/bin/activate
pip install -q --upgrade pip
# torch + torchvision with Blackwell cu128 support (see sd-webui-entrypoint.sh
# for the full rationale -- RTX 50-series is sm_120 and standard PyPI torch
# wheels stop at sm_90).
pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cu128
pip install -q -r requirements-nemo.txt
# Cache Parse model + processor in the same location vLLM used (so they're
# shared across the two backends if/when nemo-parse vLLM gets revived).
export HF_HOME='$wslRepoRoot/storage/nemo-parse/hf-cache'
python extract-nemo.py '$wslRepoRoot/data'$collArg $forceFlag
"@

& "C:\Windows\System32\wsl.exe" -e bash -lc $bash
