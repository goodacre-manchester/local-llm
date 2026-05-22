#!/usr/bin/env bash
# Download default SDXL checkpoints into the sd-webui container's models dir.
# Idempotent: skips files that already exist at expected (or larger) size.
#
# Invoke from the repo root (or via scripts/download-sd-models.ps1 on Windows).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Matches the host-side path mounted to /workspace in docker-compose.yml.
MODELS_DIR="$REPO_ROOT/storage/sd-webui/storage/stable_diffusion/models/ckpt"
mkdir -p "$MODELS_DIR"

# Each entry: NAME|URL|MIN_SIZE_BYTES (sanity check for partial downloads).
MODELS=(
  # Juggernaut XL v9 — RunDiffusion's flagship SDXL fine-tune (~6.6 GB).
  # Public on HuggingFace, no token required.
  "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors|https://huggingface.co/RunDiffusion/Juggernaut-XL-v9/resolve/main/Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors|6000000000"
)

for entry in "${MODELS[@]}"; do
  IFS='|' read -r name url min_size <<< "$entry"
  dest="$MODELS_DIR/$name"
  if [ -f "$dest" ]; then
    actual="$(stat -c %s "$dest" 2>/dev/null || echo 0)"
    if [ "$actual" -ge "$min_size" ]; then
      echo "[skip] $name already present ($(du -h "$dest" | cut -f1))"
      continue
    fi
    echo "[redo] $name exists but is $actual bytes (< $min_size) — re-downloading"
    rm -f "$dest"
  fi
  echo "[get]  $name"
  echo "       <- $url"
  echo "       -> $dest"
  # -L follows HF's redirect to the CDN; --fail-with-body surfaces 401/404 cleanly.
  curl -fL --progress-bar -o "$dest" "$url"
  echo "[done] $(du -h "$dest" | cut -f1)"
done

echo
echo "Models on disk:"
ls -lh "$MODELS_DIR"
echo
echo "Tell A1111 to rescan: open http://localhost:7860 → Settings → Reload UI"
echo "(or just restart the container: docker compose restart sd-webui)"
