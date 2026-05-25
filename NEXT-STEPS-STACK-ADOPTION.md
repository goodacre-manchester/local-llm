# Next Steps — Adopt Nemotron Eval Findings + Refresh Stack Docs

Persisted plan for converting the 2026-05-22 → 25 Nemotron RAG eval
findings into the production stack configuration, cleaning up the
eval-only artefacts, and updating the docs so a new machine can
reproduce the result. Companion to
`scripts/benchmark/BENCHMARK-RESULTS.md` (the evidence) and
`NEXT-STEPS-NEMOTRON-EVAL.md` (the eval-side resumption notes).

## Goal

After completing this plan:
- `CHAT_MODEL_DEEP` is `nemotron-3-nano:30b-a3b-q4_K_M` in production
- `data/ieee/` chunks come from Nemotron Parse extraction (Phase 3b)
- Eval-only collections (`ieee-nemo-rag-tas`, `ieee-nemo-rerank-tas`)
  are removed; Nemotron embed/rerank routing is wired-but-empty
- Open WebUI selector shows only the entries we actually use (~8 total)
- `README.md` and `design.md` describe the current stack accurately
  and include a single-sequence new-machine bootstrap that lands on
  this configuration

User-confirmed scope decisions (2026-05-25):
- **Keep all Ollama models EXCEPT `qwen2.5-coder:32b-instruct-q4_K_M`.**
  The Qwen 3.6 pair, gemma4:26b, deepseek-r1:14b, and llama3.1:8b stay
  — used for direct chat / code-gen in Open WebUI outside the RAG
  hot path. See `[[feedback-ollama-models]]` memory.
- Keep `data/ieee-nemo-parse-tas/` until Phase 3b subsumes it; remove
  the Phase 4 / 4b reject collections.
- Leave `scripts/nemo-rag/server.py` + `.venv-nemo/` +
  `storage/nemo-parse/hf-cache/` on disk as a future re-bench gate.
- Empty (but keep the variable lines for) the Nemotron routing env
  vars in compose so the wire is documented and discoverable.

---

## Phase A — Compose changes (~5 min)

A.1. Edit `docker-compose.yml` `rag-server` env block:
```yaml
- CHAT_MODEL_DEEP=nemotron-3-nano:30b-a3b-q4_K_M   # was qwen2.5-coder:32b-instruct-q4_K_M
- NEMO_EMBED_COLLECTIONS=                          # was ieee-nemo-rag-tas
- NEMO_RERANK_COLLECTIONS=                         # was ieee-nemo-rag-tas,ieee-nemo-rerank-tas
```
Leave `NEMO_RAG_URL=http://127.0.0.1:8009` set — documents the sidecar URL
for future re-bench without an env-search.

A.2. Recreate rag-server:
```bash
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose up -d rag-server"
```

A.3. Verify startup log shows the new deep model + no Nemotron routing:
```bash
wsl -e bash -lc "sudo docker logs local-llm-rag-server --tail 40 2>&1" | head -25
```
Expect: `Chat model : gemma4:e4b`, no `Nemo-RAG :` line (because both
collection lists are empty), `Collections    : amd, ieee, ieee-nemo-parse-tas`
(after Phase B cleanup).

A.4. Smoke-test the new deep route:
```bash
wsl -e bash -lc 'curl -fsS -X POST http://127.0.0.1:3000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"amd!deep\",\"messages\":[{\"role\":\"user\",\"content\":\"In one sentence, what does AXI Interrupt Controller register 0x04 do?\"}],\"stream\":false}" \
  | head -c 400'
```
Expect: a sentence-ish answer + citations. If it stalls > 5 min, abort and
check `ollama list` shows `nemotron-3-nano:30b-a3b-q4_K_M`.

---

## Phase B — Tear down eval-only artefacts (~5 min)

B.1. Remove the Phase 4 / 4b data folders:
```bash
rm -rf d:/Projects/local-llm/data/ieee-nemo-rag-tas
rm -rf d:/Projects/local-llm/data/ieee-nemo-rerank-tas
```
Keep `data/ieee-nemo-parse-tas/` (delete in Phase C after Phase 3b
makes it redundant).

B.2. Optionally drop their orphan Chroma collections (saves 10-100 MB
each; harmless to leave):
```bash
wsl -e bash -lc "curl -fsS -X DELETE 'http://127.0.0.1:8000/api/v1/collections/rag_ieee-nemo-rag-tas'    || true"
wsl -e bash -lc "curl -fsS -X DELETE 'http://127.0.0.1:8000/api/v1/collections/rag_ieee-nemo-rerank-tas' || true"
```

B.3. Drop the superseded deep model:
```bash
wsl -e bash -lc "ollama rm qwen2.5-coder:32b-instruct-q4_K_M"
```
~20 GB reclaimed. Do this AFTER Phase A so rag-server doesn't briefly
reference a missing model.

B.4. Recreate rag-server so `/v1/models` reflects the trimmed list:
```bash
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose up -d rag-server --force-recreate"
```

B.5. Verify Open WebUI selector. Open the UI → Admin → Settings →
Models. Expected entries (8 total via the rag-server OpenAI connection +
2 raw Ollama; embedding model is auto-hidden in recent Open WebUI):
- Ollama: `gemma4:e4b`, `nemotron-3-nano:30b-a3b-q4_K_M`, plus the
  user's personal-library models (`qwen3.6:35b-a3b`, `qwen3.6:27b`,
  `gemma4:26b`, `deepseek-r1:14b`, `llama3.1:8b-instruct-q8_0`)
