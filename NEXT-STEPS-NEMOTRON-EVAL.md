# Nemotron RAG Eval — Session Handoff (2026-05-24)

This is the continuation document for the Nemotron 3 RAG evaluation work
started 2026-05-22. Drop into a fresh session and you can resume from
exactly where this leaves off.

Original plan: `~/.claude/plans/concurrent-scribbling-cocke.md`.
Baseline / Phase 2 work is in the previous commit chain
(`3488922` and earlier). Phase 3a in-flight work is the latest commit.

---

## TL;DR — where we are

| Phase | Result |
|---|---|
| 1 — benchmark scaffolding + baseline | ✅ **done** — `2/6 pass`, 4 real RAG failures, baseline data in `scripts/benchmark/results/baseline-20260522-1951/` |
| 2 — Nemotron 3 Nano 30B-A3B (gen swap) | ✅ **done** — `3/6 pass`, fixed `axi-intc-register`, −24% median latency vs baseline. Committed `3488922`. |
| 2b/c — Qwen 3.6 (27b dense, 35b-a3b MoE) | ⚠ **conclusively too slow on 16 GB** — every long-answer prompt exceeds 700s on this hardware. Models pulled and in library; not viable for default use. |
| 3a — Nemotron Parse on TAS PDF | 🚧 **in flight** — vLLM-serving path proved brittle (4 vLLM versions tried, all produced degenerate output). Pivoting to NVIDIA's documented Option B: in-process HF transformers. Bootstrap of `.venv-nemo` running in background at handoff time. |
| 3b — Parse on full IEEE corpus | ⏸ blocked on 3a |
| 4 — Nemotron embed + rerank | ⏸ not started — most likely to actually fix TAS-vs-PSFP per all evidence |
| 5 — combined report + promote-to-default decision | ⏸ not started |

---

## Definitive findings to date (do not redo)

1. **Baseline RAG failures (real, reproducible):** `tas-vs-psfp-1`, `tas-vs-psfp-2`,
   `clause-explicit`, `axi-intc-register`. The first three are first-stage
   retrieval failures — the §12.29 / §8.6.9 chunks never appear in the top-K
   even with query expansion and reranker active. `clause-explicit` failing
   even with "Clause 12.29" literally in the prompt was a **new finding**
   contradicting what `NEXT-STEPS.md` claimed about the `/query` raw-retrieval
   probe — possibly query-expansion in `/v1/chat/completions` is dispersing
   focus. Worth investigating in isolation.

2. **Generation-model swap fixes 1/4 real failures.** Both Nemotron 30B-A3B
   and (in completed runs) qwen3.6:35b-a3b independently fix
   `axi-intc-register` by cross-referencing offset → register-name from
   retrieved chunks. None touch TAS/clause failures because those are
   retrieval-side. **Phase 4 (embed+rerank) is where the meaningful win
   should come.**

3. **Hardware verified facts:**
   - GPU: RTX 5070 Ti, sm_120 (Blackwell), 16 GB VRAM
   - Nemotron 30B-A3B Q4_K_M: 24 GB, partial RAM spillover, ~150s/prompt — viable
   - Qwen 3.6 27b dense and 35b-a3b: too slow for production (>700s/prompt) — pull-but-don't-default
   - Nemotron Parse (~3.75 GB) + HF/torch overhead (~2 GB) = ~6 GB GPU footprint — fits comfortably

4. **Infra issues uncovered + fixed (all committed in `3488922`):**
   - undici 5-min `bodyTimeout` was silently killing long-generation requests
     (Qwen 3.6 hit this; Nemotron came within 60s). Fixed via
     `setGlobalDispatcher` in `app/server.js` (override via
     `FETCH_BODY_TIMEOUT_MS` env, default 30 min).
   - Qwen 3.6's hybrid-thinking tokens overflow `num_ctx=12288`. Fixed via
     per-model bump to 24576 in `resolveModel()` for `qwen3.6:*`.

5. **vLLM serving of Nemotron Parse v1.2 is brittle on this stack** (still
   open as of handoff). Tried `vllm/vllm-openai:v0.14.1` → `v0.21.0`,
   added `--chat-template-content-format openai` (so images actually reach
   model), installed missing `open_clip_torch` + `albumentations`. Vision
   encoder runs, model produces ~300 chars of real content, then collapses
   into token-repeat loops regardless of sampling parameters. Pattern is
   consistent across versions. The model card's documented Option B (direct
   HF transformers + bundled `GenerationConfig`) doesn't go through vLLM's
   chat API and should sidestep this — that's the active pivot.

