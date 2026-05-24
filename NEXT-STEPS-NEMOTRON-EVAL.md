# Nemotron RAG Eval — Final State (2026-05-24, eval complete)

Five-phase evaluation of three NVIDIA Nemotron 3 components against the
local-llm RAG pipeline's documented TAS-vs-PSFP failure class. **All five
phases run.** Full results in `scripts/benchmark/BENCHMARK-RESULTS.md`.
This file is the per-session resumption doc; consume the results doc for
the actual findings + recommendation.

---

## TL;DR — what shipped, what was rejected, what to do next

| Component | Result | Status |
|---|---|---|
| Phase 1 — benchmark scaffolding | **done** in commit `3488922` | Locked in |
| Phase 2 — Nemotron-3-Nano 30B-A3B generation | **+1 fix, −24% median latency** | **Adopt** as `CHAT_MODEL_DEEP` |
| Phase 3a — Nemotron Parse v1.2 on `8021Q-2022.pdf` §8.6 + §12.29 (39 pages) | **+2 fixes, 0 regressions** | **Best single change.** Re-extract full IEEE corpus when time permits (~6.5h) |
| Phase 3b — Full IEEE corpus through Parse | **not run** — gated on hardware time | Documented as follow-up; not blocking adoption decision |
| Phase 4 — Nemotron embed-vl-1b + rerank-1b | **−1 regression vs Phase 3a** | **Reject** for this corpus. Diagnosis below. |
| Phase 4b — Phase 4 minus embed (Nemotron rerank only) | Also **−1 regression vs Phase 3a** | Reject. Isolates the regression to the rerank's deprioritization of §12.29.1 ("managed objects" config tables) on broader semantic questions. |
| Phase 5 — Stacked combined (Parse + Nemotron gen via override) | 4/6 with `-ProfileOverride` (artifact, see results doc); production-form 4-5/6 | Promote-to-default decision documented in BENCHMARK-RESULTS.md |

**Recommendation** (from BENCHMARK-RESULTS.md, not yet applied to compose):
1. `CHAT_MODEL_DEEP` → `nemotron-3-nano:30b-a3b-q4_K_M`
2. Re-extract `data/ieee/` via Nemotron Parse (one-time, ~6.5h wall-clock,
   GPU-only; image-gen paused).
3. **Keep `nomic-embed-text` + `bge-reranker-base`.** Do NOT swap to
   Nemotron embed/rerank for this corpus.

None of these are applied yet — left as deliberate user decisions after
the eval landed.

---

## Where the per-phase eval JSONs live

`scripts/benchmark/results/<run-id>/` (gitignored, local only):

| Run ID | What |
|---|---|
| `baseline-20260522-1951` | Baseline (`ieee` + `amd` collections, default stack). 2/6. |
| `p2-nemo-gen-20260522-2324` | Phase 2 (Nemotron gen). 3/6. |
| `p3a-nemo-parse-tas-20260524-2200` | Phase 3a (Parse extraction). 4/6 — **best**. |
| `p4-nemo-rag-tas-20260524-2229` | Phase 4 (full Nemotron embed+rerank). 3/6. |
| `p4b-nemo-rerank-only-20260524-2253` | Phase 4b (nomic embed + Nemotron rerank). 3/6. |
| `p5-combined-20260524-2335` | Phase 5 stacked (Parse + Nemotron gen via override). 4/6 (artifact). |

Score with: `.\scripts\benchmark\score.ps1 -RunId <id> -CompareTo <other-id>`.

---

## Components left on disk after the eval

