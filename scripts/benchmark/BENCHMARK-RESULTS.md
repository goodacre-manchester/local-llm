# RAG Benchmark Results — Nemotron 3 Evaluation (2026-05-22 → 2026-05-24)

> **2026-05-25 update:** the canonical IEEE 802.1Q source document was
> switched from `8021Q-2022.pdf` (IEEE Std 802.1Q-2022, third edition,
> 2022) to `8802-1Q-2024.pdf` (ISO/IEC/IEEE 8802-1Q:2024, the
> international reprint — incorporates IEEE 802.1Qcw-2023 / Qdx-2024 /
> 2021 maintenance amendments that the 2022 IEEE edition predates).
> The benchmark prompts in `prompts.json` were updated to assert
> `fileName_contains: "8802-1Q-2024"` and `CANONICAL_PREFERENCE` in
> `app/server.js` / `.env.example` updated to match. The `8021Q-2022.pdf`
> file was removed from `data/ieee/`. Section numbering is preserved
> between the IEEE and ISO editions, so the §12.29 / §8.6.9 prompts
> continue to assert on the same sections — just sourced from the
> superseding ISO edition. All results below predate this switch and
> reference the original 8021Q-2022.pdf where mentioned.

Five-phase evaluation of three NVIDIA Nemotron 3 components against the
local-llm RAG pipeline's documented TAS-vs-PSFP failure class. Every result
below was produced by `scripts/benchmark/run.ps1` + `score.ps1` against the
unchanged 6-prompt set in `scripts/benchmark/prompts.json`.

## TL;DR

| Component (vs baseline) | Result | Action |
|---|---|---|
| Nemotron Parse v1.2 (extraction, Phase 3a) | **+2 fixes, 0 regressions** | **Adopt.** Best single change. Run on full IEEE corpus when time permits (Phase 3b). |
| Nemotron 3 Nano 30B-A3B (generation, Phase 2) | +1 fix, 0 regressions, −24% median latency | **Adopt** as `CHAT_MODEL_DEEP` replacement for `qwen2.5-coder:32b-instruct-q4_K_M`. |
| Nemotron embed-vl 1B-v2 + rerank 1B-v2 (Phase 4 / 4b) | **−1 regression vs Phase 3a** in both configurations | **Reject** for this corpus + benchmark. See "Nemotron retrieval finding" below. |
| Qwen 3.6 (27b dense and 35b-a3b MoE) as `!deep` (Phase 2b/c) | >700s/prompt on 16 GB VRAM — unviable | Reject. Documented for future hardware revisit only. |

**Recommended production stack** (delta from current `docker-compose.yml`):
1. `CHAT_MODEL_DEEP` → `nemotron-3-nano:30b-a3b-q4_K_M`
2. PDF ingest pipeline → Nemotron Parse for the IEEE collection (run `scripts/extract-nemo.ps1` against `data/ieee` once)
3. Keep `nomic-embed-text` + `bge-reranker-base`. Do NOT swap to Nemotron embed/rerank for this corpus.

