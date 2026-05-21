# Local LLM + PDF RAG Runbook (Implemented Baseline)

This document is the definitive runbook for the implemented setup in this repository.
It records what is running now and the exact procedure to reproduce it on another machine.
It is kept in sync with `docker-compose.yml`, `app/server.js`, and the `scripts/`.

## 1. Scope and Architecture

This implementation uses a hybrid Windows + WSL model:

- Windows 11 is the operator host (VS Code, terminals, client apps).
- WSL2 Ubuntu 24.04 runs Ollama and Docker workloads.
- Docker in WSL runs:
  - `chroma` vector database on port `8000` (published `8000:8000`).
  - `rag-server` (Node.js) on port `3000` (`network_mode: host`).
  - `open-webui` browser chat interface on port `8080` (`network_mode: host`).
- Ollama runs in WSL on port `11434`.

Data flow:

1. Each immediate subfolder of `./data/` (e.g. `data/ieee`, `data/amd`) is a
   **collection**. `./data` is mounted read-only into the RAG container as `/data`.
2. PDF text is extracted, chunked, and embedded via Ollama `nomic-embed-text`.
3. Embeddings are persisted in Chroma, one Chroma collection per folder
   (`rag_<folder>`).
4. Query requests embed the question and return the top-k matched chunks; the
   OpenAI-compatible endpoint additionally feeds them to `CHAT_MODEL` for a
   synthesised answer.

## 2. Verified Host Baseline (This Machine)

Verified on this machine 2026-05-18:

- OS: Windows 11 host + WSL2 Ubuntu 24.04.4 LTS (Noble)
- WSL kernel: `6.6.87.2-microsoft-standard-WSL2`
- GPU in WSL: `NVIDIA GeForce RTX 5070 Ti`, `16303 MiB`, driver `591.86`
- Ollama: `0.24.0`
- Docker Engine (Server): `29.5.0`
- Docker Compose plugin: `v5.1.3`

Re-capture these on a new machine with:

```powershell
wsl -e bash -lc "uname -r && . /etc/os-release && echo \$PRETTY_NAME"
wsl -e bash -lc "ollama --version"
wsl -e bash -lc "sudo docker version --format '{{.Server.Version}}'"
wsl -e bash -lc "sudo docker compose version"
wsl -e bash -lc "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"
```

## 3. Repository Layout

Implemented files and folders:

- `docker-compose.yml`
- `app/package.json`, `app/server.js` — RAG server (structure-aware ingest,
  hybrid retrieval, grounded citations)
- `scripts/extract/extract.py`, `scripts/extract/requirements.txt` — PDF →
  page-tagged JSON sidecar extractor (Docling → PyMuPDF4LLM → pypdf)
- `scripts/extract-pdfs.ps1` — Windows entry point for the extractor
- `scripts/reranker/server.py`, `scripts/reranker/requirements.txt` — cross-
  encoder reranker sidecar
- `scripts/rag-mcp/package.json`, `scripts/rag-mcp/index.js` — MCP server
- `.vscode/mcp.json` — registers the MCP server for the editor
- `.env.example` — documents the variables `server.js` reads
- `scripts/bootstrap-models.sh`
- `scripts/ensure-services.sh` (WSL start + health-wait logic)
- `scripts/start-local-llm.ps1` (Windows start entry point)
- `scripts/stop-local-llm.ps1`
- `scripts/restart-local-llm.ps1`
- `scripts/wsl-run.ps1`
- `scripts/install-startup-launcher.ps1` (installs logon autostart)
- `scripts/register-autostart-task.ps1` (Task Scheduler method, requires admin)
- `scripts/test-rag.ps1`
- `README.md`
- `data/<collection>/` (input PDFs, one folder per collection)
- `storage/chroma/` (persistent vector store)
- `storage/open-webui/` (persistent Open WebUI data)
- `storage/reranker/` (persisted reranker venv + HF model cache)

## 4. Runtime Services

`docker-compose.yml` defines:

1. `chroma`
   - Image: `chromadb/chroma:0.5.5` (pinned — see note below)
   - Port: `8000:8000`
   - Persistence: `./storage/chroma:/chroma/chroma`
   - Healthcheck: HTTP `GET /api/v1/heartbeat`

