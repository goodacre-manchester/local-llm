# NEXT-STEPS — Session continuation hand-off

Drop-in context for a fresh session. As of 2026-05-20.

For full architecture see [design.md](design.md); for usage see
[README.md](README.md); this file is the *decision and benchmark trail* plus
the **open next step**.

---

## Where the project is

The stack runs locally on WSL2 (Ubuntu 24.04) + Docker on Windows 11:

- **Containers:** `chroma` (`chromadb/chroma:0.5.5`, `/api/v1` coupled — see
  design.md §4), `rag-server` (Node 20, bind-mounted `app/server.js`),
  `reranker` (Python sidecar, `BAAI/bge-reranker-base`, CPU), `open-webui`.
- **Generation (switchable per-request via `model` field, no restart):**
  - `<collection>` (fast) → `gemma4:e4b` (~50 s, 100% GPU, honest about gaps)
  - `<collection>!deep` → `qwen2.5-coder:32b-instruct-q4_K_M` (~5 min,
    CPU-offloads on 16 GB, best local accuracy)
  - `<collection>!<ollama:tag>` literal override also works
- **Embeddings:** `nomic-embed-text` (local), batched `/api/embed`.
- **Retrieval pipeline (all live, all proved to help on focused queries):**
  1. PDF → page-tagged JSON sidecars (`scripts/extract/extract.py`,
     PyMuPDF4LLM backend; Docling optional).
  2. **Clause-path sectioning** from PDF bookmark tree → every chunk carries
     its authoritative outline path (`12.29.1 The Gate Parameter Table`).
  3. **Clause-bounded chunking** (`CHUNK_CLAUSE_DEPTH=3`) — text packing
     never crosses a bookmark boundary → clause-pure chunks.
  4. Hybrid dense (Chroma) + in-process BM25 fused with RRF (k=60).
  5. Near-duplicate **dedupe + canonical preference** (collapses the
     8021Q-2022 / 8021Qbv-2015 / 8021Qci-2017 / 8802-1Q-2024 overlaps;
     prefers the consolidated standard).
  6. **Cross-encoder rerank** via the `reranker` sidecar (best-effort —
     graceful fallback to fused order).
  7. **Multi-query expansion** (`QUERY_EXPANSION=true`) — fast model
     decomposes the question into focused sub-queries; union → rerank →
     top-K.
- **Grounding:** faithfulness + abstention prompt, inline `[n]` citations,
  appended **Sources** list, `citations[]` in JSON response,
  `RAG_GROUNDING=augmented` (general expertise allowed but explicitly
  tagged and never falsely cited).
- **Corpora ingested (clause-aware):** `amd` (13 PDFs, ~11.6 k chunks);
  `ieee` (27 PDFs incl. the 98 MB `802-3-2022.pdf`, ~64.7 k chunks). Zero
  failures. `AUTO_INGEST=true` skips cleanly on restart (mtime match).

## What works well now

- Focused lookup questions (e.g. AMD Vitis HLS pragma syntax / SCHED 200-880,
  IEEE 802.1Q clause lookups with explicit clause-number wording, register
  queries on datasheets). Clause-level citations are accurate.
- Hybrid + reranker handles register identifiers (`AXI_INTC`, `0x04`).
- LAN access (Open WebUI) documented and working (see README → "Access Open
  WebUI from other devices on your LAN" + design.md §12.1; Hyper-V firewall
  rule + Defender rule, test from another device).
- Switchable model architecture, two-profile docs, MCP `deep` flag.

## The unresolved failure (conclusively isolated)

