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
- [ ] Phase E — (deferred) tear down `nemo-parse` compose service

Phases A, B, D can be done in a single ~3-4h session.
Phase C requires overnight wall-clock.