- rag-server: `rag-active`, `rag-active!deep`, `amd`, `amd!deep`,
  `ieee`, `ieee!deep`

Any saved Open WebUI conversation bound to a deleted RAG entry
(`ieee-nemo-parse-tas!deep` etc.) will need its model picker updated on
next reply.

---

## Phase C — Phase 3b: Parse-extract the full `ieee` corpus (~overnight)

Adoption step for Phase 3a's win on the production collection.

C.1. Free the GPU for the Parse process:
```bash
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose stop sd-webui"
```

C.2. Run the full extraction (rewrites every `data/ieee/*/.rag-cache/*.json`
sidecar with `backend="nemotron-parse-v1.2"`):
```powershell
.\scripts\extract-nemo.ps1 ieee
```
Expected wall-clock: **~6.5h for 8021Q-2022.pdf alone**, plus the other
26 IEEE PDFs (varies by page count; very-long PDFs dominate). Run
overnight. Recoverable — `extract-nemo.py` is idempotent per-file via
mtime check; if it dies mid-run, re-invoke and it picks up.

C.3. Force re-ingest of the `ieee` collection so embeddings reflect the
new chunks:
```bash
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/collections/ieee/ingest"
```
~minutes for 27 PDFs of nomic embed throughput.

C.4. Bring sd-webui back:
```bash
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose start sd-webui"
```

C.5. Verify the win landed via the benchmark:
```powershell
$ts = Get-Date -Format 'yyyyMMdd-HHmm'
.\scripts\benchmark\run.ps1   -RunId "post-adopt-$ts"
.\scripts\benchmark\score.ps1 -RunId "post-adopt-$ts" -CompareTo "baseline-20260522-1951"
```
Expected: `tas-vs-psfp-2` + `clause-explicit` PASS (Phase 3a result on
the production `ieee` collection). Median `!deep` latency ~−24% vs
baseline (Phase 2 contribution).

C.6. Once C.5 confirms, drop the Phase 3a slice collection — its
chunks now live in `ieee`:
```bash
rm -rf d:/Projects/local-llm/data/ieee-nemo-parse-tas
wsl -e bash -lc "curl -fsS -X DELETE 'http://127.0.0.1:8000/api/v1/collections/rag_ieee-nemo-parse-tas' || true"
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose up -d rag-server --force-recreate"
```

---

## Phase D — Documentation refresh (~2-3 hours)

The eval introduced new components (`scripts/nemo-rag/`,
`scripts/extract/extract-nemo.py`, `.venv-nemo`, env-var routing) and
swapped the deep model. Existing docs reference the old configuration
and don't mention any of the new infrastructure. Two top-level docs to
update plus inline compose comments.

### D.1. `README.md` updates

| §       | Section title (current line) | What to change |
|---------|------------------------------|----------------|
| Top     | Title / intro | Note the production stack now uses Nemotron Parse for IEEE-corpus extraction + Nemotron-3-Nano for deep generation; one-line pointer to `scripts/benchmark/BENCHMARK-RESULTS.md` for the rationale. |
| L75-93  | "Pull required models" | Replace `qwen2.5-coder:32b-instruct-q4_K_M` with `nemotron-3-nano:30b-a3b-q4_K_M` in the pull list. Remove qwen2.5-coder if it appears as required. |
| L95-126 | "Generation model & answer tuning" | Update deep-model description to Nemotron-3-Nano 30B-A3B MoE: ~3B active params, ~16 GB resident + ~8 GB RAM spill on 16 GB cards, ~−24% median latency vs the old dense 32B. |
| L127-157| "Cloud generation hybrid (Gemini `!deep`) — planned, not implemented" | Status hasn't changed (still not implemented). Optionally remove if Nemotron-3-Nano makes the cloud hybrid unnecessary, or leave with a note that local-Nemotron is now the default deep path and the cloud hybrid is a further option. |
| L478-526| "PDF RAG — Setup" / "Extract PDFs" | Document **two extraction paths**: (a) default PyMuPDF4LLM via `scripts/extract-pdfs.ps1` (existing, lightweight, works for AMD), (b) **Nemotron Parse** via `scripts/extract-nemo.ps1` (heavy, GPU, layout-aware — recommended for standards-style PDFs). State that the IEEE collection in this repo's reference config uses path (b). Include the `NEMO_PARSE_PAGES` env-var note for slice extraction. |
| L663+   | "Useful operational commands" | Add the nemo-rag sidecar start command (kept inert by default; how to bring it up for a re-bench). |

### D.2. `design.md` updates