**Contrastive standards questions where one mechanism's vocabulary dominates**
(e.g. IEEE 802.1Q *"is TAS the same as a PSFP stream gate, does it have an
IPV?"*). Tested across the full pipeline incl. all of §5.1 plus query
expansion, on both `ieee` (fast) and `ieee!deep`. Outcome is identical:

- Citations only ever return **PSFP / ATS** sections (§8.6.5.2, §8.6.5.4,
  §8.6.5.6, §12.31.x, §48.6.x YANG).
- The **TAS-defining `§12.29 Gate Parameter Table` / `§8.6.9 Scheduled
  traffic` chunks are never retrieved** for that phrasing, on either model.
- Both models therefore conflate TAS with PSFP and/or ATS in the answer.
- A `/query` probe with *explicit* "Clause 12.29" wording returns §12.29 at
  rank 1 — so the chunks are correctly indexed and reachable; the failure
  is purely first-stage recall on PSFP-dominated phrasing.

**Root cause (not fixable with more local retrieval engineering):** bridging
"TAS" → "scheduled traffic / Clause 12.29" requires *knowing* that mapping.
The local generation model **and** the local query-expansion model both lack
it, so neither path generates the disambiguating sub-query that would
recall §12.29. NotebookLM/Gemini solves it because Gemini has that domain
knowledge plus very large context. This is a local capability ceiling, not
a tweakable gap.

## The agreed next step (planned, NOT IMPLEMENTED)

**Cloud generation hybrid behind the existing `!deep` profile**, fully
opt-in per query. Detailed plan: [design.md §5.4](design.md). Summary:

| Profile | Backend | Privacy | Cost |
|---|---|---|---|
| `<col>` (fast, default) | local `gemma4:e4b` | fully private | free |
| `<col>!deep` | Gemini **paid** key (e.g. 2.x Pro) | no-training terms | per-token |
| `<col>!gflash` (optional) | Gemini **free-tier** key (Flash) | **used by Google for improvement — not private** | free, rate-limited |

Implementation effort is small (the switchable architecture already supports
arbitrary profiles via `resolveModel` in `app/server.js`; only the
generation call needs a Gemini provider keyed off the profile). Embeddings,
retrieval, grounding, citations all stay 100% local.

### To resume in a fresh session, the immediate steps would be

1. Decide which profile(s) to wire (just `!deep` paid; or both `!deep` paid
   + `!gflash` free trial).
2. User supplies `GEMINI_API_KEY` (and optionally `GEMINI_API_KEY_DEEP` /
   `GEMINI_MODEL_DEEP`) — never committed.
3. Implement provider abstraction in `app/server.js`: in `ragChat`, branch
   on the resolved `llmModel` — if it matches a Gemini model id, POST to
   Google's `generativelanguage` API with the existing `systemPrompt` +
   user messages; map back into the OpenAI-compatible response with the
   same `citations[]` shape. Reuse `embedTexts` for query embedding
   unchanged (local nomic).
4. Update `/v1/models` to advertise the Gemini profile(s) so they appear in
   Open WebUI's picker.
5. Re-run the verbatim TAS-vs-PSFP question and verify §12.29 / §8.6.9 are
   now cited and the TAS/PSFP/IPV distinctions are correct.

### Open knobs / cleanup the next session might also want to address

- A handful of IEEE chunks render mojibake section labels (e.g.
  `8802-1Q-2024.pdf p.469 §**���...**`) — the ISO reprint's text encoding;
  worth filtering at extraction or downranking at retrieval. Low priority.
- The 5-min `qwen2.5-coder:32b` `!deep` latency is unchanged. If a Gemini
  `!deep` lands, that supersedes the slow local deep for hard questions and
  qwen32b becomes a private fallback.
- Bookmark-derived sectioning has no effect on PDFs without an outline
  (e.g. `8802-1Q-2024.pdf`, the small extracts) — they fall back to the
  heuristic. Canonical preference already routes around them when content
  overlaps. Could note in extract.py output for visibility.

## Levers already tried (do not re-attempt without new evidence)

Each was implemented, validated against the TAS benchmark, and either kept
(if it helped focused queries) or judged complementary (it didn't fix the
contrastive case). None individually or in combination fixed contrastive
recall:

1. Structure-aware chunking (PDF outline / table-aware) — kept.
2. Hybrid dense + BM25 + RRF — kept.
3. Dedupe + canonical preference — kept.
4. Cross-encoder reranker (`bge-reranker-base`) — kept.
5. Clause-path bookmark metadata as `section` — kept.
6. Clause-bounded chunking (no cross-clause packing) — kept.
7. Deep 32B model (`qwen2.5-coder:32b`) on `!deep` — kept.
8. Multi-query expansion via the fast model — kept (helps focused queries,
   did NOT fix contrastive; expander itself lacks domain knowledge).

The conclusion that local is exhausted for this question class is therefore
**evidence-driven, not speculative**. Further retrieval/chunking tweaks are
not expected to help and shouldn't be the next investment.
