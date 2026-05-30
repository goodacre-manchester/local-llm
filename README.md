# Local LLM + PDF RAG Stack

A self-hosted chat + PDF retrieval-augmented generation stack that runs entirely on local hardware. Drop PDFs into a folder, ask questions in a browser, get cited answers grounded in the original documents — including the figures. Optionally toggle per-chat web grounding for current-affairs and learning-gap questions.

The stack is **local-only for inference**: no cloud LLM calls, no telemetry. The chat models, embedders, reranker, image generator and vector store all run on the local GPU/CPU. When the per-chat **Web Search** toggle is enabled, search queries (not chat content) egress to external search engines via the self-hosted SearXNG; the LLM itself still runs locally.

## Capabilities

| Capability | What it does |
|---|---|
| **Chat** | Browser UI (Open WebUI) talking to local Ollama models. |
| **PDF RAG** | One folder per document collection, OpenAI-compatible chat endpoint per collection. Cited answers with file + page + clause references. |
| **Source-tree RAG** | Drop a `.git-source.yaml` (or clone) under `data/<repo>/` to RAG-index a source tree. Answers cite `file:line` ranges with deep-linked GitHub URLs in chunk metadata. Code-aware tree-sitter chunking + a dedicated code embedder (`qwen3-embedding:0.6b`). |
| **Two generation profiles** | Per-request switch between fast (`gemma4:e4b`, ~50 s) and deep (`nemotron-3-nano:30b-a3b-q4_K_M`, ~2–3 min) by appending `!deep` to the collection name. |
| **Layout-aware extraction** | Two extractor backends: PyMuPDF4LLM (CPU, fast) for datasheets; NVIDIA Nemotron Parse v1.2 (GPU) for layout-heavy standards. |
| **Picture grounding** | Every figure in every PDF is extracted as a PNG and captioned by a vision-language model (`qwen3-vl:8b`). The captions become searchable chunk text, so questions about diagrams hit the right figure. |
| **Hybrid retrieval** | Dense vector search (Chroma + Ollama `nomic-embed-text`) + in-process BM25, fused with Reciprocal Rank Fusion. |
| **Cross-encoder reranker** | `bge-reranker-base` in a CPU sidecar reorders candidates so the right clause beats vocabulary-colliding distractors. |
| **Clause-bounded chunking** | Text is packed within PDF bookmark boundaries; chunks are clause-pure (e.g. §12.29 chunks never bleed into §12.31). |
| **Multi-query expansion** | The fast model decomposes the question into focused sub-queries for better recall. |
| **Image generation** | Automatic1111 Stable Diffusion WebUI co-resident on the GPU; click the image button on any chat reply to render an SDXL image. |
| **Web grounding** | Per-chat globe-icon toggle. Self-hosted SearXNG aggregates Google/Bing/DDG + Wikipedia/arXiv/Scholar/Stack Overflow + news engines; Open WebUI fetches the top pages via Playwright (handles cookie walls), embeds them, and feeds the most relevant chunks to the LLM with inline citations. Works with any Ollama model. |
| **Editor integration** | An MCP server (`scripts/rag-mcp`) exposes the RAG collections as tools (`query_pdfs`, `list_collections`, …) for IDEs. |
| **LAN access** | Four ports (chat UI, image gen UI, RAG API, Ollama API) can be opened to the local network with a single Windows Firewall rule. |
| **Self-restarting** | Windows Startup-folder launcher brings the stack up on every logon; containers use `restart: unless-stopped`. |

## Services and URLs

After startup, from Windows:

| URL | Service |
|---|---|
| http://localhost:8080 | Open WebUI chat |
| http://localhost:11434 | Ollama API |
| http://localhost:3000/health | RAG server health |
| http://localhost:3000/v1/models | RAG model list (one entry per collection + `!deep` variants) |
| http://localhost:8000/api/v1/heartbeat | Chroma heartbeat |
| http://localhost:7860 | Stable Diffusion WebUI |
| http://localhost:8888 | SearXNG (web search backend; also browsable as a search UI) |

## Installation

For a full new-machine install (WSL, Docker, Ollama, models, GPU plumbing, autostart), see **[design.md](design.md)** — it is the runbook for reproducing this stack from a fresh clone.