| §       | Section title (current line) | What to change |
|---------|------------------------------|----------------|
| §3 L63  | "Repository Layout" | Add `scripts/extract/extract-nemo.py`, `scripts/extract-nemo.ps1`, `scripts/extract/_smoke_nemo.py`, `scripts/nemo-rag/server.py`, `scripts/extract/.venv-nemo/` (gitignored, host-side venv), `scripts/extract/requirements-nemo.txt`. |
| §4 L108 | "Runtime Services" | Add: the `nemo-parse` compose service (kept-but-stopped; deprecated path documented). Add: the `nemo-rag` host-side sidecar (manual-start, port 8009, in-process HF transformers, both embed and rerank — not a compose service). Diagram should show the rag-server's optional routing edges. |
| §5.1 L235 | "Retrieval pipeline" | Add a note that for collections listed in `NEMO_EMBED_COLLECTIONS` and/or `NEMO_RERANK_COLLECTIONS`, the embed and rerank steps route to the nemo-rag sidecar instead of Ollama-nomic / bge — both env vars empty by default. Document the Phase 4 finding (Nemotron embed/rerank was tested and rejected for the IEEE corpus). |
| §5.3 L321 | "Generation model & grounding" | Replace qwen2.5-coder:32b deep-model description with Nemotron-3-Nano 30B-A3B. Update the benchmarked-question example if it referenced qwen2.5-coder. |
| §6.4 L470 | "PDF extraction prerequisite" | Add the `.venv-nemo` bootstrap path (heavy: torch 2.11+cu128 for Blackwell, transformers ≥4.56, ≥6 GB pip footprint). Note: only required for collections that want Parse extraction. |
| §7.1 L572 | "Ollama models" | Update default deep model. Note the personal-library convention (other models keepable for direct chat outside RAG). |
| §11 L708 | "Data Operations" | Document: per-collection choice between PyMuPDF4LLM and Parse extraction. When/why to pick which. The IEEE collection uses Parse; the AMD collection uses PyMuPDF4LLM (and works fine — the eval didn't motivate a change there). |
| §12 L757 | "Operational Notes" | Add a subsection: "Re-bench gate — bringing up the nemo-rag sidecar" with the one-command start + how to query the routed collections. |
| §13.1 L1045 | "New-machine quick-start" | **Critical**: extend the bootstrap sequence with the Phase 3b step. Order: existing 1-6 → ollama pull including nemotron-3-nano → docker compose up → seed PDFs → run scripts/extract-nemo.ps1 ieee (overnight) → curl ingest. Note the expected wall-clock. |

### D.3. Inline `docker-compose.yml` comments

D.3.a. The big block in lines 218-298 documenting `nemo-parse` references
the vLLM Phase 3a attempt. Either:
- Update the comment to say "deprecated path; the working extraction
  lives in `scripts/extract/extract-nemo.py` via the in-process HF
  transformers path. Service kept for future revival if vLLM gains the
  right multimodal `GenerationConfig` wiring."
- Or remove the service entirely (Phase E option).

D.3.b. The new NEMO_RAG_* env vars added in commit `7ec4468` have
inline comments — verify they still read correctly post-cleanup (empty
collection lists by default).

### D.4. Memory cross-reference

After D.1-D.3 land, update `~/.claude/projects/d--Projects-local-llm/memory/nemotron-eval-2026-05.md`
to add a line in the bottom block: "Adopted into production: see
`NEXT-STEPS-STACK-ADOPTION.md` commit <sha>." Keep the rest of the
memory as historical context.

### D.5. Single-commit doc-sweep

All D.1-D.4 changes commit together as one `docs:` commit, message
template:
```
docs: refresh README + design.md to match post-Nemotron-eval stack

- README §"Pull required models" + §"Generation model" + §"PDF RAG":
  Nemotron-3-Nano as deep, two extraction backends (PyMuPDF4LLM default;
  Nemotron Parse for layout-heavy standards PDFs).
- design.md §3, §4, §5.1, §5.3, §6.4, §7.1, §11, §12, §13.1: same
  edits + new-machine bootstrap extended to Phase 3b.
- docker-compose.yml: nemo-parse service comment updated (deprecated
  vLLM path; in-process HF transformers is the live extraction).
- ~/.claude memory: cross-reference to this adoption commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## Phase F — Image captioning sweep (deferred, run AFTER Phase C completes)

The current extraction backends both drop visual semantic content from
embedded figures:

- **PyMuPDF4LLM** (AMD collection) emits placeholder strings
  `**==> picture [W × H] intentionally omitted <==**` with no bbox and no
  image data in the JSON.
- **Nemotron Parse** (IEEE collection) sees figure pixels as part of the
  page render and emits `<class_Picture>` markers + bboxes for them, but
  `_clean_parse_md()` strips those markers; we currently only capture
  any text Parse detected inside the figure region. State-machine
  diagrams, timing charts, and other vector graphics end up with no
  retrievable description even though they carry meaningful content for
  RAG queries.

**Proposed Phase F architecture** (rough sketch, design choices to be
locked in when this phase is actually picked up):

1. Modify both extractors to **preserve picture metadata** (page,
   bbox-or-dims, ref-id) as `type: "picture"` blocks with empty text,
   rather than stripping them entirely.
2. Add `scripts/extract/caption-images.py` — post-extraction sweep that:
   - For PyMuPDF4LLM-extracted JSONs: re-opens the PDF via PyMuPDF and
     walks `page.get_images()` to pull embedded raster bytes
   - For Parse-extracted JSONs: re-renders the page-region from the
     preserved bbox markers
   - Dumps each image to PNG, POSTs to a local vision-language model,
     receives a short caption (~50-150 words)
   - Injects the result as a new `type: "image_caption"` block linked
     to the originating picture block, preserving page + section
3. Re-run `/collections/<name>/ingest` so the captions become searchable
   chunks with citations like `pg099-axi-intc.pdf — p.5, fig.2`.

**VLM choice** (decide at pickup time; none of the current Ollama models
are vision-capable):

| Candidate | Size | Notes |
|---|---|---|
| `qwen2.5-vl:7b` | ~7 GB | Diagram OCR + structural reasoning; strong on technical figures |
| `llava:7b` | ~5 GB | Faster, broader compatibility, weaker on schematics |
| `gemma3:*-vision` (if available) | ~6 GB | Newer; check Ollama tag list at pickup time |

Estimated wall-clock for captioning: ~3-10 s per image × a few hundred
figures across the IEEE corpus = **1-3 h** GPU-locked sweep on top of
Phase C's extraction time. AMD collection is image-light by comparison
(register tables, not diagrams) — would be ~10-20 min.

**Scope estimate:** ~6-8 h focused work (extractor metadata preservation
+ caption sweep script + smoke-test of VLM choice + plan/docs entries).

**Hard prerequisite — Phase C must complete first.** Captioning needs
the GPU; Phase C extraction is the higher-impact work already running.
Don't start Phase F until Phase C.5 verifies the eval result has landed
on the production `ieee` collection.

**Decision before starting:** smoke-test the VLM on 3-5 representative
figures from IEEE 802.1Q (one state-machine, one timing chart, one
register-layout figure) and judge whether the caption quality is worth
the GPU+wall-clock cost. If quality is mediocre, defer further or pick
a different VLM.

### VLM captioning prompt (user-developed, iterated 2026-05-25)

The prompt below is the **current locked-in version** (iteration v2 after
v3 with DO/DON'T examples backfired — see iteration history below).
Tested on `qwen3-vl:8b` against 3 timing diagrams from
`pg099-axi-intc.pdf`; produces RAG-friendly propositional captions on
~2/3 of outputs with some residual meta-commentary on more complex
images.

```
This is a technical diagram from a specification or reference document.
State only what the diagram is asserting — the facts, relationships,
scopes, and constraints it is communicating.

Do not describe how the diagram is drawn, its visual structure, arrows,
layout, spatial arrangement, or visual annotations (reference lines,
dashed lines, grid lines, axes, color coding). Do not augment with
background knowledge. If a component is labelled but its role is not
explicitly stated, name it without interpreting it. If the diagram does
not specify a precise value, state what it does specify rather than
using vague placeholders.

Output ONLY the factual statements the diagram makes. No introduction,
no conclusion, no meta-commentary about "the diagram" itself. Begin
with the first fact; end with the last.
```

**Why this prompt works** (preserved so future iterations don't drift):

1. *Context anchor* (`"technical diagram from a specification..."`)
   tells the VLM the genre/register to use — formal, factual, not
   discursive.
2. *Positive goal* (`"facts, relationships, scopes, constraints"`)
   constrains output to the propositional content that retrieval can
   actually match queries against. Atomic facts are higher density per
   chunk than narrative descriptions.
3. *Negative suppression of visual description* kills the default VLM
   failure mode ("there is an arrow from box A to box B labelled X")
   which produces text useless once retrieval surfaces the chunk in a
   citation list. v2 added explicit coverage of "reference lines,
   dashed lines, grid lines, axes, color coding" after v1 leaked
   "the red dashed vertical lines" through.
4. *Anti-hallucination via "do not augment with background knowledge"*
   keeps the caption grounded in the figure rather than padding with
   "this is a typical TSN state machine..." from training data.
5. *Anti-hallucination via "name without interpreting"* prevents the
   second failure case where the VLM invents roles for labelled
   components ("the `CycleStart` counter increments at...").
6. *Anti-vague-placeholder* added in v2: when the diagram doesn't
   specify a precise value, the model should say what IS specified, not
   fall back on "at a specific time point" / "for a duration".
7. *Anti-meta-commentary*: explicit "no introduction, no conclusion".
   Partially effective on qwen3-vl:8b; the model retains some chat-
   training reflex to wrap output in "The diagram asserts the
   following..." openings on complex images.

### Iteration history + lessons learned (2026-05-25 smoke session)

**v1** (original user-developed): worked on simple diagrams but two
suppression leaks:
  - Visual reference markers slipped through ("the red dashed vertical lines")
  - ~1/3 of outputs had meta-commentary opening ("The diagram asserts
    the following facts and timing relationships...")

**v2** (current): added explicit visual-annotation list (reference
lines, dashed lines, grid lines, axes, color coding); added "if the
diagram does not specify a precise value, state what it does specify";
added "no introduction, no conclusion, no meta-commentary about 'the
diagram' itself". Result on qwen3-vl:8b: color-reference leak fixed;
some meta-commentary still appears on complex images.

**v3** (DO/DON'T few-shot examples): BACKFIRED. Added explicit
examples of DO write and DO NOT write. The model **parroted the
DON'T examples back as the output's opening** ("The diagram shows the
behavior of several signals over time..."). Known LLM failure mode —
negation in instructions is unreliable; few-shot negative examples can
act as targets rather than anti-targets. **Reverted to v2.**

### Model comparison (2026-05-25 smoke session, same 3 images)

| Model | Instruction-following | Hallucination resistance | Latency |
|---|---|---|---|
| **qwen3-vl:8b** (v2 prompt) | Mostly good; meta-leak on ~1/3 outputs | Strong (no invented physical values, no spurious AXI-4 labels) | ~50-95s per image, consistent |
| **qwen3-vl:8b** (v3 prompt with DO/DON'T) | Worse — model parroted DON'T examples back | Strong | 80-120s |
| **llama3.2-vision:11b** (v3 prompt) | **Bad** — pervasive section headers ("**Clock Domain**", "**Constraints**", "**Scope**", "**Assumptions**") | **Bad** — invented "AXI-4 Signals", "clock signal is a 1.0 V signal", "Assumptions" sections | 12s OR timeout (>300s), unstable |
| **llama4:scout** | NOT TESTED — 20-24 GB VRAM doesn't coexist with Phase C's 6 GB Parse load on the 16 GB card. Test deferred. | — | — |

**Verdict so far:** qwen3-vl:8b on v2 prompt is the current chosen
baseline. The remaining ~30% meta-commentary leak is a chat-training
reflex that prompt iteration alone has a hard ceiling on; post-
processing or model change is the next lever.

### Open items for next session

The smoke test was a **good-enough validation that VLM captioning is
viable**, with two concrete unresolved questions:

1. **Chain-of-Thought (CoT) prompt variant — SMOKED 2026-05-25.**
   Added as `prompt-id=cot` in `_smoke_vlm.py` alongside the locked-in
   v2. A/B run on the same 3 pg099-axi-intc images, qwen3-vl:8b,
   concurrent with Phase C Parse extraction:

   | | v2 (locked-in) | CoT (experimental) |
   |---|---|---|
   | Avg latency | 89.7s | **62.9s (−30%)** |
   | Empty outputs | **1/3** (image 2) | 0/3 |
   | Meta-commentary leak | Image 1 heavy intro + closer; closer leaks the suppression list itself ("does not require referencing specific visual elements like dashed lines or grid axes") | None |
   | Output format | Long prose with bold | Clean numbered propositions |
   | Spurious claims | Lower — relationships described accurately | **Higher** — invents "X transitions before Y" sequential ordering by reading visual left-to-right as temporal ordering |
   | Component coverage | Comparable; image 3 catches processor_ack[1:0], first_ack, second_ack | Comparable; image 3 catches same |

   **CoT wins on format discipline, latency, and consistency. Loses on
   a new failure mode specific to timing diagrams: it reads visual
   left-to-right as temporal ordering and emits spurious sequential
   claims.** Whether this mode persists on block / state-machine /
   register / packet diagrams is unknown — the entire sample was
   timing diagrams from ONE PDF. The "Do not reference Step 1, Step 2,
   Step 3" instruction worked (no meta-commentary about the analysis
   process leaked through, which was the headline risk listed before
   the smoke).

   **Decision is deferred** until smoke on diverse diagram types
   (item 4 below) — at which point we can pick the prompt that wins on
   the broadest set or accept a per-diagram-type prompt selector. For
   now both prompts are retained in `_smoke_vlm.py`.

   Caveat on the v2 empty-output: may be GPU-contention with concurrent
   Parse extraction (Parse held ~3.4 GB VRAM during the smoke), not a
   deterministic prompt issue. Re-run after Phase C completes to
   disentangle.

2. **Test llama4:scout (after Phase C completes).** ~20-24 GB VRAM
   doesn't fit alongside Parse extraction. Once Phase C is done and
   sd-webui can be restarted, the GPU is free — load llama4:scout
   and re-run the same 3 images with v2 prompt for comparison. If
   instruction-following is significantly better than qwen3-vl:8b on
   the meta-commentary issue, switch the production VLM. Otherwise
   stick with qwen3-vl:8b.

3. **Build post-processing meta-stripper** as the deterministic fallback
   regardless of model choice. ~10-20 lines of Python; strip lines
   matching `^(The diagram|The figure|This diagram|These relationships|
   These assertions|The following|Here is)` and section headers like
   `^### `, `^\*\*[A-Z]`. Cheap insurance — even with a perfect VLM,
   chunks should be propositions-only.

4. **Smoke-test on diverse diagram types.** Current sample was 3 timing
   diagrams. Test on state-machine diagrams (e.g. 8021Qbv §8.6.9), block
   diagrams (e.g. 802.1AB Fig 6-1), register layout (e.g. pg099 Fig
   2-X), packet format (e.g. 802.1AB LLDP TLV format). The prompt may
   need diagram-type-specific tuning, OR a universal prompt may work
   if the architecture is right.

### Phase F state at handoff

| Artefact | Location | Status |
|---|---|---|
| Smoke script | `scripts/extract/_smoke_vlm.py` | Committed; **both `v2` (locked-in default) and `cot` (experimental) prompts present**; selectable via 4th CLI arg; reads embedded PDF images, calls Ollama, prints captions |
| Models pulled | `qwen3-vl:8b` (6.1 GB), `llama3.2-vision:11b` (7.8 GB) | Both in Ollama; llama3.2 underperforms so could be `ollama rm`'d to free disk |
| `llama4:scout` | NOT pulled | Defer test until after Phase C completes (GPU constraints) |
| Test PDFs used | `data/amd/pg099-axi-intc.pdf` (3 timing diagrams) | A/B'd v2 vs CoT 2026-05-25 — see CoT findings above; STILL insufficient diversity, need state-machine / block / register / packet diagrams |
| Production integration | NOT BUILT | Currently just a smoke harness; the real `caption-images.py` per the Phase F architecture sketch is still to come |

---

## Phase H — JSON content quality for RAG and rendering (deferred)

**Reframed 2026-05-25** from "extract-nemo.py polish" to a broader
JSON-quality phase. The 2026-05-25 PDF-vs-MD audits + the
markdown-renderer iteration revealed that **most renderer fixes are
forensic documentation of what the JSON should have carried in the
first place**. The same content quality issues that make the .md hard
to read also pollute the embeddings the rag-server builds for
retrieval. Investing in JSON quality at extraction time is the
leverage point: it improves RAG retrieval AND simplifies the .md
renderer (which today carries ~250+ lines of pattern detection that
would be unnecessary if the JSON already had typed `type:"table"` /
`type:"code"` / `type:"picture"` blocks and chrome-tagged
`is_chrome:true` blocks).

The current pipeline shape:

```
PDF ──Parse/PyMuPDF4LLM──► sidecar.json (mixed content quality)
                                │
                ┌───────────────┴────────────────┐
                ▼                                ▼
         rag-server ingest                 dump-sidecar-md.py
         (Chroma chunks +                  (.rag-md/*.md for
          embeddings; both                  preview; band-aids
          carry the JSON's                  much of the JSON's
          quality issues)                   structural debt)
```

Both downstream consumers benefit from cleaner JSON. The audit-
documented Parse failures (token-collapse hallucination, missing
title-page metadata, MIB ASN.1 indentation lost) are necessary fixes;
the reframing adds **structural typing** to the scope.

### Phase H scope

**(A) Block-level typing.** Today every block is `type:"text"` (with
"heading" / "table" as the only structural alternatives, and the
"table" type only meaning "this content had `|` chars in markdown"
rather than carrying structured cells). Phase H should emit:

- `type:"table"` with `cells:[[...]]` (structured rows × columns) for
  detected tabular content — not raw LaTeX or em-dash-separated text.
  Today this detection lives in the renderer (`_render_latex_tabular`,
  `_bullets_to_gfm_table`, ~250 lines). Move it to extract-nemo.py
  post-processing.
- `type:"code"` with `language:"asn1"` (or other detected language) for
  MIB modules / pseudocode / state-machine code. Today the renderer
  detects `DEFINITIONS ::= BEGIN`, `MODULE-IDENTITY`, etc. and wraps in
  fences; Phase H should do this at extract time so retrieval can
  treat code blocks differently from prose.
- `type:"picture"` blocks with `bbox:{x0,y0,x1,y1}` for Phase F image
  captioning insertion. Currently `_clean_parse_md()` strips Parse's
  `<class_Picture>` and `<x_..><y_..>` markers — Parse SEES picture
  regions but we throw the metadata away.

**(B) Content cleanup at extract time.**

- **Statistical chrome detection at JSON level.** Same algorithm the
  renderer uses (look for content recurring near page boundaries with
  normalized matching); emit chrome blocks with `is_chrome:true` so
  rag-server's ingest can filter them and the renderer can drop them
  uniformly. Result: ~30-50% fewer noise tokens in IEEE-spec
  embeddings; cleaner citations.
- **Multi-block consolidation at extract time.** When Parse splits a
  logical unit (MIB module, multi-page table, multi-page caption)
  across paragraphs, recombine them into one block. Today the renderer
  has `_consolidate_code_fences()` that does this for MIB; the same
  consolidation should happen at extract time so each chunk
  corresponds to a semantic unit.
- **Token-collapse / hallucination detection.** Per-page output scan
  for excessive short-fragment repetition; either retry with adjusted
  `GenerationConfig` (lower `max_new_tokens`, repetition penalty) or
  fall back to PyMuPDF4LLM for that page. Same composition with
  Phase G as before (G = whole-PDF dispatch; H = per-page guardrail).
- **Bbox-aware section assignment.** Currently `_apply_toc()` in
  `extract.py` assigns `section` per page from the bookmark tree. When
  multiple clauses share a page (e.g. §11.3.x and §11.4 on the same
  page of 8021AB-2016), every block gets tagged with the last clause
  start. Use Parse's bbox info to resolve per-block position. Same
  bbox preservation work as the picture-marker bullet.
- **ASN.1 indentation preservation.** A lightweight SMIv2 reformatter
  so the JSON's `text` for code blocks carries proper indentation.
  Helps both retrieval (better chunk boundaries on structured code)
  and rendering (preview readability).

### Downstream simplifications

After Phase H lands, the markdown renderer can drop roughly:
- `_LATEX_TABULAR` + `_split_latex_rows` + `_render_latex_tabular` +
  `_rows_to_gfm_or_bullets` (~200 lines) — JSON already has structured
  `type:"table"` blocks
- `_strip_orphan_latex` + `_ORPHAN_TABULAR_PREAMBLE` + `_LATEX_MULTI*`
  (~50 lines) — JSON content is pre-cleaned
- `_ASN1_MARKERS` + `_reformat_asn1` + `_wrap_code_blocks` (~100 lines)
  — JSON already has typed `type:"code"` blocks with language metadata
- `_consolidate_code_fences` (~100 lines) — JSON has pre-consolidated
  blocks per semantic unit
- `_detect_page_chrome` + `_normalize_chrome_line` (~40 lines) — chrome
  is already tagged in JSON via `is_chrome:true`
- `_collapse_long_runs` — applied during JSON cleanup, not rendering
- `_TABLE_CAPTION` + `_bullet_to_cells` + `_bullets_to_gfm_table` +
  `_detect_and_render_tables` (~150 lines) — no caption-driven recon-
  struction needed when JSON has typed table blocks

**Estimated renderer shrink: from ~700 lines today to ~150 lines
after Phase H.** What remains is the actual markdown-emission logic
(heading levels, code fences, table syntax, page markers) — no
detection / pattern matching.

### Verification

Phase H should be measured against the existing regression benchmark.
Specifically:

- **RAG retrieval impact.** Re-run `scripts/benchmark/run.ps1` against
  the Phase-H-extracted IEEE collection. Plausibly pushes
  `tas-vs-psfp-1` (the residual failure) toward PASS because cleaner
  embeddings + chrome-filtered chunks should improve first-stage
  recall on the disambiguation case.
- **Renderer regression check.** Re-run `dump-sidecar-md.py` on the
  cleaned JSON and visually compare against current renderer output.
  After deleting the band-aid detection passes the simplified renderer
  should produce equivalent or better .md output.
- **Audit re-do.** Re-audit 8021AB-2016.pdf vs the post-Phase-H .md to
  confirm the surfaced issues (email-loop hallucination, missing
  metadata, indentation loss) are resolved.

**Effort:** ~2-3 days focused work given the broader scope.
- Block-level typing (A): ~1 day (post-processing pass on extracted
  output; needs careful regex/heuristics for table and code detection)
- Content cleanup (B): ~1 day (much of the logic ports directly from
  the renderer; collapse-loop detection is the new piece)
- Validation against benchmark + audit + renderer simplification:
  ~half day

**Audit-supporting evidence:** the 2026-05-25 PDF-vs-MD audits on
`8021AB-2016.pdf` and `8021Q-2022.pdf` documented the failure modes
with examples; the audit texts can be retrieved from session history.

---

## Phase G — Speculative-fallback extraction (auto-backend dispatch, deferred)

Removes the per-collection "which extractor?" operator decision in
favour of a fast-path-then-escalate dispatcher. Cheap PyMuPDF4LLM
extraction runs universally (~seconds per PDF); a scoring step on the
resulting sidecar decides whether to escalate to Nemotron Parse for
that specific PDF. The expensive backend runs only where the cheap one
isn't good enough, measured against concrete artifacts (placeholder
counts, section coverage, block density) rather than structural
predictions about the input PDF.