---

## What's in flight at handoff

**Background task** `bhe11dhv9` (started ~2026-05-24 mid-day) is
bootstrapping `scripts/extract/.venv-nemo`:

  - pip install torch + torchvision from `https://download.pytorch.org/whl/cu128`
    (~2.5 GB)
  - pip install -r `scripts/extract/requirements-nemo.txt` (transformers,
    accelerate, open_clip_torch, albumentations, einops, pymupdf, pillow)
  - Final import check

Estimated 10-15 min from start. Output file (local-only, gone after harness
cleanup):
  `C:\Users\john_\AppData\Local\Temp\claude\d--Projects-local-llm\180f5385-97c6-404d-8697-b8ccc351bd80\tasks\bhe11dhv9.output`

If the file is gone in the new session (it will be), just run the bootstrap
fresh via `.\scripts\extract-nemo.ps1 ieee-nemo-parse-tas` — the script
handles the venv creation idempotently.

**Container state at handoff:**
  - `chroma`, `rag-server`, `reranker`, `open-webui`: healthy, normal
  - `sd-webui`: **stopped** (freed GPU for Parse extraction)
  - `nemo-parse` (vLLM): **stopped** (deprecated for now; compose entry kept
    in `docker-compose.yml` in case a future vLLM release fixes the brittle
    output). To resume image-gen: `sudo docker compose start sd-webui`.

---

## Exact resumption sequence (copy/paste)

```powershell
# 1. Confirm where we are
git log --oneline -5
git status --short

# 2. Read this doc + the plan file
#    - this NEXT-STEPS-NEMOTRON-EVAL.md
#    - ~/.claude/plans/concurrent-scribbling-cocke.md

# 3. Bring sd-webui back up if you want image gen during the session
#    (NOT required for extraction; skip until Phase 3a is done)
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose start sd-webui"

# 4. Smoke-test the in-process Parse on one PDF page
#    (idempotent; finishes .venv-nemo bootstrap if not already done)
.\scripts\extract-nemo.ps1 ieee-nemo-parse-tas
# ^ First run takes ~10-15 min on a fresh machine for the venv + model
#   download (~6 GB total). Will fail on "no PDFs" if the data dir isn't
#   prepared yet -- see step 5.

# 5. Set up the parallel collection for Phase 3a
#    (single PDF, to validate end-to-end before doing the full IEEE corpus)
mkdir d:\Projects\local-llm\data\ieee-nemo-parse-tas
copy d:\Projects\local-llm\data\ieee\8021Q-2022.pdf d:\Projects\local-llm\data\ieee-nemo-parse-tas\
# (symlinks would also work; copy is simpler on Windows)

# 6. Run the extraction (will be SLOW -- 2163 pages * ~5s each = 3+ hours)
.\scripts\extract-nemo.ps1 ieee-nemo-parse-tas
# This writes data/ieee-nemo-parse-tas/.rag-cache/8021Q-2022.pdf.json
# with backend="nemotron-parse-v1.2".

# 7. Ingest the new sidecars into a parallel Chroma collection
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/collections/ieee-nemo-parse-tas/ingest"
# This embeds via the existing nomic-embed-text (so the variable being
# tested is ONLY extraction quality).

# 8. Run the benchmark with the new collection on the same prompts
$ts = Get-Date -Format 'yyyyMMdd-HHmm'
.\scripts\benchmark\run.ps1 -RunId "p3a-nemo-parse-tas-$ts" `
    -CollectionOverride "ieee-nemo-parse-tas" `
    -OverrideOnlyCollection "ieee"

# 9. Score + compare vs baseline
.\scripts\benchmark\score.ps1 -RunId "p3a-nemo-parse-tas-<ts>" `
    -CompareTo "baseline-20260522-1951"