The short version, assuming WSL2 / Docker / Ollama / NVIDIA Container Toolkit are already present:

```powershell
# 1. Pull all required models (and the optional model library)
.\scripts\wsl-run.ps1 "chmod +x scripts/bootstrap-models.sh && ./scripts/bootstrap-models.sh"

# 2. Download the default SDXL checkpoint
.\scripts\download-sd-models.ps1

# 3. Install MCP server deps
.\scripts\wsl-run.ps1 "cd scripts/rag-mcp && npm install"

# 4. Install the Windows Startup-folder launcher
.\scripts\install-startup-launcher.ps1

# 5. First start
.\scripts\start-local-llm.ps1
```

Every subsequent Windows logon brings the stack up automatically.

## Daily Usage

### Chat

Open http://localhost:8080. Create the admin account on first run, then pick a model from the selector:

- Direct Ollama models (`gemma4:e4b`, `nemotron-3-nano:30b-a3b-q4_K_M`, `llama3.1:8b-instruct-q8_0`, …) for general chat.
- RAG collection models (`ieee`, `amd`, `rag-active`) for grounded answers from your PDFs. See "PDF RAG" below for the one-time connection setup.

Stop / start / restart:

```powershell
.\scripts\stop-local-llm.ps1
.\scripts\start-local-llm.ps1
.\scripts\restart-local-llm.ps1                 # all services
.\scripts\restart-local-llm.ps1 open-webui      # one service
```

### PDF RAG

Each immediate subfolder of `data/` is a **collection** (`data/ieee/`, `data/amd/`). One-time browser setup:

1. http://localhost:8080 → **Settings → Connections → OpenAI API → Add connection**
2. URL: `http://127.0.0.1:3000/v1`
3. API key: `local` (or whatever `RAG_API_KEY` is set to, if you set one)

The model selector then lists `ieee`, `amd`, `rag-active`, plus `*!deep` variants. Select a collection model and ask questions normally. Replies cite sources inline as `[n]` and end with a **Sources** list of file, page, and section.

**Switching the deep / fast profile per request:**

- In Open WebUI: select `<collection>!deep` from the model dropdown.
- Via the API: set `"model": "<collection>!deep"` in the chat completion body.
- Via the MCP `query_pdfs` tool: pass `deep: true`.

**Switching the active collection** (used by `rag-active`):

```powershell
wsl -e bash -lc "curl -fsS -X PUT http://127.0.0.1:3000/active-collection -H 'Content-Type: application/json' -d '{\"name\":\"ieee\"}'"
```

**Query directly via the API:**

```powershell
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/query -H 'Content-Type: application/json' -d '{\"query\":\"what is the gate parameter table?\",\"collection\":\"ieee\",\"topK\":5}'"
```

**OpenAI-compatible chat completion** (streaming supported via `"stream": true`):

```powershell
wsl -e bash -lc @'
curl -fsS -X POST http://127.0.0.1:3000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ieee!deep",
    "messages": [{"role": "user", "content": "Summarise Clause 12.29 — the Gate Parameter Table."}]
  }'
'@
```

### Adding PDFs to a collection

Drop PDFs into `data/<collection>/` (create the folder if new), then run extract → caption → ingest. Full procedure including extractor choice in **[design.md §7](design.md)**. Short version:

```powershell
# 1. Extract (pick the backend by PDF type)
.\scripts\extract-pdfs.ps1 <collection>      # PyMuPDF4LLM: datasheets, UGs, RFCs (fast)
.\scripts\extract-nemo.ps1 <collection>      # Nemotron Parse: standards (GPU, hours for large PDFs)

# 2. Caption every picture in every PDF in every collection
wsl -e bash -lc "bash /mnt/d/Projects/local-llm/scripts/extract/run-caption-pipeline.sh"

# 3. Ingest into Chroma
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/collections/<collection>/ingest"

# 4. (For a NEW collection) recreate rag-server so it appears in the model selector
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose up -d rag-server --force-recreate"
```

### Adding a source tree to a collection

Source-tree collections use the same `data/<name>/` convention, plus a new extractor. Two modes:

**Link mode** (recommended for large upstream repos): create `data/<name>/.git-source.yaml` pointing at a remote — the extractor shallow-clones into `storage/code-cache/<name>/` (gitignored) and walks that. Example for nginx:

