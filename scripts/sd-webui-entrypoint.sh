#!/bin/bash
# sd-webui container entrypoint wrapper.
#
# Why this exists: ghcr.io/ai-dock/stable-diffusion-webui:latest-cuda ships
# CUDA 12.1 + torch 2.4.0+cu121, whose prebuilt kernels stop at sm_90. RTX
# 50-series cards (Blackwell, sm_120) get the runtime "no kernel image is
# available for execution on the device" error and SDXL generation fails.
#
# Evidence (captured 2026-05-21 on RTX 5070 Ti):
#   - GPU compute capability: sm_120
#   - Default torch arch list:    [sm_50..sm_90]   ← missing sm_120
#   - torch 2.11.0+cu128 archs:   [sm_75..sm_90, sm_100, sm_120]  ← works
#
# So on EVERY container creation, before A1111 starts, we upgrade torch +
# torchvision to a cu128 wheel that includes Blackwell kernels, then hand
# off to ai-dock's normal init.sh. The install is idempotent — pip exits
# fast when the target version is already present — and a host-mounted pip
# cache (see docker-compose.yml `./storage/sd-webui/pip-cache`) makes the
# 2nd+ recreate take seconds instead of re-downloading ~2.5 GB.
#
# Remove this wrapper once ai-dock ships a `:latest-cuda-12.8+` (or later)
# tag with a Blackwell-capable PyTorch — at that point switch the image tag
# in docker-compose.yml and delete this entrypoint override.

set -e

WEBUI_VENV_PIP=/opt/environments/python/webui/bin/pip
# Lower bound — anything >= this version has cu128 + sm_120 kernels.
TARGET_TORCH=2.11.0

if [ -x "$WEBUI_VENV_PIP" ]; then
  current="$("$WEBUI_VENV_PIP" show torch 2>/dev/null | awk '/^Version:/ {print $2}')"
  echo "[sd-webui-entrypoint] webui venv torch: ${current:-not-installed}; target: ${TARGET_TORCH}+cu128"

  case "$current" in
    "${TARGET_TORCH}+cu128"|"2.1[1-9].*+cu128"|"2.[2-9][0-9].*+cu128")
      echo "[sd-webui-entrypoint] already on a cu128 Blackwell-capable torch — skipping reinstall"
      ;;
    *)
      echo "[sd-webui-entrypoint] upgrading to torch ${TARGET_TORCH}+cu128 + torchvision for Blackwell (sm_120) kernels"
      # --quiet keeps the boot log readable. || true so a transient network
      # blip doesn't block startup entirely — A1111 will then start with the
      # old torch and just fail at generate-time, which is debuggable.
      "$WEBUI_VENV_PIP" install --quiet --upgrade \
        "torch==${TARGET_TORCH}" torchvision \
        --index-url https://download.pytorch.org/whl/cu128 || \
        echo "[sd-webui-entrypoint] WARN: torch upgrade failed; continuing with whatever torch is installed"
      ;;
  esac
else
  echo "[sd-webui-entrypoint] WARN: ${WEBUI_VENV_PIP} not found — image layout changed? skipping torch upgrade"
fi

# Hand off to ai-dock's normal startup.
exec /opt/ai-dock/bin/init.sh "$@"