2. `rag-server`
   - Image: `node:20-slim`
   - Network: `host`
   - Working dir: `/app`
   - Mounts: `./app:/app`, `./data:/data:ro`
   - Startup command: `npm install && node server.js`
   - Depends on: `chroma` healthy
   - Healthcheck: HTTP `GET /health`

3. `reranker`
   - Image: `python:3.12-slim`
   - Network: `host`, port `8008`
   - Mounts: `./scripts/reranker:/app`, `./storage/reranker:/cache`
     (persisted venv + HF model cache → torch/model download once)
   - Startup: venv + `pip install` + `python server.py`
   - Healthcheck: HTTP `GET /health` (long `start_period`: first boot
     installs torch and downloads the reranker model)
   - CPU-only by design (no GPU container plumbing). No `depends_on` from
     `rag-server` — retrieval degrades to fused order if it is unavailable.

4. `open-webui`
   - Image: `ghcr.io/open-webui/open-webui:main`
   - Network: `host`
   - Port: `8080`
   - Ollama upstream: `http://127.0.0.1:11434`
   - Persistence: `./storage/open-webui:/app/backend/data`
   - Depends on: `rag-server` healthy
   - No healthcheck is defined in compose; readiness is verified by
     `scripts/ensure-services.sh` polling `http://127.0.0.1:8080/health`.

All containers use `restart: unless-stopped` so Docker restarts them after a
crash or daemon restart. Startup ordering is enforced through
`condition: service_healthy` on each `depends_on` reference.

> **Chroma version coupling:** `server.js` uses the `/api/v1` Chroma API,
> which only exists on Chroma `< 0.6`. The image is intentionally pinned to
> `chromadb/chroma:0.5.5`. Upgrading the image to `>= 0.6` removes `/api/v1`
> and breaks every Chroma call — migrate to `/api/v2` first if upgrading.

Environment defaults used by `server.js` (see `.env.example` for the full list):

- `PORT=3000`
- `OLLAMA_HOST=http://127.0.0.1:11434`
- `CHROMA_URL=http://127.0.0.1:8000`
- `DATA_DIR=/data`
- `EMBEDDING_MODEL=nomic-embed-text`
- `CHAT_MODEL=qwen2.5-coder:32b-instruct-q4_K_M` (see §5.1 for the
  speed/accuracy trade vs `deepseek-r1:14b` / `llama3.1:8b`)
- `CHAT_NUM_CTX=12288` (Ollama context window; its ~2-4k default silently
  truncates retrieved sources on long questions)
- `CHUNK_SIZE=1000`
- `CHUNK_OVERLAP=200` (clamped to `< CHUNK_SIZE`)
- `EMBED_MAX_CHARS=1600` (hard embed-input ceiling; oversized tables are
  row-split with the header repeated)
- `TOP_K_RESULTS=8`
- `RAG_GROUNDING=augmented` (`strict` = sources only; `augmented` =
  sources primary + tagged general expertise for gaps, NotebookLM-like)
- `AUTO_INGEST=true` (set `false` during large manual ingests to stop a
  container recreate from auto-flat-ingesting un-extracted PDFs)
- `DEFAULT_COLLECTION=` (empty → first folder alphabetically)
- `RAG_API_KEY=` (empty → unauthenticated; localhost-only use)

## 5. RAG Server API (Implemented)

### 5.1 Retrieval pipeline

1. **Extraction** (`scripts/extract/extract.py`, run via
   `scripts/extract-pdfs.ps1`): each PDF → `data/<col>/.rag-cache/<pdf>.json`
   with page/section-tagged blocks; tables exported as Markdown and kept whole.
   Backend chosen by availability: Docling > PyMuPDF4LLM > pypdf.
   **Clause-path sectioning**: the PDF bookmark/outline tree (PyMuPDF
   `get_toc`) is the authoritative section structure for standards/datasheets
   — every block's `section` is overwritten with the deepest active bookmark
   title (e.g. `12.29.1 The Gate Parameter Table`), falling back to the
   backend's heuristic heading only where the outline is silent (front
   matter / un-bookmarked PDFs). This is the key disambiguator for mechanisms
   that collide in prose but live in separate outline branches — IEEE 802.1Q
   **TAS** = 12.29 / 8.6.9, **PSFP** = 12.31, **ATS** = 8.6.5.6 / 47 — and it
   makes citations clause-exact. The full breadcrumb is also kept as
   `section_path`.
