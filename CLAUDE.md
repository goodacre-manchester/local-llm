# CLAUDE.md

Project-level operating rules for AI assistants (e.g. Claude Code) working in this repository.
These rules override default assistant behavior. Companion docs: [README.md](README.md)
(capabilities), [design.md](design.md) (build/operate runbook), [NEXT-STEPS.md](NEXT-STEPS.md)
(open items and acknowledged limitations).

## Rule 0 — Persist rules here, not only in local memory

Durable project rules, conventions, and constraints MUST live in version-controlled project files
(this `CLAUDE.md`, `design.md`, `NEXT-STEPS.md`) and be committed and pushed to origin — never only
in machine-local assistant auto-memory. Auto-memory is per-machine and does not travel with a clone;
project files do. When you learn, derive, or are told a new durable rule, add it here (or to the
relevant doc) and push it, so it is portable across every clone of the repo.

## Rule 1 — Local-only inference (hard constraint)

All inference runs locally on the operator's hardware: chat models, embedders, reranker, image
generator, and the vector store. No cloud LLM calls, no telemetry egress. There is no cloud
fallback and none is planned (the Gemini hybrid plan was dropped 2026-05-30). Do not propose,
wire in, or default to any hosted-LLM path.

Scoped exception — web grounding: when the per-chat Web Search globe is toggled on, the self-hosted
SearXNG egresses only the (LLM-rephrased) search-query string plus page-fetch requests — never chat
history or corpus content. The chat model itself never crosses the network. Web Search is opt-in
per chat; default-off models stay fully local.

## Rule 2 — Do not recommend removing Ollama models

The operator keeps a personal model library beyond the RAG stack's hot path. Never recommend
`ollama rm` for a model merely because it is "not wired into docker-compose." Only suggest removal
for hardware-incompatible models, or when the operator explicitly asks.

## Rule 3 — Honor the configuration invariants

`design.md` §11 lists the invariants in full. The most error-prone, kept here for visibility:

- Chroma image is pinned to `chromadb/chroma:0.5.5` — `app/server.js` calls `/api/v1`, which is
  removed in 0.6+. Do not bump it without porting the API calls.
- Collection convention: each immediate subfolder of `data/<name>/` is one collection (Chroma
  collection `rag_<name>`), containing `.rag-cache/` (sidecar JSON), `.rag-images/`, `.rag-md/`.
- Fixed ports: 11434 Ollama, 8000 Chroma, 3000 rag-server, 3001 rag-mcp, 7860 sd-webui,
  8008 reranker, 8080 open-webui, 8888 searxng.
- The two entrypoint-wrapper mounts are load-bearing: `sd-webui-entrypoint.sh` (Blackwell-capable
  PyTorch) and `open-webui-entrypoint.sh` (surgical Web Search bug patch). Remove each only when its
  documented upstream-fix criterion is met.
- Code-RAG env wiring on rag-server: `EMBEDDING_MODEL_CODE` and `EMBED_CODE_COLLECTIONS`.
- `docker-compose.yml` env changes require a container recreate (`docker compose up -d <svc>`), not
  a plain `restart`, to take effect.

## Rule 4 — Platform and shells

Hybrid host: Windows 11 is the operator host; WSL2 Ubuntu runs Ollama (native) and the Docker
services. Entry-point automation is PowerShell under `scripts/`; service-side commands run inside
WSL. Use the shell appropriate to the target, and prefer the existing `scripts/` helpers
(`wsl-run.ps1`, `start-local-llm.ps1`, the `extract-*.ps1` entries) over ad-hoc invocations.
