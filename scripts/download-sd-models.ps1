# Download default SDXL checkpoints for the sd-webui container.
# Thin wrapper around scripts/download-sd-models.sh so Windows users have a
# one-shot PowerShell command consistent with the rest of the scripts/ folder.
#
# Usage:  .\scripts\download-sd-models.ps1
#
# Idempotent: re-runs only re-download files missing or incomplete.

$ErrorActionPreference = 'Stop'

& "$PSScriptRoot\wsl-run.ps1" "chmod +x scripts/download-sd-models.sh && ./scripts/download-sd-models.sh"