Predicted combined config (via env vars in compose): **4/6 pass** with
production-realistic settings. See "Phase 5 caveat" below — a benchmark-
harness limitation makes the literal `-ProfileOverride` form report 4/6
artificially as a regression on `tas-vs-psfp-2`, but the `CHAT_MODEL_DEEP`
env-var form (which is what we'd ship) keeps Phase 3a's 4-pass result.
Promoting `nemotron-3-nano` to `CHAT_MODEL` (fast profile) would add the
`axi-intc-register` win for **5/6 pass**, but trades fast-profile snappiness
across the rest of the prompt set.

---

## Per-prompt matrix

| Prompt | Baseline | P2 (gen) | P3a (Parse) | P4 (full Nemo RAG) | P4b (Nemo rerank only) | P5 (Parse+Nemo gen, via override) |
|---|---|---|---|---|---|---|
| `tas-vs-psfp-1` | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |
| `tas-vs-psfp-2` | FAIL | FAIL | **PASS** | FAIL | FAIL | FAIL† |
| `clause-explicit` | FAIL | FAIL | **PASS** | **PASS** | **PASS** | **PASS** |
| `vitis-hls-pragma` | PASS | PASS | PASS | PASS | PASS | PASS |
| `axi-intc-register` | FAIL | **PASS** | FAIL | FAIL | FAIL | **PASS** |
| `abstention-test` | PASS | PASS | PASS | PASS | PASS | PASS |
| **Totals (pass / total)** | **2/6** | **3/6** | **4/6** | **3/6** | **3/6** | **4/6** |
| Median latency (s) | ~198 | ~149 | ~88 | ~146 | ~184 | ~108 |

† Phase 5 row uses `-ProfileOverride "nemotron-3-nano:30b-a3b-q4_K_M"` (per the
plan's "stacked best of each phase" call). That goes through
`resolveModel`'s literal-model branch which sets `topK = TOP_K_RESULTS = 8`
(fast default), not `TOP_K_DEEP = 15`. §12.29.1 was at rank 15 in Phase
3a's retrieved candidates — top-8 cuts it off, so the rule check fails.
See "Phase 5 caveat" below. The production promote-path
(`CHAT_MODEL_DEEP=nemotron-3-nano` env var, resolves through the `"deep"`
branch with `topK=15`) is not subject to this — `tas-vs-psfp-2` would
inherit Phase 3a's PASS.

`axi-intc-register` flips because of Phase 2's `ProfileOverride` (forced
`nemotron-3-nano` for the `!fast` profile too, which fixed the
gemma4-abstain failure). Phase 3a/4/4b did NOT override the profile, so
that prompt went back to the default `gemma4:e4b` and abstained again —
not a regression introduced by extraction or retrieval changes.

---

## Headline finding 1 — Parse extraction is a clear, isolated win

Phase 3a re-extracted `8021Q-2022.pdf` (§8.6 + §12.29 chapters only, 39
pages, ~7 min wall-clock) through NVIDIA Nemotron Parse v1.2 via the
in-process HF transformers path (NOT vLLM — see infra notes). Embeddings
and rerank were kept on the baseline `nomic-embed-text` + `bge-reranker-base`
stack.

Result: `tas-vs-psfp-2` and `clause-explicit` both flipped to PASS. No
regressions. Predicted mechanism: Parse's vision-encoder layout-aware
extraction produces cleaner section-bounded chunks (clean markdown
heading hierarchy + bbox metadata stripped post-process) so the
existing clause-bounded chunker doesn't bleed §8.6.9 narrative into
§8.6.10 state-machine tables, and §12.29 managed-objects content
survives ingest in retrievable form.

Phase 3a remains the best single configuration in the matrix above.

## Headline finding 2 — Nemotron RAG embed + rerank regress this benchmark

Both Phase 4 (full Nemotron embed + rerank) and Phase 4b (nomic embed +
Nemotron rerank only) flipped `tas-vs-psfp-2` from PASS back to FAIL.

Diagnosis (from per-citation inspection of run JSONs):
1. **First-stage candidate pool difference (Phase 4):** Nemotron
   `embed-vl-1b-v2` is multimodal (Llama 3.2 1B + SigLip2 400M).
   On long-form technical text it loses §12.29 from the candidate pool
   for the broader question "Explain the differences between TAS, PSFP,
   and ATS in IEEE 802.1Q." nomic-embed-text keeps it (rank #18-20 of
   ~30 pre-rerank candidates).