2. **Ingest**: `server.js` prefers the sidecar (structure-aware chunking —
   tables/headings emitted whole, text packed to `CHUNK_SIZE`, page/section/
   blockType stored in Chroma metadata). **Clause-bounded chunking**: text
   packing never crosses a bookmark boundary (`section_path` truncated to
   `CHUNK_CLAUSE_DEPTH` levels) so every chunk is clause-pure — a paragraph
   from `8.6.8.5 ATS …` cannot share a chunk with `8.6.9 Scheduled traffic …`,
   which removes the cross-clause bleed that fed the TAS↔ATS/PSFP conflation. Falls back to flat `pdf-parse` text
   if no usable sidecar exists — that fallback now logs loudly
   (`[sidecar]`/`[ingest] FLAT`) so a silent structured→flat degradation
   can't recur (it once produced 21k page-less IEEE chunks). Embedding is
   **batched** (`embedTexts`, 64 chunks per `/api/embed` call): fewer HTTP
   round-trips and cleaner/more robust code, but measured wall-clock gain was
   marginal (~22 vs ~21 ms/chunk) — ingest is bound by nomic-embed-text GPU
   throughput + Chroma upsert, not request overhead. Real speedups would need
   a faster/smaller embed model, parallel embed requests, or not sharing the
   GPU with a resident chat model during ingest.
3. **Hybrid retrieval**: dense Chroma vector search + an in-process BM25
   lexical index (rebuilt lazily from Chroma after restart, invalidated on
   ingest), fused with Reciprocal Rank Fusion (k=60). Fixes exact-identifier
   recall on datasheets. Over-fetches `max(topK*6, RERANK_CANDIDATES*2, 40)`.
3a. **Dedupe + canonical preference**: this corpus has the consolidated
   standard, the amendments it incorporates, and an ISO reprint, so identical
   text recurs across files. Near-duplicate chunks (normalised-signature
   collision) are collapsed, keeping the copy from the earliest
   `CANONICAL_PREFERENCE` source so citations point at the current
   consolidated standard, not a superseded amendment.
3b. **Cross-encoder rerank** (`reranker` sidecar, best-effort): the deduped
   top `RERANK_CANDIDATES` are rescored by a `bge-reranker` cross-encoder so
   the right clause outranks vocabulary-colliding distractors — the concrete
   failure this fixes is IEEE 802.1Q **TAS transmission gate** (Clause 12.29)
   retrieving **PSFP stream gate** (Clause 12.31) chunks because the two
   mechanisms share nearly all terminology. Down/disabled → fused order
   (graceful, like the BM25 channel). The `!deep` profile retrieves
   `TOP_K_DEEP` (more context for the stronger model).
4. **Grounding**: faithfulness system prompt (answer only from numbered
   sources, inline `[n]` citations, fixed abstention sentence when
   unsupported). Zero-retrieval queries short-circuit to the abstention
   response. Replies get an appended **Sources** list; the non-stream JSON
   response also carries a `citations[]` array.

### 5.2 Endpoints

Service in `app/server.js` exposes:

1. `GET /health` — Chroma heartbeat + active-collection summary. Always
   exempt from `RAG_API_KEY` auth so the Docker healthcheck works.
2. `GET /sse` — SSE stream of ingest progress events.
3. `GET /collections` — list collection folders and their Chroma/ingest status.
4. `POST /collections/:name/ingest` — ingest one collection. Body
   `{ "force": false }`. Files whose mtime is unchanged are skipped.
5. `GET /active-collection` / `PUT /active-collection` — read / switch the
   active collection (`{ "name": "..." }`); switching does not re-index.
6. `POST /query` — body `{ "query": "...", "collection"?: "...", "topK"?: 5 }`;
   returns top-k matched chunks with metadata and distance.
7. `GET /v1/models` — OpenAI-compatible model list (`rag-active` + one entry
   per collection).