**Algorithm sketch** (new top-level `scripts/extract.ps1` — existing
`extract-pdfs.ps1` + `extract-nemo.ps1` stay as manual-override
escape hatches):

```
For each PDF in <collection> lacking a current sidecar (mtime-skip):
  1. Run PyMuPDF4LLM → data/<col>/.rag-cache/<pdf>.json
  2. Score the sidecar:
       - mean picture-omitted-placeholders per page
       - section-field empty rate (post-_apply_toc)
       - mean blocks per page (under-/over-fragmentation outliers)
       - table-block ratio
       - extraction errors during PyMuPDF4LLM
  3. If aggregate score above threshold:
       - (Stop sd-webui if running, to free GPU)
       - Re-run Nemotron Parse on THIS PDF only → overwrites sidecar
At end: print per-PDF table of {backend chosen, score, reason}
        + restart sd-webui if any Parse runs occurred
```

**Why this beats the advisor approach** I sketched earlier (heuristic
on the input PDF before extraction): the quality signal is *observed*
(post-extraction artifacts), not *predicted* (structural inputs). The
cost is paid only when needed; mixed collections work per-PDF without
any operator input; failures are post-detectable so the regression
benchmark catches mis-routes.

**Calibration prerequisite — gated on Phase C completing**: the
threshold must route all `data/ieee/*` PDFs → Parse and all
`data/amd/*` PDFs → PyMuPDF4LLM, matching the eval's evidence. Phase C
produces the Parse-extracted IEEE sidecars that we'd score against to
tune thresholds. Without Phase C done, calibration would lack ground
truth.

