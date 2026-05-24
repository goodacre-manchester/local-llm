#!/bin/bash
# nemo-parse container entrypoint wrapper.
#
# Why this exists: vllm/vllm-openai:v0.14.1 ships without `open_clip` (the
# vision-encoder dep used by NVIDIA-Nemotron-Parse-v1.2's C-RADIO ViT-H
# encoder). Without it, vLLM crashes at config-load time with:
#   ImportError: This modeling file requires the following packages that
#   were not found in your environment: open_clip. Run `pip install open_clip`
#
# This wrapper pip-installs the missing dep before handing off to vLLM's
# normal `vllm serve` entrypoint. Idempotent (pip detects when already
# installed). A host-mounted pip cache (./storage/nemo-parse/pip-cache)
# makes the 2nd+ container creation near-instant.
#
# Remove if/when a newer vllm/vllm-openai image bundles open_clip natively
# (or once NVIDIA publishes an official NIM container for Nemotron Parse
# that we can use instead).

set -e

# Nemotron Parse's modeling code (loaded dynamically from the HF repo at
# runtime via trust_remote_code) imports several packages that the upstream
# vllm/vllm-openai image doesn't bundle. Discovered iteratively from
# `docker logs` ModuleNotFoundError stack traces:
#   - open_clip           (vision encoder C-RADIO ViT-H -- package is
#                          open_clip_torch on PyPI but module is open_clip)
#   - albumentations      (image augmentation/preprocessing pipeline)
# Install all at once so we don't churn through one missing-dep error per
# container creation. Idempotent: pip skips when already present.
echo "[nemo-parse-entrypoint] ensuring Nemotron Parse runtime deps installed..."
pip install --quiet open_clip_torch albumentations || \
  echo "[nemo-parse-entrypoint] WARN: dep install failed; vllm may crash at startup"

# Hand off to vLLM's normal serve command with all the args from docker-compose.yml.
exec vllm serve "$@"