8. `POST /v1/chat/completions` — OpenAI-compatible RAG chat. `model` selects
   the collection (`rag-active` = current active). Honours an optional `topK`
   in the request body and `stream: true`.

Security notes:

- CORS is restricted to `localhost` / `127.0.0.1` browser origins. Non-browser
  clients (curl, the Open WebUI backend, the MCP server) are unaffected.
- If `RAG_API_KEY` is set, all endpoints except `/health` require
  `Authorization: Bearer <key>` or an `x-api-key` header. The Open WebUI
  OpenAI-connection API key and the MCP server's `RAG_API_KEY` must match.
- This is a REST + SSE + OpenAI-compatible service, plus a separate stdio MCP
  server (`scripts/rag-mcp`). It is not a single MCP protocol server.

### 5.3 Generation model & grounding (benchmarked on a deep Vitis-HLS question)

The retrieval/citation layer is model-independent and grounds correctly
(right UG1399 pages every time). The generator is a speed/accuracy trade:

| Model | Latency | Accuracy on deep specifics | Notes |
|---|---|---|---|
| `qwen2.5-coder:32b-instruct-q4_K_M` | ~5 min | Best — only local model right on BOTH exact pragma and the ram_s2p subtlety | >16GB VRAM → CPU offload. **`!deep` profile.** |
| `gemma4:e4b` | ~50 s | Shallower (honestly punts); imprecise pragma — but **lowest hallucination risk**, flags doc gaps instead of confabulating | ~10GB, **100% GPU**. **Fast default.** |
| `gemma4:26b` | ~3 min | Confident but **wrong** (mis-"corrected" the pragma; ram_s2p inversion; DATAFLOW mismatch) | MoE-A4B ~17GB, CPU offload. Not recommended. |
| `deepseek-r1:14b` | ~30 s | Structured but confidently wrong on ram_s2p | rare CJK token leak; fits 16GB |
| `llama3.1:8b-instruct-q8_0` | ~13 s | Weakest; hedges; one wrong B answer | fits 16GB, fast |

**Decision: a switchable two-profile setup** (the fast/honest vs slow/accurate
split is complementary, not a single winner):

- `CHAT_MODEL` (fast default) = `gemma4:e4b` — GPU-resident, ~50s, honest.
- `CHAT_MODEL_DEEP` = `qwen2.5-coder:32b` — max accuracy when it matters.
- Select per request via the OpenAI `model` suffix: `amd` (fast) vs
  `amd!deep` (deep). `resolveModel()` parses `<collection>[!profile]`;
  `!<ollama:tag>` also works as a literal override. `/v1/models`
  advertises both variants per collection (Open WebUI shows them); the MCP
  `query_pdfs` tool exposes a `deep` boolean. `CHAT_NUM_CTX_DEEP` lets the
  deep profile use a different context size.

#### Gemma 4 config notes (critically adapted, not copied)

Third-party guidance (leetllm.com) recommends `temperature=1.0, top_p=0.95,
top_k=64` and `num_ctx=32768` for Gemma 4, plus a `<|think|>` system-prompt
prefix for thinking mode. Applied judgement for *this* project:

- **Reject `temperature=1.0` for the RAG path.** That is Google's general/
  thinking default; this server does grounded, citation-faithful extraction,
  which needs *low* temperature (~0.2). `server.js` currently sends no
  sampling options (Ollama/model defaults); if we add them they must be
  RAG-tuned, not the blog's creative-mode values.
- **`num_ctx`:** the blog's 32768 is reasonable for `gemma4:e4b` (fits 16GB
  with headroom); keep it model-specific via `CHAT_NUM_CTX` — do NOT pair a
  large ctx with a CPU-offloaded model (qwen32b/gemma4:26b) or latency
  balloons. Default stays 12288.
- **Thinking mode** adds latency/verbosity; not desirable for grounded
  factual extraction — leave off.