**Strong evidence from the 2026-05-25 PDF-vs-MD audit on
`8021AB-2016.pdf`**: title pages were exactly the failure mode Phase G
is designed to handle. Parse hallucinated thousands of words of fake
email addresses on the title page (a clean text-only region where
PyMuPDF4LLM extracts cleanly). The Phase G dispatcher, if implemented,
would have scored the Parse title-page output as "low quality"
(extraction errored / contained repetition artifacts) and escalated to
PyMuPDF4LLM for that PDF. Per-PDF dispatch composes with Phase H's
per-page collapse-loop detection (Phase G is whole-PDF; Phase H is
per-page within Parse).

**Cons / risks** (honest list, for the pickup-time decision):

- Heuristic threshold can still mis-route a PDF; errors are post-
  detectable (benchmark + override) but not preventable a priori.
- A PDF that PyMuPDF4LLM extracts "cleanly" but produces retrieval-
  killing chunks would slip through. Mitigated by tuning metrics
  against the existing benchmark prompts (if PyMuPDF4LLM on IEEE
  scores below threshold for the prompts that motivated Parse
  adoption, the metric works).
- First extraction of a Parse-warranting PDF is slightly slower
  (PyMuPDF4LLM run + Parse run vs. straight-to-Parse). Marginal —
  +10 s on a 6.5 h Parse job.