```yaml
# data/nginx/.git-source.yaml
url: https://github.com/nginx/nginx.git
ref: release-1.27.0
sparse_paths:
  - src/
  - auto/
  - docs/
```

**In-place mode**: drop source files (or `git clone` into the folder directly). The extractor detects this when there's no `.git-source.yaml` and at least one source file extension present. Use this for small repos or vendored snapshots.

Run the extractor:

```powershell
.\scripts\extract-code.ps1 <name>           # one collection
.\scripts\extract-code.ps1 <name> -Force    # re-extract every file
```

The extractor walks the tree, chunks each source file via `tree-sitter` (function/class boundaries for `.c/.cpp/.py/.go/.rs/.js/.ts/.java/.rb`; line-window fallback for everything else), and writes one sidecar per file into `data/<name>/.rag-cache/`. Each chunk carries `file_path`, `line_start`, `line_end`, `language`, and (for link-mode collections) a `github_url` resolved against the actual commit SHA.

Add the new collection name to `EMBED_CODE_COLLECTIONS` in `docker-compose.yml`'s `rag-server` env so it uses `qwen3-embedding:0.6b` (code-aware) instead of `nomic-embed-text`. Then recreate rag-server and ingest:

```powershell
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose up -d rag-server"
wsl -e bash -lc "curl -fsS -X POST -H 'Content-Type: application/json' -d '{\"force\":true}' http://127.0.0.1:3000/collections/<name>/ingest"
```

The collection appears in Open WebUI's model dropdown as `<name>` + `<name>!deep`. Updates: re-run `extract-code.ps1 <name>` — link mode does `git fetch` + `reset --hard FETCH_HEAD`; both modes mtime-skip unchanged files.

If you add captions to a collection that's already been ingested, the PDF mtimes are unchanged — pass `{"force":true}` to the ingest call so it re-reads the sidecars.

**Inspect captions** before or after ingest by opening `data/<collection>/picture-review.html` — a browser page with every figure side-by-side with its caption and raw VLM output. Rebuild it any time with:

```powershell
wsl -e bash -lc "python3 /mnt/d/Projects/local-llm/scripts/extract/build-picture-review.py /mnt/d/Projects/local-llm/data"
```

### Image generation

Open WebUI has a per-message **image** button on every assistant reply. Click it to send the reply text as a prompt to the local `sd-webui` (Automatic1111 with the Juggernaut XL v9 SDXL checkpoint by default). The rendered image is inlined into the conversation. Generation costs ~6–10 s and ~8 GB VRAM per image on SDXL.

Other trigger modes (auto-image per reply, tool calling) and the SDXL backend details are in **[design.md §10.6](design.md)**.

### Web grounding (per chat)

Open the chat input's **Integrations** menu (the 4-dots icon) and toggle the **globe icon** on for the current chat. From then on, every prompt in that chat triggers a SearXNG search, Open WebUI fetches the top result pages via Playwright (so cookie walls and JS-rendered sites work), chunks and embeds them, and feeds the most relevant snippets to the chat model as context. The reply ends with a **Sources** panel of clickable URLs and inline `[link]` citations.

Curated engines (configured in [searxng/settings.yml](searxng/settings.yml)):

| Category | Engines |
|---|---|
| General web | Google, Bing, DuckDuckGo, Startpage, Mojeek, Brave |
| News / current affairs | Google News, Bing News, DuckDuckGo News |
| Reference / learning | Wikipedia, Wikidata, arXiv, GitHub, Stack Overflow, Google Scholar, Semantic Scholar |

Tuned defaults wired in [docker-compose.yml](docker-compose.yml) (and live in the Admin Panel for already-running instances):

| Setting | Value | Why |
|---|---|---|
| `WEB_SEARCH_RESULT_COUNT` | 10 | OW's 3-default is too tight — many pages don't yield usable text after extraction, so 10 keeps the rank step well-fed. |
| `RAG_TOP_K` | 5 | More chunks reach the chat model's context → more inline citations possible. |
| `WEB_SEARCH_DOMAIN_FILTER_LIST` | excludes `youtube.com`, `youtu.be`, `merriam-webster.com`, `dictionary.com`, `quora.com` | These rank well but yield empty/noisy text for chat grounding — skipping them frees fetch slots for real articles. |
| `WEB_LOADER_ENGINE` | `playwright` | Handles cookie walls + JS-rendered pages. |
| `ENABLE_SEARCH_QUERY_GENERATION` | `true` | LLM rephrases the user prompt into focused sub-queries (e.g. *"main news today"* → *"top global news headlines 2026-05-30"*). |

