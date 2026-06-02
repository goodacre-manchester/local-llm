# Open Items

This file lists what is unfinished or unresolved in the current project. Project description: [README.md](README.md). Build / operate runbook: [design.md](design.md). Benchmark evidence and per-configuration scoring: `scripts/benchmark/BENCHMARK-RESULTS.md`.

## Project policy: local-only inference

All inference (chat models, embedders, reranker, image generator, vector store) runs locally. No cloud LLM fallback is planned. The local/private boundary for *inference* is a hard constraint, not a tunable.

**Scoped egress: web grounding.** When the per-chat Web Search globe is toggled on, the self-hosted SearXNG aggregates queries to external search engines and Open WebUI fetches the resulting pages via headless Chromium. The egressed payload is the (LLM-rephrased) search query string + page-fetch requests — *not* chat history or PDF-corpus content. The chat model itself never crosses the network. Web Search is opt-in per chat; default-off models remain fully local.

## Acknowledged limitation: contrastive-recall ceiling

The stack handles focused lookup questions and figure-grounded questions correctly. One failure class remains:

**Contrastive standards questions where one mechanism's vocabulary dominates the question** (e.g. *"is TAS the same as a PSFP stream gate, does it have an IPV?"*). For these:

- Citations return only PSFP / ATS sections (§8.6.5.2, §8.6.5.4, §8.6.5.6, §12.31.x, §48.6.x YANG).
- The TAS-defining `§12.29 Gate Parameter Table` / `§8.6.9 Scheduled traffic` chunks are not retrieved for that phrasing on either generation profile.
- Both profiles therefore conflate TAS with PSFP and/or ATS in the answer.
- A `/query` probe with explicit "Clause 12.29" wording returns §12.29 at rank 1 — the chunks are indexed correctly and reachable; the failure is purely first-stage recall.

Root cause: bridging "TAS" → "scheduled traffic / Clause 12.29" requires knowing that mapping. The local generation model and the local query-expansion model both lack it, so neither generates the disambiguating sub-query that would recall §12.29.

Phase F/H VLM captioning does not fix this. The contrastive-recall failure is about *text* vocabulary collision, not figure-grounding.

### Closing the ceiling — credible but unwired

- A **stronger local generator/expander** that has the domain knowledge baked into weights. The switchable `!profile` architecture leaves room to slot one in without code changes.
- A **per-corpus domain glossary** fed into `expandQueries` in `app/server.js` (e.g. hand-curated `TAS → "scheduled traffic / Clause 12.29 / Qbv"`). Tractable; would be the investment if the failure mode becomes a working blocker.

### Practical workaround

Re-ask the question with the defining clause number explicit (*"summarise Clause 12.29 — the Gate Parameter Table"*).

## Pending tasks (next session)

- **Investigate second 5070 Ti impact** (if installed). With 32 GB total VRAM the deep model fits fully and the per-workflow GPU contention disappears. Expected measurements to capture as a baseline vs single-card:
  - `!deep` profile latency on a known prompt — expected ~2–3 min → ~30–60 s as `nemotron-3-nano:30b-a3b-q4_K_M` (~24 GB) stops spilling ~8 GB to system RAM.
  - VLM captioning rate — expected ~30–45 s/pic → ~15–25 s/pic, and no longer requires `docker compose stop sd-webui` first.
  - Concurrent SDXL image-gen + chat — expected to work without OOM (currently OOMs on `!deep` + SDXL together).
  - Web-search Task-Model rephrasing latency — expected 5–15 s → ~0–5 s if the Task Model stays resident on the second card.
  - Run `nvidia-smi` to confirm both cards visible; `ollama ps` to see per-model GPU placement. If sd-webui keeps grabbing the same card as the chat model, pin it via `CUDA_VISIBLE_DEVICES=1` in its compose env.
- **Process `data/seccom/` corpus.** Listed in `/v1/models` as `seccom` / `seccom!deep` but source PDFs need extract → caption → ingest:
  - Inspect `data/seccom/` (PDF count, document type) to pick the extractor — PyMuPDF4LLM for datasheets / RFCs, Nemotron Parse for layout-heavy standards.
  - Run `scripts/extract-pdfs.ps1 seccom` or `scripts/extract-nemo.ps1 seccom`.
  - Run `scripts/extract/run-caption-pipeline.sh` against the corpus to caption pictures.
  - Force re-ingest: `POST /collections/seccom/ingest {"force":true}`.
  - Confirm the `seccom` model in Open WebUI returns grounded answers with citations.

## Open polish items (low priority, none blocking)

- **Persistent empty captions** in a small number of dense state-diagram figures in `802-3-2022`. `qwen3-vl:8b` reliably emits empty completions on these. A different VLM (larger context or different sampling) is the lever; another retry pass has diminishing returns.
- **Numbered+bold-list leak through the caption stripper.** `clean_vlm_caption.py`'s `_NUMBERED_BOLD_HEADER` anchor is too tight to catch mid-document numbered+bold headers. Easy regex tweak.
- **"shown in the diagram" forbidden-phrase leak** occasionally passes through the v3-ctx prompt + stripper. Add to the stripper's forbidden-phrase list, or accept as occasional noise.
- **Mojibake section labels** in some IEEE chunks (e.g. `8802-1Q-2024.pdf p.469 §**���...**`) from ISO-reprint text encoding. Worth filtering at extraction or downranking at retrieval.
- **Outline-less PDFs** (e.g. `8802-1Q-2024.pdf`, small extracts) fall back to heuristic sectioning. Canonical preference already routes around them when content overlaps. Could log a warning in `extract.py` for visibility.

## Levers tried — do not re-attempt without new evidence

The following were implemented, validated against the regression-guard benchmark, and are either kept in the production pipeline or judged unhelpful for contrastive recall. None individually or in combination resolved the contrastive failure class.

Kept in production:

- Structure-aware chunking (PDF outline / table-aware).
- Hybrid dense + BM25 + Reciprocal Rank Fusion.
- Dedupe + canonical preference.
- Cross-encoder reranker (`bge-reranker-v2-m3` — promoted from `bge-reranker-base` 2026-05-31, see `scripts/code-bench/BENCHMARK-RESULTS.md`; was the highest-leverage code-RAG upgrade).
- Clause-path bookmark metadata as `section`.
- Clause-bounded chunking (no cross-clause packing).
- Multi-query expansion via the fast model.
- Nemotron Parse v1.2 extraction for the IEEE collection.
- Phase F/H VLM picture captioning.

Rejected:

- Nemotron embed + rerank (`llama-nemotron-embed-vl-1b-v2` + `rerank-1b-v2`) — regressed contrastive recall on the IEEE corpus. `nomic-embed-text` + `bge-reranker-base` stays in production. The host-side `scripts/nemo-rag/server.py` is retained as a re-bench gate if a future Nemotron retrieval-model release warrants another evaluation; see `scripts/benchmark/BENCHMARK-RESULTS.md` for the resumption procedure.

Further retrieval / chunking tweaks are unlikely to move the needle on contrastive recall. The credible directions are listed under "Closing the ceiling" above.