- Corrupt / encrypted / scanned-as-image PDFs may throw during
  PyMuPDF4LLM — fallback must treat "errored" identically to "low
  quality" and escalate.
- Does not solve image captioning (Phase F) or OCR for scanned PDFs.

**Scope:** ~3-4 h focused work — new `scripts/extract.ps1` +
`scripts/extract/extract_auto.py`, scoring/threshold calibration
against `data/ieee/` + `data/amd/`, README §2/§5 updates demoting the
manual-decision table to override-reference status, smoke verification
that AMD doesn't fall back and at least one IEEE PDF does.

---

## Phase E — Optional teardown of the `nemo-parse` compose service

Now that Phase 3a's in-process path is the adopted one, the original
vLLM-served Parse attempt is documented-dead code. Trade-off:
- **Keep:** zero runtime cost (no `restart` policy), ~80 lines of YAML +
  the `scripts/nemo-parse-entrypoint.sh` wrapper, leaves a paper trail
  for "we tried vLLM; here's how" + a hot path if a future vLLM release
  fixes multimodal `GenerationConfig`.
- **Remove:** cleaner compose file, ~80 fewer lines to read, but the
  Phase 3a "don't redo vLLM serving of Parse" lesson lives only in the
  results doc + the memory file.

Recommendation: **defer**. Revisit if/when you next touch
`docker-compose.yml` for an unrelated reason, since the file is already
under active maintenance.