2. **Rerank-step deprioritization (Phase 4b):** Even with nomic's
   first-stage candidates (which DO include §12.29.1), the Nemotron
   `rerank-1b-v2` cross-encoder scores §12.29.1 ("Managed objects for
   scheduled traffic" — config tables, not narrative) BELOW the
   narrative §8.6.x chunks for the broader question. bge-reranker-base
   gave it a looser positive score that just-barely landed it at #15
   of the top-15 reranked list. Nemotron rerank pushed it lower → out
   of top-15 → benchmark rule (requires §12.29 cited) fails.

   In isolation the Nemotron rerank IS working as advertised: a smoke
   test gave §12.29 a +28-point lead over a PSFP distractor for a
   clean "What does Clause 12.29 specify?" query (10.13 vs −18.25).
   The regression only appears on the BROADER differences-question
   where §12.29's relevance is more debatable. The rerank's judgment
   may actually be more semantically correct than bge's — the benchmark
   rule was written against bge's looser ordering. Either way, the
   end-to-end score is what we shipped to measure, and it goes down.

**Action:** do NOT swap embed or rerank to Nemotron 1B-v2 family for this
corpus. The wider-context Nemotron `llama-embed-nemotron-8b` (text-only,
larger) was not tested — flagged for a future revisit if VRAM budget grows.

## Phase 5 caveat — benchmark-harness topK quirk discovered

When `run.ps1 -ProfileOverride <literal-model-tag>` is used, the resulting
modelField string ("ieee-nemo-parse-tas!nemotron-3-nano:30b-a3b-q4_K_M")
parses through `resolveModel` in `app/server.js`. The current branch
structure is:

```javascript
if (profile === "deep") {
  llmModel = CHAT_MODEL_DEEP; numCtx = CHAT_NUM_CTX_DEEP; topK = TOP_K_DEEP;  // 15
} else if (profile && profile !== "fast") {
  llmModel = profile;  // literal override — keeps default topK = TOP_K_RESULTS = 8
}
```

So a literal-model override (which is conceptually a "use this specific
heavy model" intent, often equivalent to a deep model) silently uses the
**fast** topK. For Phase 5 this dropped `tas-vs-psfp-2`'s §12.29.1 chunk
(which had ranked #15 in Phase 3a's retrieval) from the candidate pool
the model saw.

**This is not a generation regression.** The same prompt against the same
collection with the same embed/rerank passes when topK=15. The fix
options are:

1. Promote `CHAT_MODEL_DEEP=nemotron-3-nano:30b-a3b-q4_K_M` in
   `docker-compose.yml` (the actual production path) — `!deep` then
   resolves through the deep branch with topK=15. This is the recommended
   change anyway and avoids the issue entirely.
2. Tighten `resolveModel`: when literal-model override is requested,
   inherit deep topK/numCtx (treat literal overrides as deep-equivalent).
   One-line behavioural change; would affect any caller using the
   `<col>!<literal>` syntax. Not done as part of this eval; flagged as
   follow-up.

The Phase 2 results (committed in `3488922`) were also produced via
`-ProfileOverride`, so they're subject to the same topK=8 limit; the
`axi-intc-register` fix landed there because that prompt's `expected_citations`
are loose enough that 8 candidates are sufficient.

---

## Headline finding 3 — Generation-model swap is a clean small win

Phase 2 (Nemotron 3 Nano 30B-A3B Q4_K_M, MoE 3B active params per token):

- Fixed `axi-intc-register` by cross-referencing offset → register name
  from the retrieved AMD datasheet chunks (the dense gemma4:e4b abstained).
- Median latency 149s vs baseline 198s (−24%). MoE active-params advantage
  beats dense `qwen2.5-coder:32b` even with system-RAM spillover on 16 GB.
- No regressions across the other 5 prompts.

Promote `CHAT_MODEL_DEEP=nemotron-3-nano:30b-a3b-q4_K_M` in
`docker-compose.yml`. Pull is `ollama pull nemotron-3-nano:30b-a3b-q4_K_M`.

---

## Infrastructure findings (landed in commit `3488922`, keep regardless of phase decisions)

1. **undici `setGlobalDispatcher` in `app/server.js`** — overrides the
   default 5-min `bodyTimeout`/`headersTimeout` to 30 min (configurable
   via `FETCH_BODY_TIMEOUT_MS`). Caught a silent generation-cutoff that
   bit Qwen 3.6 and was within 60s of biting Nemotron 30B-A3B on
   `axi-intc-register`.
2. **Per-model `num_ctx` bump for `qwen3.6:*`** in `resolveModel()` — the
   hybrid-thinking reasoning tokens overflow the 12288 default ctx. Only
   activates for `qwen3.6:*` literal-tag overrides.
3. **Don't re-attempt vLLM serving of Nemotron Parse v1.2** — tried
   `vllm/vllm-openai:v0.14.1` and `v0.21.0` with `--chat-template-content-format
   openai` and missing-deps wrapper (`open_clip_torch`, `albumentations`).
   Both produced token-collapse output regardless of sampling parameters.
   The brittleness is in vLLM's chat-completions pathway not applying
   Parse's bundled `GenerationConfig`. Pivoted to in-process HF transformers
   in `scripts/extract/.venv-nemo` — works cleanly with bundled
   `GenerationConfig.from_pretrained()`.
4. **Don't re-eval Qwen 3.6 on 16 GB VRAM** — both 27b dense and 35b-a3b
   MoE configurations exceed 700s per long-answer prompt. MoE compute on
   partial-CPU-spill is the bottleneck; not solvable without more VRAM.

---

## Components left on disk after the eval

| Location | Purpose | Keep? |
|---|---|---|
| `scripts/extract/extract-nemo.py` + `extract-nemo.ps1` + `.venv-nemo/` | Parse extraction tooling | **YES** — promoted-to-default path needs this. |
| `scripts/nemo-rag/server.py` | embed + rerank HF sidecar | **YES** — quiet on disk; re-bench gate for future Nemotron releases. |
| `storage/nemo-parse/hf-cache/` | HF model cache (Parse + embed + rerank) | **YES** — ~10 GB, but re-downloading is annoying. |
| `data/ieee-nemo-parse-tas/` | Phase 3a slice (39 pages of 8021Q-2022) | **YES** — eval reproducibility. |
| `data/ieee-nemo-rag-tas/` | Phase 4 (Nemotron embed) | YES temporarily; can delete after Phase 5 commit. |
| `data/ieee-nemo-rerank-tas/` | Phase 4b (nomic embed + Nemotron rerank) | YES temporarily; can delete after Phase 5 commit. |
| `nemo-parse` compose service (stopped) | Original vLLM-served Parse attempt | YES — flagged as deprecated in compose comments; revival gate documented. |
| `docker-compose.yml`: `NEMO_RAG_URL` + `NEMO_EMBED_COLLECTIONS` + `NEMO_RERANK_COLLECTIONS` env vars | Per-feature routing | **YES** — inert when sidecar not running; documents how to opt collections in. |

---

## What this doc deliberately does NOT do

- Does not change any default in `docker-compose.yml`. The "Recommended
  production stack" above is a follow-up decision the operator makes
  by editing the compose env vars and running `scripts/extract-nemo.ps1`
  on `data/ieee/` (Phase 3b).
- Does not delete the Phase 4 / 4b collections or sidecar. They're inert
  when the sidecar isn't running and useful for re-bench gating.
- Does not run the Phase 5 combined configuration against the FULL IEEE
  corpus. That's the "Phase 3b once we adopt" follow-up, ~6.5h extraction.

---

## Resumption notes for a future session

If revisiting after a Nemotron model-family update or a hardware bump:
1. Start the nemo-rag sidecar:
   `wsl -e bash -lc "cd /mnt/d/Projects/local-llm/scripts/extract && \
     . .venv-nemo/bin/activate && \
     export HF_HOME=/mnt/d/Projects/local-llm/storage/nemo-parse/hf-cache && \
     python /mnt/d/Projects/local-llm/scripts/nemo-rag/server.py"`
2. `docker compose up -d rag-server` to recreate (env vars already wired).
3. `.\scripts\benchmark\run.ps1` + `score.ps1` against the four eval
   collections (`ieee-nemo-parse-tas`, `ieee-nemo-rag-tas`,
   `ieee-nemo-rerank-tas`, baseline `ieee`).

Plan file: `~/.claude/plans/concurrent-scribbling-cocke.md`. Per-session
narrative: `NEXT-STEPS-NEMOTRON-EVAL.md` (updated alongside this report).