Latency budget: ~3–10 s for search + Playwright fetch + embed; the rest is the chat model's own generation time. Works with any model in the dropdown but **not** with the PDF-RAG collection models (`ieee`, `amd`, `rag-active`) — those route through the rag-server and have their own RAG pipeline. To make web grounding default-on for a specific model, create a per-model preset under **Workspace → Models** with Web Search enabled in its capabilities.

**Getting longer / more expansive replies:** small chat models (e.g. `gemma4:e4b`) default to terse, single-paragraph summaries even with rich context. Three levers:

1. **Use a custom system prompt** — Admin Panel → Settings → Models → pick the model → **System Prompt** field. Something like: *"When answering with web-search context, write a multi-paragraph, multi-topic summary. Group related items under headings (Politics, Economy, Science, …). For each topic, name the source and a specific concrete detail. Always cite every source you use."*
2. **Switch to a more verbose model** — `llama3.1:8b-instruct-q8_0` is chattier than `gemma4:e4b`; `nemotron-3-nano:30b-a3b-q4_K_M` (deep profile) synthesises more thoroughly but takes 2-3 min.
3. **Ask specific queries instead of broad ones** — *"latest on UK general election"* returns article-level pages with body content; *"today's news"* returns section-index pages that are mostly nav + headlines, which gives the model thin material to expand on.

Privacy note: queries (not chat content) egress to the external search engines that SearXNG aggregates. The LLM, embeddings, page fetcher, and ranker all run locally.

### Editor integration (MCP)

`scripts/rag-mcp/` is a stdio MCP server registered in `.vscode/mcp.json`. Tools: `query_pdfs`, `list_collections`, `set_active_collection`, `ingest_collection`. The `query_pdfs` tool takes a `deep` boolean for the deep profile.

If you set `RAG_API_KEY` on the rag-server, add the same value to the `env` block of `.vscode/mcp.json`.

### LAN access (optional)

Four ports can be exposed to other devices on the LAN: `8080` (chat UI), `7860` (image gen UI), `3000` (RAG API), `11434` (Ollama API). The full procedure (WSL mirrored networking, Ollama bind change, single firewall rule, security trade-offs) is in **[design.md §9](design.md)**.

## Acknowledged Limitation

The local stack handles focused lookup questions and figure-grounded questions well. One failure class remains: **contrastive standards questions** where one mechanism's vocabulary dominates the question (e.g. *"is TAS the same as a PSFP stream gate?"*). First-stage retrieval pulls only PSFP chunks; the local generation model and the local query-expander both lack the domain knowledge to bridge *"TAS ⇒ scheduled-traffic / Clause 12.29"*.

The chunks are correctly indexed — a `/query` probe with explicit clause wording (*"Clause 12.29"*) returns the right chunks at rank 1. The failure is purely first-stage recall on vocabulary-colliding phrasing.

**Workaround:** re-ask the question with the defining clause number explicit (*"summarise Clause 12.29 — the Gate Parameter Table"*).

The two credible directions for closing this gap (stronger local generator/expander, or a per-corpus domain glossary fed into expansion) are recorded in **[NEXT-STEPS.md](NEXT-STEPS.md)** alongside the polish backlog.

## Pointers

- **[design.md](design.md)** — runbook for rebuilding the stack on a new machine, service-by-service spec, operational reference.
- **[NEXT-STEPS.md](NEXT-STEPS.md)** — open polish items, acknowledged limitation, levers already tried.
- **scripts/benchmark/BENCHMARK-RESULTS.md** — regression-guard benchmark + the evaluation rationale behind the current configuration choices.
- **docs/archive/** — historical session hand-off documents, retained for reference.

## Help

`/help` in Open WebUI, or open an issue on the upstream Open WebUI repo. For Claude Code itself: feedback at https://github.com/anthropics/claude-code/issues.