If you do tear it down:
1. Delete the `nemo-parse:` block in `docker-compose.yml`.
2. Delete `scripts/nemo-parse-entrypoint.sh`.
3. Optionally clear `storage/nemo-parse/pip-cache/` (the
   `storage/nemo-parse/hf-cache/` cache is shared with the in-process
   extractor — leave it).
4. Commit with message clarifying the deprecation lineage.

---

## What this plan does NOT do

- Does not change the `amd` collection's extraction backend.
  PyMuPDF4LLM works well for the AMD datasheets (4/6 of the baseline
  passes were AMD prompts); the eval didn't motivate a change. Parse
  on AMD is a future investigation, not an adoption blocker.
- Does not promote `nemotron-3-nano` to `CHAT_MODEL` (fast profile).
  Phase 2's `axi-intc-register` fix only materialises if the fast
  profile also runs through Nemotron — but that defeats the purpose
  of having a snappy fast profile. Trade-off documented in
  `BENCHMARK-RESULTS.md`; not adopted.
- Does not fix the `resolveModel` literal-override topK quirk. One-liner
  whenever `resolveModel` is next touched; not urgent.
- Does not remove the `nemo-rag` host-side sidecar artifacts
  (`scripts/nemo-rag/`, `.venv-nemo`, `storage/nemo-parse/hf-cache`).
  Kept as a re-bench gate.