# 10. Decision gate: if Phase 3a fixed any TAS / clause-explicit prompts,
#     proceed to Phase 3b (full IEEE corpus, hours-long extraction).
#     Otherwise skip to Phase 4 (embed + rerank).
```

---

## Outstanding decisions (queued for the next session)

1. **Smoke-test confirms Parse produces stable output via HF transformers?**
   If yes → run full PDF extraction. If no → escalate (try `transformers`
   version pin, or fall back to Docling-only extraction for ieee-nemo-parse-tas
   to test whether Parse-specific extraction matters at all vs just having
   *any* clean extraction).

2. **Phase 4 sequencing — go after Phase 3a or in parallel?** Phase 4 needs
   different services (vLLM serving nemotron-embed and nemotron-rerank).
   These are smaller (1B params each, ~5 GB total) and shouldn't have the
   same brittle-output issue as Parse. The plan put Phase 4 after Phase 3a
   but the user's evidence suggests Phase 4 is where the real recall win lives.

3. **Promote-to-default decision for Phase 2 winner.** Nemotron 30B-A3B was
   a clean +1 fix with -24% latency. Switching `CHAT_MODEL_DEEP` in
   `docker-compose.yml` from `qwen2.5-coder:32b-instruct-q4_K_M` to
   `nemotron-3-nano:30b-a3b-q4_K_M` is the obvious move. NOT done yet —
   left as a deliberate user decision after Phase 3/4 results are in.

4. **clause-explicit anomaly.** The new finding (explicit "Clause 12.29" in
   prompt still doesn't retrieve §12.29 chunks via `/v1/chat/completions +
   !deep + query expansion`) deserves a ~30 min isolated investigation:
   probe `/query` directly with same wording and see if raw retrieval works.
   If yes, the fix is in expansion/rerank routing — a much smaller fix than
   any Nemotron swap.

---

## File pointers for the new session

| What | Where |
|---|---|
| Full plan (originally approved) | `~/.claude/plans/concurrent-scribbling-cocke.md` |
| Benchmark prompts + automated rules | `scripts/benchmark/prompts.json` |
| Benchmark runner | `scripts/benchmark/run.ps1` |
| Benchmark scorer | `scripts/benchmark/score.ps1` |
| Baseline + Phase 2 results | `scripts/benchmark/results/` (gitignored; local only) |
| Phase 2 commit | `3488922` (see commit message for the full landed work) |
| Phase 3a (in-flight) vLLM service | `docker-compose.yml` `nemo-parse` service (stopped) |
| Phase 3a vLLM wrapper | `scripts/nemo-parse-entrypoint.sh` |
| Phase 3a in-process extractor | `scripts/extract/extract-nemo.py` (HF transformers version) |
| Phase 3a wrapper | `scripts/extract-nemo.ps1` |
| Phase 3a heavy deps | `scripts/extract/requirements-nemo.txt` |
| Phase 3a venv (host) | `scripts/extract/.venv-nemo/` (gitignored) |
| HF model cache (Parse weights) | `storage/nemo-parse/hf-cache/` (gitignored) |
| Plan-related memory | `~/.claude/projects/d--Projects-local-llm/memory/` |

---

## Things that will save the next session time

- **Don't re-attempt vLLM serving of Parse** without strong new evidence
  (e.g. a specific vLLM release notes entry that mentions multimodal
  generation_config wiring). We tried v0.14.1 and v0.21.0; both produced
  identical degenerate output. The brittleness is in vLLM's chat API
  pathway, not specific to the version we picked.
- **Don't re-eval Qwen 3.6 unless hardware upgrades.** All three Qwen 3.6
  configurations (27b dense, 35b-a3b MoE, both with bumped ctx and undici
  fix) clustered at >700s/prompt — that's MoE compute on partial-CPU-spill,
  not solvable without more VRAM.
- **The undici timeout fix benefits everything**, not just Qwen 3.6. Keep it.
- **Nemotron 30B-A3B is genuinely faster than dense qwen2.5-coder:32b** on
  this hardware (-24% median latency) even with similar VRAM spill, because
  MoE active params are 3B vs dense 32B. Worth promoting to default once
  Phase 3/4 finish.

---

## What this doc does NOT replace

- The plan file at `~/.claude/plans/concurrent-scribbling-cocke.md` — that's
  the architectural design + per-phase rationale + risk register. Read it
  first if you want the full "why each phase exists" context.
- The Phase 2 commit message at `3488922` — that's the per-file change log
  for what landed.
- The README and design.md — those document the production stack; this doc
  is eval-specific and shouldn't be promoted into them until/unless Phase 5
  recommends a default change.
