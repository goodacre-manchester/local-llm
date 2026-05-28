# Phase F captioning — resume notes

Hand-off doc for a new session to pick up an in-flight VLM captioning
run. Last update 2026-05-28.

## Where the pipeline is RIGHT NOW

The unified captioning pipeline crashed at IEEE picture
`802-3-2022.pdf` pic905 of 2251 (exit code 4 from the python process;
suspected cause is VRAM contention — at the time of crash some other
GPU consumer was holding ~15 GB, leaving qwen3-vl:8b with insufficient
headroom for a chat request).

**Persisted-to-disk progress** (verified by inspecting JSON sidecars):

| Collection | Captioned | Total | % |
|---|---|---|---|
| IEEE | **825** | 2918 | 28.3% |
| AMD | **0** | 393 | 0% |

Per-file IEEE breakdown:
- `802-3-2022`: 825 / 2251 ← only this file partially captioned (the
  captioner sorted alphabetically and started with this one)
- All 18 other IEEE files: 0 / N (untouched — captioner never reached
  them before the crash)

AMD: zero captioning attempted.

## Phase H extraction (the upstream step) is COMPLETE

Don't re-run extract-nemo.py or extract.py for picture extraction
unless you have a code change you specifically want to test. All
sidecars in `data/{ieee,amd}/.rag-cache/` already carry their
`type:"picture"` blocks with `bbox`, `caption`, `image_path` fields.
All PNGs are persisted under `data/{ieee,amd}/.rag-images/`.

## How to resume

Single command:

```bash
wsl -e bash -lc "bash /mnt/d/Projects/local-llm/scripts/extract/run-caption-pipeline.sh"
```

Or `run_in_background` from a Claude Code session:

```python
# Bash tool with run_in_background=True:
wsl -e bash -lc "bash /mnt/d/Projects/local-llm/scripts/extract/run-caption-pipeline.sh"
```

Resumability is built in:
- `caption-images.py` skips any picture block where `vlm_description`
  is already set (O(1) check; no VLM call). So the 825 already-captioned
  802-3-2022 blocks are skipped instantly.
- The captioner CHECKPOINTS the JSON to disk every 25 captioned blocks
  (`CAPTION_CHECKPOINT_EVERY=25`). Worst-case loss on a future crash:
  ~25 minutes of GPU work, not the entire file.

## Pre-resume sanity checks

Before launching the pipeline, verify:

1. **GPU is free** — the captioner needs ~6 GB VRAM for qwen3-vl:8b
   plus a few hundred MB headroom for inference. With the 16 GB card,
   < ~10 GB should be already in use. Check:
   ```
   nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader
   ```
   If used > ~8 GB, identify and stop the holder. The 2026-05-28 crash
   was triggered by something OTHER than the captioner consuming 15 GB.
   nvidia-smi compute-apps reported `[Insufficient Permissions]` —
   the holder was a Windows-side process not visible from WSL. Check
   Windows Task Manager → Performance → GPU → "Per-process" view.

2. **No leftover captioner / watcher processes**:
   ```
   ps aux | grep -E 'caption-images|run-caption-pipeline' | grep -v grep
   ```
   Should be empty before relaunch.

3. **Ollama responsive**:
   ```
   curl -s http://127.0.0.1:11434/api/ps
   ```

## Estimated time to completion (from current 825/2918 state)

At ~60-80s per picture average (varies with diagram complexity):

| Phase | Pics remaining | ETA |
|---|---|---|
| 802-3-2022 (rest) | 1426 | ~24-32h |
| 18 other IEEE files | 667 | ~11-15h |
| **IEEE total remaining** | **2093** | **~35-47h** |
| Regenerate IEEE .md | — | ~1-2 min |
| AMD captioning | 393 | ~7-9h |
| Regenerate AMD .md | — | ~1 min |
| **Total remaining** | | **~42-56h** |

## Output you should expect on completion

Per picture block (in the sidecar JSON):
```json
{
  "id": "p27-pic1",
  "page": 27,
  "section": "6. Principles of operation",
  "type": "picture",
  "bbox": [0.236, 0.230, 0.756, 0.579],
  "caption": "Figure 6-1—LLDP agent and its relationship to its LLC entity",
  "image_path": ".rag-images/8021AB-2016/p27-pic1.png",
  "text": "Figure 6-1—LLDP agent and its relationship to its LLC entity\n\n<VLM proposition text>",
  "vlm_description": "<VLM proposition text, post-stripper>",
  "vlm_description_raw": "<raw VLM output before stripper>",
  "vlm_model": "qwen3-vl:8b",
  "vlm_prompt_id": "v2-ctx"
}
```

`vlm_description_raw` is preserved so a future stripper-pattern
improvement can be re-applied via `caption-images.py --restrip-only`
without spending VLM time.

## After captioning completes

1. The `unified-caption.log` will have `unified pipeline done.` at the tail
2. All sidecars in `data/{ieee,amd}/.rag-cache/` have `vlm_description`
   populated on picture blocks
3. All `.rag-md/*.md` regenerated with figure blocks + image links + descriptions
4. **NOT YET DONE**: re-ingest into Chroma. PDF mtimes haven't changed
   so the standard ingest path will skip everything. Force re-ingest:
   ```powershell
   Invoke-RestMethod -Uri http://localhost:3001/collections/ieee/ingest `
                     -Method POST -ContentType application/json `
                     -Body '{"force": true}'
   Invoke-RestMethod -Uri http://localhost:3001/collections/amd/ingest `
                     -Method POST -ContentType application/json `
                     -Body '{"force": true}'
   ```
   ~5-30 min wall-clock per collection.

## Architecture notes (for context)

- **Picture extraction** (Phase H) lives in two places:
  - `scripts/extract/extract-nemo.py` for IEEE (uses Parse's
    `<class_Picture>` markers + bbox + paired captions)
  - `scripts/extract/extract.py` `_extract_pymupdf_pictures` for AMD
    (uses `page.get_images()` + `page.get_image_rects()` + heuristic
    "Figure N..." caption pairing)
- **Captioning** (Phase F): `scripts/extract/caption-images.py` —
  reads persisted PNG, builds context window from neighbouring text
  blocks, posts to Ollama qwen3-vl:8b with the locked-in v2-ctx
  prompt, strips framing via `clean_vlm_caption.strip`, writes back.
- **Pipeline**: `scripts/extract/run-caption-pipeline.sh` chains
  IEEE captioning → IEEE .md regen → AMD captioning → AMD .md regen.

## Known-unfixed issues for future iteration

- `vlm_description_raw` was added AFTER the original 802-3-2022 run
  started; the 825 already-captioned blocks have `vlm_description` but
  NO `vlm_description_raw`. Re-stripping those would require re-running
  the VLM. Acceptable cost if a stripper pattern improvement justifies
  it; otherwise leave them.
- ~10% of pictures return empty VLM output (`raw 0 -> stripped 0`).
  Could be retried with a longer timeout or different prompt; currently
  they're saved as empty `vlm_description` and skipped on resume (which
  is wrong — empty caption should retry, not skip). One-line fix to
  caption-images.py `if pic.get("vlm_description") and not force`
  → `if (pic.get("vlm_description") or "").strip() and not force`.
- Caption-pairing for AMD figures uses a "Figure N-M" heuristic in
  pymupdf4llm-extracted text. Some figures don't have caption text or
  use a different naming convention; those get `caption: ""` in the
  block. The VLM is still asked to describe them — just without the
  figure-title hint.