- Does not test `nvidia/llama-embed-nemotron-8b` (text-only larger
  Nemotron embedder). Heavy enough to need its own VRAM budget
  decision; future work.

---

## Execution checklist

Hand to a future session as one block.

- [ ] Phase A — Compose edits + recreate + smoke (5 min)
- [ ] Phase B — Remove eval-only data + Chroma + qwen2.5-coder (10 min)
- [ ] Phase B.5 — Visual verify Open WebUI selector
- [ ] Phase C.1 — Stop sd-webui (free GPU)
- [ ] Phase C.2 — Overnight: `extract-nemo.ps1 ieee` (~hours)
- [ ] Phase C.3 — Force re-ingest of `ieee`
- [ ] Phase C.4 — Restart sd-webui
- [ ] Phase C.5 — Benchmark verifies adoption
- [ ] Phase C.6 — Drop `data/ieee-nemo-parse-tas/`
- [ ] Phase D.1 — README sections updated (table above)
- [ ] Phase D.2 — design.md sections updated (table above)
- [ ] Phase D.3 — docker-compose.yml comments updated
- [ ] Phase D.4 — Memory cross-reference added
- [ ] Phase D.5 — Single docs commit
- [ ] Phase F — (deferred, post-Phase-C) Image-captioning sweep — VLM smoke-test first, then design + build per the Phase F sketch above. Gate: Phase C.5 must verify the eval result first.
- [ ] Phase G — (deferred, post-Phase-C) Speculative-fallback extraction dispatcher — removes the operator "which extractor?" decision via PyMuPDF4LLM-first-then-score-then-escalate-to-Parse. Strengthened by the 2026-05-25 audit: title-page hallucinations are exactly the failure mode Phase G handles. Gate: Phase C must complete so Parse-extracted IEEE sidecars exist as ground truth for threshold calibration.
- [ ] Phase H — (deferred, post-Phase-C) **JSON content quality for RAG and rendering** — block-level typing (`type:"table"` with structured cells, `type:"code"` with language, `type:"picture"` with bbox), content cleanup at extract time (chrome tagging, multi-block consolidation, collapse-loop detection, bbox-aware section assignment, ASN.1 indentation). Re-framed from "extract-nemo polish" to the broader leverage point — same fixes benefit BOTH the rag-server ingest path (cleaner embeddings) and the markdown renderer (shrinks from ~700 lines to ~150 lines as detection logic moves upstream). Composes with Phase G (G = whole-PDF dispatch; H = per-page guardrails + JSON structural typing). ~2-3 days focused work. Gate: Phase C must complete to have current sidecars as before/after comparison.
- [ ] Phase E — (deferred indefinitely) tear down `nemo-parse` compose service

Phases A, B, D can be done in a single ~3-4h session.
Phase C requires overnight wall-clock.
Phase F is a separate ~6-8 h follow-up plus ~1-3 h captioning wall-clock.
Phase G is a separate ~3-4 h follow-up; can run before or after Phase F (independent).
Phase H is ~2-3 days focused work; **highest-leverage of the deferred phases** because it improves BOTH RAG retrieval quality AND renderer simplicity. Logically precedes Phase G (cleaner JSON makes the Phase G fallback decision easier) and Phase F (proper picture blocks give Phase F insertion points). If only one post-Phase-C investment, Phase H is the one.
