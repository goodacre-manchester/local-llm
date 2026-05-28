#!/bin/bash
# Unified VLM captioning pipeline. Captions every type:"picture" block
# emitted by the Phase H extractors (extract-nemo.py for IEEE, extract.py
# for AMD) and then regenerates the .rag-md previews so the captions
# render alongside the figures.
#
# Resumable: caption-images.py skips picture blocks that already have a
# non-empty vlm_description. The captioner itself checkpoints every
# CAPTION_CHECKPOINT_EVERY (default 25) pictures, so a crash mid-file
# loses at most ~25 minutes of GPU work. Re-running this script after
# any interruption picks up where it left off automatically.
#
# Phases:
#   1. IEEE captioning  (~2918 picture blocks, ~50-80s/pic, 40-65h GPU)
#   2. Regenerate IEEE .md
#   3. AMD captioning   (~393 picture blocks, 6-9h GPU)
#   4. Regenerate AMD .md
#
# Usage:
#   bash scripts/extract/run-caption-pipeline.sh
#
# Logs to storage/nemo-parse/unified-caption.log so the run is
# inspectable across sessions / shell restarts.

LOG=/mnt/d/Projects/local-llm/storage/nemo-parse/unified-caption.log
PROJECT=/mnt/d/Projects/local-llm

mkdir -p "$(dirname "$LOG")"
exec > "$LOG" 2>&1

cd "$PROJECT/scripts/extract"
source .venv/bin/activate

echo "[$(date -Is)] === Phase 1: IEEE captioning ==="
python -u caption-images.py "$PROJECT/data" ieee
IEEE_RC=$?
echo "[$(date -Is)] IEEE captioner exited rc=$IEEE_RC"

echo
echo "[$(date -Is)] === Phase 2: regenerate IEEE .md ==="
cd "$PROJECT"
python3 scripts/extract/dump-sidecar-md.py data ieee --force | tail -25

echo
echo "[$(date -Is)] === Phase 3: AMD captioning ==="
cd "$PROJECT/scripts/extract"
python -u caption-images.py "$PROJECT/data" amd
AMD_RC=$?
echo "[$(date -Is)] AMD captioner exited rc=$AMD_RC"

echo
echo "[$(date -Is)] === Phase 4: regenerate AMD .md ==="
cd "$PROJECT"
python3 scripts/extract/dump-sidecar-md.py data amd --force | tail -15

echo
echo "[$(date -Is)] unified pipeline done."