- Gemma 4 supports the `system` role (older Gemma didn't), so the existing
  faithfulness system prompt works as-is.

No local model fully matches NotebookLM (Gemini) on questions the manuals
only partially cover — that gap is what `RAG_GROUNDING=augmented` narrows by
letting the model add clearly-tagged general expertise on top of cited
sources. `strict` keeps maximum provenance at the cost of depth.

`CHAT_NUM_CTX` must stay large enough for `TOP_K_RESULTS` chunks + a long
question + the answer; the original abstention/quality failures traced to
Ollama's small default context silently truncating the retrieved sources.

### 5.4 Planned: Gemini `!deep` cloud hybrid (NOT YET IMPLEMENTED)

The local stack has now been exhaustively tuned (every retrieval lever in
§5.1 plus multi-query expansion in `expandQueries`). One failure class
remains, evidence-isolated: **contrastive standards questions** where one
mechanism's vocabulary dominates the question (e.g. *"is TAS the same as a
PSFP stream gate, does it have an IPV?"*). For these, first-stage retrieval
never recalls the TAS-defining `§12.29 Gate Parameter Table` / `§8.6.9`
chunks because **resolving the disambiguation requires knowing
TAS ⇒ scheduled-traffic / 802.1Qbv / Clause 12.29** — domain knowledge the
local generation model **and** the local query-expansion model don't have,
so neither generation nor expansion can bridge it. The same wrong-cite
pattern reproduced across `gemma4:e4b`, `qwen2.5-coder:32b`, with and
without clause-bounded chunking, with and without query expansion (see
[NEXT-STEPS.md](NEXT-STEPS.md) for the benchmark trail).

NotebookLM/Gemini gets this class right because Gemini has the standard's
domain knowledge plus very large context. Hence the planned hybrid:

| Profile | Backend | Privacy | Cost |
|---|---|---|---|
| `<col>` (fast, default) | local `gemma4:e4b` | fully private | free |
| `<col>!deep` | **Gemini paid key** (e.g. 2.x Pro) | no-training data terms | per-token |
| `<col>!gflash` (optional) | Gemini free-tier (Flash) | **used by Google for improvement — non-private** | free, rate-limited |

Implementation is contained — the existing `resolveModel` already supports
arbitrary profiles; only the LLM generation call needs a Gemini provider
keyed off the profile (embeddings/retrieval/grounding/citations are
unchanged and stay local). New env: `GEMINI_API_KEY`,
`GEMINI_API_KEY_DEEP`, `GEMINI_MODEL`, `GEMINI_MODEL_DEEP`.

Status: **deliberately not implemented yet.** It crosses the local/private
line the project was built around. Enable only as a conscious decision.

## 6. One-Time Machine Provisioning

Run from Windows PowerShell.

### 6.1 WSL and GPU prerequisites

```powershell
wsl -l -v
wsl -e bash -lc "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"
```

### 6.2 Install WSL packages

Install `zstd` (required by the Ollama installer):

```powershell
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y zstd"
```

Install Ollama:

```powershell
wsl -e bash -lc "curl -fsSL https://ollama.com/install.sh | sh"
```

Install Docker Engine + Compose plugin in Ubuntu 24.04:

```powershell
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg"
wsl -e bash -lc "sudo install -m 0755 -d /etc/apt/keyrings"
wsl -e bash -lc "[ -f /etc/apt/keyrings/docker.gpg ] && sudo rm -f /etc/apt/keyrings/docker.gpg || true"
wsl -e bash -lc "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --batch --yes -o /etc/apt/keyrings/docker.gpg"
wsl -e bash -lc "sudo chmod a+r /etc/apt/keyrings/docker.gpg"
wsl -e bash -lc "ARCH=$(dpkg --print-architecture); CODENAME=$(. /etc/os-release && echo $VERSION_CODENAME); echo \"deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${CODENAME} stable\" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null"
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
```

Start Docker in WSL:

```powershell
wsl -e bash -lc "sudo service docker start"
```

### 6.3 Passwordless sudo (required for autostart)

The startup, stop, and restart scripts run inside a **non-interactive**
`wsl -e bash -lc` session, so any `sudo` that prompts for a password will hang.
`scripts/ensure-services.sh`, `stop-local-llm.ps1`, and `restart-local-llm.ps1`
fall back to non-`sudo` `docker` when passwordless sudo is unavailable, but
unattended logon autostart and `sudo service docker start` require it. Either:

- Add the user to the `docker` group (avoids `sudo` for docker entirely):

  ```powershell
  wsl -e bash -lc "sudo usermod -aG docker $USER"
  ```

  (Applies on the next WSL login.)

- Or grant passwordless sudo for the relevant commands via `visudo`.

### 6.4 PDF extraction prerequisite

The extractor (`scripts/extract-pdfs.ps1`) builds a Python venv. Install the
venv package once in WSL:

```powershell
wsl -e bash -lc "sudo apt-get install -y python3-venv"
```

The lightweight backend (`pymupdf4llm`, `pypdf`) installs into that venv
automatically on first run. Optionally `pip install docling` into
`scripts/extract/.venv` for best-quality table/layout extraction.

### 6.5 Install MCP server dependencies

The editor MCP integration (`.vscode/mcp.json`) runs
`node scripts/rag-mcp/index.js`, which needs its npm dependencies installed:

```powershell
.\scripts\wsl-run.ps1 "cd scripts/rag-mcp && npm install"
```

(or run `npm install` in `scripts/rag-mcp` from any shell). The RAG server's
own dependencies install automatically — the `rag-server` container runs
`npm install` on start.

## 7. Model Provisioning

From repository root:

```powershell
.\scripts\wsl-run.ps1 "chmod +x scripts/bootstrap-models.sh && ./scripts/bootstrap-models.sh"
```

This script pulls:

- `llama3.1:8b-instruct-q8_0`
- `qwen2.5-coder:32b-instruct-q4_K_M`
- `deepseek-r1:14b`
- `nomic-embed-text`

## 8. Autostart After Machine Reboot

A Windows Startup folder launcher is installed by:

```powershell
.\scripts\install-startup-launcher.ps1
```

This places a `.cmd` file in:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start-local-llm.cmd
```

On every Windows logon, that launcher calls `scripts\start-local-llm.ps1` which
runs `scripts/ensure-services.sh` in WSL. That script:

1. Starts the Docker daemon in WSL if not already running.
2. Starts the Ollama service in WSL if not already running.
3. Runs `docker compose up -d` for all three containers.
4. Waits for each health endpoint to become reachable.
5. Holds the WSL session open (`exec sleep infinity`) so WSL — and therefore
   Docker — stays alive and reachable from Windows. The launcher process
   therefore stays resident by design; this also means running
   `start-local-llm.ps1` from an interactive terminal will not return.

An alternative Task Scheduler method is available via
`scripts\register-autostart-task.ps1` but requires admin elevation, and the
created task runs with limited rights — passwordless sudo (§6.3) is required
for it to start Docker unattended.

## 9. Stack Startup and Validation

```powershell
.\scripts\start-local-llm.ps1            # start + wait for health (then blocks)
.\scripts\stop-local-llm.ps1             # stop all containers
.\scripts\restart-local-llm.ps1          # restart all
.\scripts\restart-local-llm.ps1 open-webui   # restart a single service
```

Validate endpoints directly:

```powershell
wsl -e bash -lc "curl -fsS http://127.0.0.1:8000/api/v1/heartbeat"
wsl -e bash -lc "curl -fsS http://127.0.0.1:3000/health"
wsl -e bash -lc "curl -I http://127.0.0.1:8080"
wsl -e bash -lc "sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
```

Expected result:

- Chroma heartbeat returns JSON.
- RAG `/health` returns `{ "ok": true, ... }`.
- Open WebUI responds on `http://localhost:8080`.
- All three containers are `Up`.

## 10. Open WebUI First-Run Access

1. Open `http://localhost:8080` in a browser on Windows.
2. On first launch, create the admin account.
3. In the model selector, choose a pulled Ollama model, or a RAG collection
   (`ieee`, `amd`, `rag-active`) after adding the OpenAI connection — see
   README "PDF RAG — Query from the browser".

## 11. Data Operations

Create one subfolder per collection under `data/` and drop PDFs into it
(e.g. `data/ieee/`, `data/amd/`). Avoid storing the same document under two
filenames in one collection — dedup is keyed on file name, so duplicates are
indexed twice.

Extract PDFs into page-tagged sidecars (recommended — enables table-aware
chunks and page-level citations), then ingest:

```powershell
.\scripts\extract-pdfs.ps1 amd
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/collections/amd/ingest"
```

Without the extract step, ingest still works via flat `pdf-parse` text but
loses page citations and table structure. Re-run `extract-pdfs.ps1` after
adding/changing PDFs (unchanged files skip; `-Force` overrides). Ingest only
processes new/changed files.

Query example:

```powershell
wsl -e bash -lc "curl -fsS -H 'Content-Type: application/json' -d '{\"query\":\"summarize the architecture\",\"collection\":\"amd\",\"topK\":5}' http://127.0.0.1:3000/query"
```

If `RAG_API_KEY` is set, add `-H "Authorization: Bearer <key>"` to every call
except `/health`.

## 12. Operational Notes

- Autostart on logon is handled by the Startup folder launcher (§8).
- Container-level auto-recovery is handled by `restart: unless-stopped`.
- Startup ordering between containers is enforced by `condition: service_healthy`.
- First-run `AUTO_INGEST` embeds every PDF in every collection sequentially and
  can take a while for large document sets; the `rag-server` healthcheck has a
  generous `start_period`/retry budget to tolerate this.
- If `rag-server` fails at startup, inspect logs:

  ```powershell
  .\scripts\wsl-run.ps1 "sudo docker compose logs --tail=200 rag-server"
  ```

- If Ollama embedding calls fail, verify Ollama:

  ```powershell
  wsl -e bash -lc "ollama --version && curl -fsS http://127.0.0.1:11434/api/tags"
  ```

### 12.1 LAN access to Open WebUI (WSL2 mirrored mode)

Open WebUI is localhost-only by default. Exposing it to other LAN devices
under `networkingMode=mirrored` requires opening **two independent firewall
layers** — inbound LAN traffic clears the Windows Defender Firewall on the
physical adapter, then the Hyper-V firewall on the WSL vNIC. Both default to
blocking inbound. Run as Administrator:

```powershell
New-NetFirewallRule -DisplayName "Open WebUI 8080" -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 8080 -Profile Any
New-NetFirewallHyperVRule -Name "OpenWebUI-8080" -DisplayName "Open WebUI 8080" `
  -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol TCP -LocalPorts 8080 -Action Allow
```

Gotchas:

- **Validate from a separate device**, never the Windows host itself: in
  mirrored mode the host connecting to its own mirrored LAN IP does not loop
  into WSL (only `127.0.0.1` has the localhost relay), so a host-side test of
  `http://<lan-ip>:8080` fails even when LAN access is correct.
- The service binds `0.0.0.0:8080` inside WSL already; this is purely a
  firewall matter, not a binding/config one.
- DHCP lease IP changes on reconnect — use a router reservation for stability.
- Same two-rule pattern exposes any other port. Do not expose `3000`
  (RAG server) without first setting `RAG_API_KEY` — it is unauthenticated.

Full step-by-step (finding the LAN IP, profile/guest-SSID troubleshooting)
is in README → "Access Open WebUI from other devices on your LAN".

## 13. Configuration for Other Machines

Keep these invariant unless intentionally changed:

1. WSL Ubuntu release (`24.04`) and Docker/Ollama installation method.
2. Chroma image (`chromadb/chroma:0.5.5`) and `/api/v1` usage in `server.js`
   (coupled — see §4).
3. Collection folder convention (`data/<name>`, `data/<name>/.rag-cache/`
   for extraction sidecars, `storage/chroma`).
4. Default ports (`11434`, `8000`, `3000`, `8080`).
5. Open WebUI persistent storage path (`storage/open-webui`).
6. Run `scripts\install-startup-launcher.ps1` on each new machine for logon
   autostart, and ensure passwordless sudo or `docker` group membership (§6.3).
7. Run `npm install` in `scripts/rag-mcp` on each new machine for the MCP
   integration (§6.5).
8. Run `scripts\extract-pdfs.ps1` after adding/changing PDFs so ingest gets
   table-aware chunks and page citations (the `.venv` and `.rag-cache/` are
   machine-local and rebuildable — safe to gitignore).

**Relocation:** The project is relocatable. All PowerShell scripts derive the
repo path dynamically from `$PSScriptRoot` — no hardcoded paths. Clone the repo
to any drive or directory and the scripts work without modification.