| Location | Purpose |
|---|---|
| `scripts/extract/extract-nemo.py` + `extract-nemo.ps1` + `.venv-nemo/` | Parse extraction tooling (adopt path). Supports `NEMO_PARSE_PAGES` env for slice extraction. |
| `scripts/nemo-rag/server.py` | embed + rerank HF sidecar (Phase 4 infra). Quiet on disk; revival gate for future Nemotron model releases. |
| `storage/nemo-parse/hf-cache/` | HF cache (Parse + embed + rerank). ~10 GB. |
| `data/ieee-nemo-parse-tas/` | Phase 3a slice (Parse-extracted, 39 pages). |
| `data/ieee-nemo-rag-tas/` | Phase 4 collection (Nemotron embed + rerank). |
| `data/ieee-nemo-rerank-tas/` | Phase 4b collection (nomic embed + Nemotron rerank). |
| `docker-compose.yml` `NEMO_RAG_URL`/`NEMO_EMBED_COLLECTIONS`/`NEMO_RERANK_COLLECTIONS` env vars | Per-feature opt-in routing (inert when sidecar not running). |
| `nemo-parse` compose service | Original vLLM Parse attempt — kept stopped, flagged deprecated. |

---

## How to resume the sidecar for a future re-bench

```bash
# 1. Start nemo-rag sidecar in WSL (~3-5 min model load from local HF cache):
wsl -e bash -lc "cd /mnt/d/Projects/local-llm/scripts/extract && \
  . .venv-nemo/bin/activate && \
  export HF_HOME=/mnt/d/Projects/local-llm/storage/nemo-parse/hf-cache && \
  python /mnt/d/Projects/local-llm/scripts/nemo-rag/server.py"

# 2. Recreate rag-server so it picks up the env (already wired):
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose up -d rag-server"

# 3. Bench the Nemotron-routed collection(s):
.\scripts\benchmark\run.ps1 -RunId "rebench-<date>" \
    -CollectionOverride "ieee-nemo-rag-tas" \
    -OverrideOnlyCollection "ieee"
```

---

## Things proven this won't work — don't redo without strong new evidence

- vLLM serving of Nemotron Parse v1.2 (tried v0.14.1 + v0.21.0; identical
  token-collapse output across versions).
- Qwen 3.6 dense or MoE on 16 GB for production-latency RAG (>700s/prompt
  on any non-trivial generation; partial CPU-spill bottleneck).
- Nemotron `llama-nemotron-embed-vl-1b-v2` + `rerank-1b-v2` as a drop-in
  for nomic-embed/bge-reranker on this IEEE-spec corpus (regressed
  `tas-vs-psfp-2` in both full and rerank-only configurations).

---

## Outstanding follow-ups (none blocking; pick up if/when)

1. **Phase 3b — extract full IEEE corpus through Parse.** ~6.5h wall-clock
   for `8021Q-2022.pdf` alone (warm avg 10.8s/page × 2163 pages); plus
   the other 26 IEEE PDFs. Run overnight when GPU isn't otherwise needed.
2. **Fix `resolveModel` literal-override topK semantics** — currently
   `<col>!<literal-model>` inherits FAST topK (8) instead of DEEP topK
   (15). One-line fix; documented in BENCHMARK-RESULTS.md "Phase 5
   caveat". Affects benchmarks using `-ProfileOverride` and any
   user-facing literal-tag override.
3. **Try `nvidia/llama-embed-nemotron-8b`** — text-only, larger
   (~16 GB VRAM in bf16). Would test whether the Phase 4 regression
   is the multimodal embedder being weak on technical text vs all
   Nemotron embedders being weak. Heavy enough to need its own GPU
   budget decision.
4. **`clause-explicit` /query-probe investigation** — flagged in the
   prior session as a ~30-min isolated investigation: probe `/query`
   directly with "Clause 12.29" wording to confirm whether the
   pre-Phase-3a failure was query-expansion dispersing focus or raw
   retrieval scoring. clause-explicit now passes via Phase 3a, so
   this is no longer urgent.

---

## Pointers

- Plan file: `~/.claude/plans/concurrent-scribbling-cocke.md`
- Per-phase results doc: `scripts/benchmark/BENCHMARK-RESULTS.md`
- Memory file: `~/.claude/projects/d--Projects-local-llm/memory/nemotron-eval-2026-05.md`
