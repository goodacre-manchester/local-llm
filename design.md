# Local LLM + PDF RAG — Runbook

This document is the runbook for rebuilding the stack on a new machine from a fresh clone of this repository. It describes the running configuration and the steps to provision it.

The stack is **local-only**: no cloud LLM calls, no telemetry egress. Everything runs on the operator's hardware.

## 1. Architecture

Hybrid Windows + WSL host:

- **Windows 11** is the operator host (browser, terminals, IDE).
- **WSL2 Ubuntu 24.04** runs Ollama and Docker workloads.
- **Docker** in WSL runs five services: `chroma`, `rag-server`, `reranker`, `open-webui`, `sd-webui`.
- **Ollama** runs natively in WSL as a systemd service.

Data flow:

1. Each immediate subfolder of `./data/` is a **collection** (`data/ieee/`, `data/amd/`, etc.). `./data` is bind-mounted read-only into `rag-server` as `/data`.
2. PDFs are extracted to page-tagged JSON sidecars under `data/<collection>/.rag-cache/`. Each picture in the PDF is also rendered as a PNG under `data/<collection>/.rag-images/<pdf-stem>/` and captioned by a VLM.
3. Sidecar text and picture-caption text are chunked, embedded (Ollama `nomic-embed-text`), and stored in Chroma (one Chroma collection per folder, `rag_<folder>`).
4. Query requests embed the question, retrieve top-k candidates via hybrid search (dense + BM25 + RRF), rerank with a cross-encoder, and feed the ranked chunks to a chat model for a grounded, cited answer.
5. Image generation is orthogonal to RAG. Any Open WebUI reply can be sent as a prompt to `sd-webui` via the per-message image button or the chat-input Integrations toggle.

## 2. Hardware and OS

Required:

- Windows 11 with WSL2 (Ubuntu 24.04).
- NVIDIA GPU with ≥16 GB VRAM (validated on RTX 5070 Ti, Blackwell `sm_120`). Older Ada / Ampere cards work; smaller VRAM is tight on the deep generation profile.
- ≥50 GB free disk for models + checkpoints + storage volumes (~95 GB if you pull the full optional model library).
- LAN connectivity for the operator to reach `http://localhost:8080` on Windows; optionally a single firewall rule to expose four ports to other devices.

Verify after install:

```powershell
wsl -l -v
wsl -e bash -lc "uname -r && . /etc/os-release && echo $PRETTY_NAME"
wsl -e bash -lc "ollama --version"
wsl -e bash -lc "sudo docker version --format '{{.Server.Version}}'"
wsl -e bash -lc "sudo docker compose version"
wsl -e bash -lc "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"
wsl -e bash -lc "sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi"
```

The final command confirms the NVIDIA Container Toolkit is wired into the Docker daemon — GPU passthrough into containers is required by `sd-webui`.

## 3. Repository Layout

```
.
├── docker-compose.yml              # five compose services + the deprecated nemo-parse
├── app/
│   ├── package.json
│   └── server.js                   # RAG server (ingest, hybrid retrieval, grounded chat)
├── scripts/
│   ├── start-local-llm.ps1         # Windows entry point — start everything + wait for health
│   ├── stop-local-llm.ps1
│   ├── restart-local-llm.ps1
│   ├── ensure-services.sh          # WSL-side start + health-wait
│   ├── wsl-run.ps1                 # PowerShell → WSL command helper
│   ├── install-startup-launcher.ps1 # places launcher in Windows Startup folder
│   ├── bootstrap-models.sh         # pulls all Ollama models
│   ├── download-sd-models.ps1      # pulls the default SDXL checkpoint
│   ├── sd-webui-entrypoint.sh      # ensures Blackwell-capable PyTorch in sd-webui
│   ├── open-webui-entrypoint.sh    # surgical upstream-bug patch for Web Search
│   ├── extract-pdfs.ps1            # entry for PyMuPDF4LLM extractor
│   ├── extract-nemo.ps1            # entry for Nemotron Parse extractor
│   ├── extract-code.ps1            # entry for source-tree extractor
│   ├── dump-sidecar-md.ps1         # renders human-readable .md views of sidecars
│   ├── extract/
│   │   ├── extract.py              # PyMuPDF4LLM-backed extractor
│   │   ├── extract-nemo.py         # NVIDIA Nemotron Parse v1.2 extractor
│   │   ├── extract-code.py         # source-tree extractor (tree-sitter, git link/in-place)
│   │   ├── caption-images.py       # VLM captioner (qwen3-vl:8b, v3-ctx prompt)
│   │   ├── run-caption-pipeline.sh # unified caption pipeline
│   │   ├── rerender-pictures.py    # re-render PNGs from stored bboxes
│   │   ├── build-picture-review.py # emit per-collection picture-review.html
│   │   ├── invalidate-captions.py  # clear VLM fields to trigger re-captioning
│   │   ├── clean_vlm_caption.py    # regex meta-stripper used by caption-images.py
│   │   ├── sanitize_collapse.py    # token-collapse sanitiser for Parse output
│   │   ├── dump-sidecar-md.py      # sidecar → readable .md
│   │   ├── .venv/                  # PyMuPDF4LLM venv (gitignored)
│   │   └── .venv-nemo/             # Nemotron + VLM tooling venv (gitignored)
│   ├── reranker/
│   │   ├── server.py               # bge-reranker-base sidecar
│   │   └── requirements.txt
│   ├── nemo-rag/server.py          # optional manual Nemotron embed+rerank sidecar
│   ├── rag-mcp/                    # stdio MCP server (editor integration)
│   └── benchmark/                  # regression-guard prompt suite
├── searxng/
│   └── settings.yml                # curated engine list for Web Search
├── data/
│   └── <collection>/
│       ├── *.pdf                   # input documents
│       ├── .rag-cache/             # extracted sidecar JSON
│       ├── .rag-images/            # per-PDF picture PNGs
│       ├── .rag-md/                # human-readable sidecar previews
│       └── picture-review.html     # browser-friendly captioned-figure index
├── storage/                        # persisted runtime state (gitignored)
│   ├── chroma/                     # Chroma vector store
│   ├── open-webui/                 # Open WebUI SQLite + uploads
│   ├── reranker/                   # reranker venv + HF model cache
│   ├── sd-webui/                   # A1111 workspace + SDXL checkpoints + pip cache
│   ├── nemo-parse/                 # Parse HF cache + caption pipeline logs
│   ├── open-webui-playwright/      # persisted Chromium for Playwright web loader
│   └── code-cache/                 # cloned repos for "link-mode" code collections
├── docs/archive/                   # historical hand-off docs (reference only)
├── design.md                       # this file
├── README.md                       # project description + capabilities + usage
└── NEXT-STEPS.md                   # open polish items + acknowledged limitation
```

## 4. Runtime Services

`docker-compose.yml` defines five services that start together, plus a deprecated `nemo-parse` entry retained for documentation only.

### chroma

- Image: `chromadb/chroma:0.5.5` (pinned — `app/server.js` uses `/api/v1`, which 0.6+ removes).
- Port: `8000:8000` published to the host.
- Persistence: `./storage/chroma:/chroma/chroma`.
- Healthcheck: `GET /api/v1/heartbeat`.

### rag-server

- Image: `node:20-slim`.
- Network: `host` (binds `0.0.0.0:3000` in WSL).
- Mounts: `./app:/app`, `./data:/data:ro`.
- Startup: `npm install && node server.js`.
- Depends on `chroma` healthy.
- Key env (see `.env.example` for the full list):
  - `PORT=3000`
  - `OLLAMA_HOST=http://127.0.0.1:11434`
  - `CHROMA_URL=http://127.0.0.1:8000`
  - `EMBEDDING_MODEL=nomic-embed-text`
  - `CHAT_MODEL=gemma4:e4b` (fast profile)
  - `CHAT_MODEL_DEEP=nemotron-3-nano:30b-a3b-q4_K_M` (deep profile)
  - `CHUNK_SIZE=1000`, `CHUNK_OVERLAP=200`, `CHUNK_CLAUSE_DEPTH=3`
  - `TOP_K_RESULTS=8`, `TOP_K_DEEP=15`
  - `CHAT_NUM_CTX=12288`, `CHAT_NUM_CTX_DEEP=12288`
  - `RAG_GROUNDING=augmented`
  - `QUERY_EXPANSION=true`, `MAX_SUBQUERIES=5`
  - `AUTO_INGEST=true`
  - `RERANKER_URL=http://127.0.0.1:8008/rerank`
  - `RAG_API_KEY=` (empty → unauthenticated)

### reranker

- Image: `python:3.12-slim`.
- Network: `host`, port `8008`.
- Mounts: `./scripts/reranker:/app`, `./storage/reranker:/cache` (persists venv + HF model cache).
- Model: `BAAI/bge-reranker-base`, CPU-only.
- First-boot installs torch + downloads the model (`start_period: 600s`).
- The rag-server falls back to fused order if the reranker is unavailable, so it is not a strict dependency.

### open-webui

- Image: `ghcr.io/open-webui/open-webui:main`.
- Network: `host`, port `8080`.
- Persistence: `./storage/open-webui:/app/backend/data`.
- Auth: `WEBUI_AUTH=true` — first-run wizard creates the admin account.
- Image-generation env pre-pointed at `sd-webui`:
  - `ENABLE_IMAGE_GENERATION=true`
  - `IMAGE_GENERATION_ENGINE=automatic1111`
  - `AUTOMATIC1111_BASE_URL=http://127.0.0.1:7860`
  - `IMAGE_GENERATION_MODEL=Juggernaut-XL_v9_RunDiffusionPhoto_v2`
  - `IMAGE_SIZE=1024x1024`, `IMAGE_STEPS=30`
- Depends on `rag-server` healthy.

### sd-webui

- Image: `ghcr.io/ai-dock/stable-diffusion-webui:latest-cuda`.
- Port: `7860:7860` published to the host.
- Entrypoint override: `scripts/sd-webui-entrypoint.sh` — pip-installs `torch 2.11.0+cu128` (Blackwell `sm_120` capable) into A1111's venv before A1111 starts. Idempotent.
- Launch args (`WEBUI_ARGS`): `--api --listen --cors-allow-origins=* --no-half-vae --opt-sdp-attention`.
  - `--api` exposes `/sdapi/v1/*` (the surface Open WebUI calls).
  - `--listen` binds `0.0.0.0` inside the container.
  - `--opt-sdp-attention` uses PyTorch's native scaled-dot-product attention (replaces the prebuilt xformers, which is ABI-incompatible with the upgraded torch).
- Auth: `WEB_ENABLE_AUTH=false` (LAN gating is on `open-webui`).
- GPU: NVIDIA via `deploy.resources.reservations.devices`.
- Volumes:
  - `./storage/sd-webui:/workspace` — ai-dock workspace.
  - `./storage/sd-webui/storage/stable_diffusion/models/{ckpt,lora,vae}/` → A1111's model dirs.
  - `./storage/sd-webui/pip-cache:/root/.cache/pip` — cu128 wheel cache.
- Healthcheck: `GET /sdapi/v1/options` with `start_period: 900s` (first boot clones A1111 and installs PyTorch).

### searxng

- Image: `searxng/searxng:latest` (Docker Hub).
- Port: `8888:8080` published to the host.
- Mounts:
  - `./searxng:/etc/searxng:rw` — host-controlled `settings.yml` (curated engine list).
- Env:
  - `SEARXNG_BASE_URL=http://127.0.0.1:8888/`
  - `SEARXNG_SECRET=${SEARXNG_SECRET:-}` — read from `.env`. Generate once with `openssl rand -hex 32`.
- Healthcheck: `GET /healthz` with `start_period: 60s`.
- Engine curation (`searxng/settings.yml`):
  - General web: Google, Bing, DuckDuckGo, Startpage, Mojeek, Brave.
  - News: Google News, Bing News, DuckDuckGo News.
  - Reference: Wikipedia, Wikidata, arXiv, GitHub, Stack Overflow, Google Scholar, Semantic Scholar.
  - Disabled: Yandex, Qwant, image / video / map / shopping / file / torrent / music engines.
- `search.formats: [html, json]` is mandatory — Open WebUI's Web Search calls `/search?format=json` and gets 403s without it.
- `server.limiter: false` — bot-detection limiter off for trusted local-LAN use.
- Not LAN-exposed by design (chroma + reranker are also internal-only; see §9).
- No `depends_on` to/from other services. Open WebUI degrades to "no sources" on SearXNG outage rather than blocking startup.

### nemo-parse (deprecated, kept in compose)

vLLM-served Nemotron Parse v1.2 entry. Not part of the autostart chain (no `restart` policy); `docker compose up` does not bring it up. The live Parse extractor instead runs in-process via HF transformers in `scripts/extract/.venv-nemo` (it produces stable output where the vLLM chat-completions API does not apply Parse's bundled `GenerationConfig`). Compose entry retained as the revival hook if a future vLLM release fixes that wiring.

### Open WebUI entrypoint wrapper

`open-webui` uses an entrypoint wrapper (`scripts/open-webui-entrypoint.sh`) bind-mounted to `/usr/local/bin/open-webui-entrypoint.sh`. On every container start the wrapper applies one surgical patch to `/app/backend/open_webui/retrieval/web/utils.py`, then execs the image's `start.sh`. The patch removes a duplicate `allow_redirects` keyword argument in `SafeWebBaseLoader._fetch` that otherwise raises `TypeError: ClientSession.get() got multiple values for keyword argument 'allow_redirects'` on every URL fetch and silently kills Web Search ("no sources found" replies). The script header records the upstream bug + the removal criterion (delete the wrapper when upstream fixes it).

Web Search also requires `WEB_LOADER_ENGINE=playwright` for cookie-walled / JS-rendered sites (Guardian, BBC, most EU/UK news). Upstream `start.sh` runs `playwright install chromium` when that env is set; the ~600 MB Chromium cache is persisted via the `./storage/open-webui-playwright:/root/.cache/ms-playwright` host volume so container recreates don't re-download.

Tuned defaults wired in `docker-compose.yml` (override OW defaults which are too narrow for useful grounding):

| Env var | Value | Why |
|---|---|---|
| `WEB_SEARCH_RESULT_COUNT` | `10` | OW default `3` is too tight — extraction drops empty pages, leaving too few sources. |
| `WEB_SEARCH_CONCURRENT_REQUESTS` | `10` | Parallelise Playwright fetches. |
| `RAG_TOP_K` | `5` | Top-K chunks reaching the chat model (across PDF-RAG AND web-search). OW default `3` collapses inline citations to one source. |
| `WEB_SEARCH_DOMAIN_FILTER_LIST` | JSON exclude-list (`!youtube.com`, `!youtu.be`, `!merriam-webster.com`, `!dictionary.com`, `!quora.com`) | Skip sites that rank well but yield no usable article text. |
| `ENABLE_SEARCH_QUERY_GENERATION` | `true` | LLM rephrases user prompt into focused sub-queries via the Task Model. |

### Common notes

All six active services (`chroma`, `rag-server`, `reranker`, `open-webui`, `sd-webui`, `searxng`) use `restart: unless-stopped`. Startup ordering uses `depends_on: condition: service_healthy`. The Chroma image is pinned to `0.5.5` because `app/server.js` calls `/api/v1`, which is removed in 0.6+.

## 5. RAG Server API

`app/server.js` exposes:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Chroma heartbeat + active-collection summary. Always exempt from `RAG_API_KEY` auth. |
| `GET /sse` | SSE stream of ingest progress. |
| `GET /collections` | List collection folders with Chroma / ingest status. |
| `POST /collections/:name/ingest` | Ingest one collection. Body `{ "force": false }`. Unchanged files (by mtime) are skipped unless `force:true`. |
| `GET /active-collection`, `PUT /active-collection` | Read or switch the active collection. Switching does not re-index. |
| `POST /query` | Body `{ "query", "collection"?, "topK"? }` → top-k matched chunks. |
| `GET /v1/models` | OpenAI-compatible model list (`rag-active` + one entry per collection, both fast and `!deep`). |
| `POST /v1/chat/completions` | OpenAI-compatible RAG chat. `model` field selects the collection. Supports `stream`, `topK`. |

CORS is restricted to `localhost` / `127.0.0.1` browser origins. Non-browser clients (curl, the Open WebUI backend, the MCP server) are unaffected.

If `RAG_API_KEY` is set, all endpoints except `/health` require `Authorization: Bearer <key>` or `x-api-key`. The Open WebUI OpenAI-connection API key and the MCP server's `RAG_API_KEY` must match.

### Retrieval pipeline

1. **Extraction**: each PDF → `data/<col>/.rag-cache/<pdf>.json` with page/section-tagged blocks. Two backends:
   - PyMuPDF4LLM (`extract.py`) — CPU, seconds per PDF. Default for datasheets.
   - NVIDIA Nemotron Parse v1.2 (`extract-nemo.py`) — GPU, ~10 s/page warm. Layout-aware extraction; used for IEEE 802.1 standards.
2. **Picture extraction (Phase H)**: both backends emit `type:"picture"` blocks alongside text. PNGs are persisted under `data/<collection>/.rag-images/<pdf-stem>/`. Render-time bbox padding compensates for backend-specific bbox tightness.
3. **VLM captioning (Phase F)**: `caption-images.py` calls Ollama `qwen3-vl:8b` with the `v3-ctx` prompt on each picture, persists `vlm_description` + `vlm_description_raw`. The stripped caption becomes the chunk text for picture blocks.
4. **Clause-path sectioning**: every block's `section` is overwritten with its deepest active PDF bookmark (e.g. `12.29.1 The Gate Parameter Table`). Falls back to backend heuristic headings where the outline is silent.
5. **Ingest**: `server.js` chunks blocks with clause-bounded packing — text is never packed across a bookmark boundary truncated to `CHUNK_CLAUSE_DEPTH` levels. Embeddings via Ollama `nomic-embed-text` in batches of 64.
6. **Hybrid retrieval**: dense Chroma vector search + in-process BM25 fused with Reciprocal Rank Fusion (k=60).
7. **Dedupe + canonical preference**: near-duplicate chunks (normalised-signature collision) are collapsed, keeping the copy from the earliest `CANONICAL_PREFERENCE` source.
8. **Cross-encoder rerank**: top candidates rescored by `bge-reranker-base` via the reranker sidecar (best-effort; falls back to fused order on failure).
9. **Multi-query expansion**: the fast model decomposes the question into focused sub-queries (`MAX_SUBQUERIES=5`); union → rerank → top-K.
10. **Grounded generation**: faithfulness system prompt; inline `[n]` citations; abstention sentence when unsupported; appended **Sources** list (file, page, section); `citations[]` array in the non-stream JSON response.

### Generation profiles

Two profiles, switchable per request via the `model` field:

| Profile | Model | Approx latency | Use |
|---|---|---|---|
| fast (default) | `gemma4:e4b` | ~50 s | Day-to-day questions, lookups. GPU-resident. |
| deep | `nemotron-3-nano:30b-a3b-q4_K_M` | ~2–3 min | Hard technical questions. MoE 3B-active / 30B-total, ~24 GB at Q4_K_M (spills ~8 GB to system RAM on 16 GB cards). |

Request the deep profile by appending `!deep` to the collection: `amd!deep`, `ieee!deep`, `rag-active!deep`. `<collection>!<ollama:tag>` is a per-request literal override.

`/v1/models` advertises both variants per collection so they appear in Open WebUI's selector.

### Acknowledged local-only limitation

The local stack has been exhaustively tuned (every lever above plus multi-query expansion). One failure class remains, evidence-isolated: **contrastive standards questions** where one mechanism's vocabulary dominates the question (e.g. *"is TAS the same as a PSFP stream gate, does it have an IPV?"*). First-stage retrieval never recalls the TAS-defining `§12.29 Gate Parameter Table` / `§8.6.9` chunks because resolving the disambiguation requires knowing TAS ⇒ scheduled-traffic / 802.1Qbv / Clause 12.29 — domain knowledge the local generation model and the local query-expander don't have, so neither can bridge it. A `/query` probe with explicit clause wording returns §12.29 at rank 1; the chunks are indexed correctly, the failure is purely first-stage recall.

This is treated as a project constraint of the local-only footprint, not a bug to fix. The credible (but unwired) directions for closing it are: a stronger local generator/expander, or a per-corpus domain glossary fed into expansion. Practical workaround: re-ask the question with the defining clause number explicit (*"summarise Clause 12.29 — the Gate Parameter Table"*).

## 6. New-Machine Bring-Up

Run from Windows PowerShell at the repository root.

### 6.1 WSL + base packages

```powershell
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y zstd python3-venv ca-certificates curl gnupg"
```

### 6.2 Ollama (native in WSL)

```powershell
wsl -e bash -lc "curl -fsSL https://ollama.com/install.sh | sh"
```

### 6.3 Docker Engine + Compose plugin in WSL

```powershell
wsl -e bash -lc "sudo install -m 0755 -d /etc/apt/keyrings"
wsl -e bash -lc "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --batch --yes -o /etc/apt/keyrings/docker.gpg"
wsl -e bash -lc "sudo chmod a+r /etc/apt/keyrings/docker.gpg"
wsl -e bash -lc "ARCH=`$(dpkg --print-architecture); CODENAME=`$(. /etc/os-release && echo `$VERSION_CODENAME); echo \"deb [arch=`${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu `${CODENAME} stable\" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null"
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
wsl -e bash -lc "sudo service docker start"
```

### 6.4 Passwordless sudo

The autostart launcher runs the start script in a **non-interactive** `wsl -e bash -lc` session, so any `sudo` that prompts for a password will hang. Either:

```powershell
wsl -e bash -lc "sudo usermod -aG docker $env:USER"   # docker group → no sudo for docker
```

(applies on next WSL login), or grant passwordless sudo via `visudo`.

### 6.5 NVIDIA Container Toolkit

Required by `sd-webui`. On Docker Desktop this is bundled; on hand-installed `docker-ce` in WSL:

```powershell
wsl -e bash -lc "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
wsl -e bash -lc "curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list"
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit"
wsl -e bash -lc "sudo nvidia-ctk runtime configure --runtime=docker && sudo service docker restart"
```

Verify:

```powershell
wsl -e bash -lc "sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi"
```

### 6.6 Ollama models

```powershell
.\scripts\wsl-run.ps1 "chmod +x scripts/bootstrap-models.sh && ./scripts/bootstrap-models.sh"
```

Required for the RAG path:

- `gemma4:e4b` — fast generation profile.
- `nemotron-3-nano:30b-a3b-q4_K_M` — deep generation profile.
- `nomic-embed-text` — embedding model (PDF + general).
- `qwen3-embedding:0.6b` — code-aware embedder for source-tree RAG collections (per-collection routing via `EMBED_CODE_COLLECTIONS`).
- `qwen3-vl:8b` — VLM for figure captioning.

The script also pulls optional models for direct chat (`llama3.1:8b-instruct-q8_0`, `deepseek-r1:14b`, `gemma4:26b`, `qwen3.6:27b`, `qwen3.6:35b-a3b`). Edit it before running if you only want the required set (the optional library is ~95 GB).

### 6.7 SDXL checkpoint

```powershell
.\scripts\download-sd-models.ps1
```

Downloads `Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors` (~6.6 GB) into `storage/sd-webui/storage/stable_diffusion/models/ckpt/`. Idempotent. Drop additional `.safetensors` into the same folder for more checkpoints; A1111 picks them up on next boot or via **Settings → Reload UI**.

### 6.8 MCP server dependencies

```powershell
.\scripts\wsl-run.ps1 "cd scripts/rag-mcp && npm install"
```

### 6.9 Autostart launcher

```powershell
.\scripts\install-startup-launcher.ps1
```

Installs `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start-local-llm.cmd`, which runs `start-local-llm.ps1` on every Windows logon.

### 6.10 First start

```powershell
.\scripts\start-local-llm.ps1
```

Starts Docker + Ollama in WSL, then `docker compose up -d` for all five services. Waits for `chroma /api/v1/heartbeat`, `rag-server /health`, and `open-webui /health`. The slow first-boot services (`reranker` ~5 min for torch + model download; `sd-webui` ~10–15 min for A1111 source + PyTorch install) start in the background; the script logs `ready` / `still booting` after non-blocking probes.

The script holds the WSL session open with `exec sleep infinity` so Docker stays alive and reachable from Windows. Run via the Startup launcher (one-shot per logon) — running it from an interactive terminal does not return.

After the first start, every subsequent boot self-activates: logon → launcher → `ensure-services.sh` → `docker compose up -d` resurrects all containers via `restart: unless-stopped`.

## 7. Adding PDFs to a Collection

Drop PDFs under `data/<collection>/` (create the folder if new), then extract → caption → ingest.

### Choose the extractor

| Backend | Script | Cost | Use for |
|---|---|---|---|
| PyMuPDF4LLM | `extract-pdfs.ps1` | CPU, seconds per PDF | Datasheets, UGs, RFCs, textbooks. |
| NVIDIA Nemotron Parse v1.2 | `extract-nemo.ps1` | GPU, ~10 s/page warm | Layout-heavy standards. |

Sidecar shape is identical between backends; rag-server consumes them interchangeably. Both scripts skip unchanged PDFs by source mtime; `-Force` re-extracts.

First run of `extract-pdfs.ps1` builds `scripts/extract/.venv` and installs PyMuPDF4LLM + pypdf. Optionally `pip install docling` into `.venv` for higher-quality table/layout extraction (`extract.py` auto-detects it).

First run of `extract-nemo.ps1` builds `scripts/extract/.venv-nemo` and installs `torch 2.x+cu128`, transformers, accelerate, open_clip_torch, albumentations, pymupdf, pillow (~6 GB pip footprint) and downloads Parse model weights (~3.75 GB) into `storage/nemo-parse/hf-cache/` on first call. Free the GPU first if `sd-webui` is using it (`sudo docker compose stop sd-webui`).

### Caption pictures

Each extractor emits `type:"picture"` blocks with bounding boxes and persisted PNGs. The caption pipeline turns those into VLM descriptions stored on the same blocks.

```powershell
wsl -e bash -lc "bash /mnt/d/Projects/local-llm/scripts/extract/run-caption-pipeline.sh"
```

Captioning is resumable (skip check is `if pic.get("vlm_description") and not force`, which is falsy on empty strings — empty completions get retried automatically on re-run). Rate ~30–45 s/pic at ~6.5 GB peak VRAM on `qwen3-vl:8b`. The pipeline also regenerates the human-readable `.rag-md/` previews per collection.

After captioning, regenerate the per-collection picture review HTML:

```powershell
wsl -e bash -lc "python3 /mnt/d/Projects/local-llm/scripts/extract/build-picture-review.py /mnt/d/Projects/local-llm/data"
```

Open `data/<collection>/picture-review.html` in any browser to inspect every figure side-by-side with its caption and the raw VLM output.

### Per-extractor render-time bbox padding

| Backend | `BBOX_PAD_X_FRAC` | `BBOX_PAD_Y_FRAC` |
|---|---|---|
| `nemotron-parse-v1.2` | 0.02 | 0.00 |
| `pymupdf4llm` | 0.02 | 0.01 |

Padding is applied at render time only; the stored `bbox` is the unpadded detected extent. After changing padding, re-render PNGs in place without re-running Parse:

```powershell
wsl -e bash -lc "python3 /mnt/d/Projects/local-llm/scripts/extract/rerender-pictures.py /mnt/d/Projects/local-llm/data"
```

The script reads each sidecar's `backend` field and picks the corresponding defaults automatically.

### Ingest

```powershell
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/collections/<name>/ingest"
```

Ingest reads the sidecar if present and applies clause-bounded chunking. If no sidecar exists it falls back to flat `pdf-parse` text with a loud `[ingest] FLAT pdf-parse fallback (no pages/sections)` log line — re-extract and re-ingest to overwrite.

After captioning an existing collection (PDF mtimes unchanged), pass `{"force":true}` so ingest re-reads the sidecars:

```powershell
wsl -e bash -lc "curl -fsS -X POST -H 'Content-Type: application/json' -d '{\"force\":true}' http://127.0.0.1:3000/collections/<name>/ingest"
```

To make a new collection appear in Open WebUI's model selector, recreate `rag-server`:

```powershell
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose up -d rag-server --force-recreate"
```

### Tools for working with sidecars without re-extracting

| Tool | Use |
|---|---|
| `rerender-pictures.py <data_dir> [collection]` | Re-render every `type:"picture"` PNG from stored bbox. |
| `build-picture-review.py <data_dir> [collection]` | Emit `picture-review.html`. |
| `invalidate-captions.py <data_dir> [collection] [--only-prompt PID] [--dry]` | Clear VLM fields so the next captioner run regenerates them. |
| `caption-images.py <data_dir> <collection> [--force] [--restrip-only] [--sample N]` | Re-run captioning with various scopes. |
| `dump-sidecar-md.py <data_dir> <collection> [--force]` | Re-render `.rag-md/` previews. |

## 7a. Adding a source tree to a collection

Source-tree (code) collections use the same `data/<name>/` convention plus a different extractor backend (`scripts/extract/extract-code.py`). Two modes:

- **Link mode**: `data/<name>/.git-source.yaml` exists. The extractor shallow-clones the remote repo into `storage/code-cache/<name>/` (gitignored) at the configured ref/sparse-paths, then walks that clone. Each chunk's `github_url` is built from the resolved commit SHA so citations are stable. Updates: re-run the extractor — does `git fetch` + `reset --hard FETCH_HEAD`.
- **In-place mode**: no yaml; the extractor walks `data/<name>/` directly (skipping `.git/`, `.rag-cache/`, `.rag-images/`, dotfiles). Used for small repos or vendored snapshots.

`.git-source.yaml` shape:

```yaml
url: https://github.com/<owner>/<repo>.git
ref: <branch | tag | commit-SHA>     # optional; default = remote HEAD
sparse_paths:                         # optional; sparse-checkout to these subtrees
  - src/
include_globs:                        # optional; defaults cover common source files
  - "*.c"
exclude_globs:                        # optional; merged with built-in defaults
  - "**/contrib/**"
```

Per file the extractor:

1. Filters by include/exclude globs and skips binary files + files >1 MB (`EXTRACT_CODE_MAX_FILE_KB`).
2. For supported languages (`.c .h .cpp .hpp .py .go .rs .js .ts .java .rb`), chunks via `tree_sitter` at function/class/struct boundaries.
3. For unsupported languages and markdown/configs, falls back to line-window chunks (50 lines / 10-line overlap, char-capped at `CHUNK_MAX_CHARS=800` so rag-server's whitespace-collapse never fires).
4. Writes one sidecar per source file at `data/<name>/.rag-cache/<encoded-path>.json` with `backend="code-tree-sitter"`. Block fields: `text` (preserving whitespace), `type:"code"`, `section` (`<file path>::<function-or-chunk-id>`), `file_path`, `line_start`, `line_end`, `language`, `github_url` (link mode only).

Workflow:

```powershell
.\scripts\extract-code.ps1 nginx
# add the collection name to EMBED_CODE_COLLECTIONS in docker-compose.yml
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose up -d rag-server"
wsl -e bash -lc "curl -fsS -X POST -H 'Content-Type: application/json' -d '{\"force\":true}' http://127.0.0.1:3000/collections/nginx/ingest"
```

The rag-server treats `type:"code"` as plain text for chunking but the `file_path` / `line_start` / `github_url` metadata propagates to Chroma and surfaces in citations. **Per-collection embedder routing**: collections listed in `EMBED_CODE_COLLECTIONS` use `EMBEDDING_MODEL_CODE` (default `qwen3-embedding:0.6b`) instead of `EMBEDDING_MODEL` (`nomic-embed-text`). The choice is per-collection on a single rag-server.

For larger embedding quality at the cost of latency, swap `EMBEDDING_MODEL_CODE` to `qwen3-embedding:4b` or `:8b` and recreate rag-server. Re-ingest required (different embedding dimensions).

**Scale note**: nginx (~250k LoC, 395 source files after sparse-checkout) yields ~9k chunks and embeds in ~25-30 min on `qwen3-embedding:0.6b` with the embedder co-resident with a chat model. Larger repos should be scoped per-subtree as separate collections rather than one mega-collection to keep ingest tractable and retrieval focused.

## 8. Open WebUI Setup

1. Open `http://localhost:8080`.
2. Create the admin account on first run.
3. Connect to the RAG server so collections appear as models:
   - **Settings → Connections → OpenAI API → Add connection**
   - URL: `http://127.0.0.1:3000/v1`
   - API key: `local` (any non-empty value, unless `RAG_API_KEY` is set)
4. The model selector now lists `ieee`, `amd`, `rag-active`, plus `*!deep` variants.

If `RAG_API_KEY` is set, use that exact value as the connection's API key and also add it to the `env` block of `.vscode/mcp.json` for the MCP server.

## 9. LAN Access (Optional)

Four ports can be exposed to other devices on the LAN: open-webui (`8080`), sd-webui (`7860`), rag-server (`3000`), Ollama (`11434`). `chroma` (`8000`) and `reranker` (`8008`) stay internal.

### 9.1 WSL mirrored networking

`%USERPROFILE%\.wslconfig` must contain:

```ini
[wsl2]
networkingMode=mirrored
```

After changing this, `wsl --shutdown` then `.\scripts\start-local-llm.ps1`.

### 9.2 Bind Ollama to all interfaces

Ollama in WSL binds `127.0.0.1:11434` by default. To expose it, add a systemd drop-in (one-off):

```powershell
wsl -e bash -lc "sudo mkdir -p /etc/systemd/system/ollama.service.d && sudo tee /etc/systemd/system/ollama.service.d/override.conf > /dev/null <<'EOF'
[Service]
Environment=\"OLLAMA_HOST=0.0.0.0:11434\"
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama"
```

Verify `wsl -e bash -lc "ss -tlnp | grep 11434"` reports `*:11434`. The other three services already bind `0.0.0.0` inside WSL.

### 9.3 Windows Firewall

In PowerShell as Administrator:

```powershell
New-NetFirewallRule -DisplayName "local-llm stack (LAN)" `
  -Direction Inbound -Action Allow -Protocol TCP `
  -LocalPort 3000,7860,8080,11434 `
  -Profile Domain,Private -Enabled True
```

If the Wi-Fi profile is Public, either change it to Private (Windows **Settings → Network**) or add `Public` to the `-Profile` list.

WSL2 mirrored mode also maintains a separate Hyper-V Firewall layer. The standard rule above is sufficient for this stack. If a future change breaks LAN access, add the matching Hyper-V rule:

```powershell
New-NetFirewallHyperVRule -Name "local-llm-stack-lan" -DisplayName "local-llm stack (HV)" `
  -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol TCP -LocalPorts 3000,7860,8080,11434 -Action Allow
```

### 9.4 Validate from another device

Test from a peer machine, not the Windows host itself. In mirrored mode, the host connecting to its own LAN IP does not loop into WSL — `Test-NetConnection 192.168.0.x -Port 8080` reports `PingSucceeded=True, TcpTestSucceeded=False` even when LAN access is working.

| URL | Expected |
|---|---|
| `http://<lan-ip>:8080` | Open WebUI login screen |
| `http://<lan-ip>:7860` | A1111 UI |
| `http://<lan-ip>:11434/api/tags` | JSON model list |
| `http://<lan-ip>:3000/v1/models` | JSON RAG collection list |

### 9.5 Security on LAN

- Open WebUI gates LAN access with `WEBUI_AUTH=true`.
- rag-server and Ollama are unauthenticated by default. If the LAN is not fully trusted, set `RAG_API_KEY` on rag-server and omit `11434` from the firewall rule (Ollama has no built-in auth).
- sd-webui (`WEB_ENABLE_AUTH=false`) is local-only image generation; safe on a trusted LAN.
- SearXNG (`8888`) and the internal services `chroma` (`8000`) and `reranker` (`8008`) are intentionally NOT in the firewall rule. They serve Open WebUI / rag-server over `127.0.0.1` only.

## 10. Operational Reference

### 10.1 Start / stop / restart

```powershell
.\scripts\start-local-llm.ps1            # start + wait + block (autostart entry)
.\scripts\stop-local-llm.ps1
.\scripts\restart-local-llm.ps1          # restart all
.\scripts\restart-local-llm.ps1 open-webui   # restart one
```

### 10.2 Validation

```powershell
wsl -e bash -lc "curl -fsS http://127.0.0.1:8000/api/v1/heartbeat"
wsl -e bash -lc "curl -fsS http://127.0.0.1:3000/health"
wsl -e bash -lc "curl -I http://127.0.0.1:8080"
wsl -e bash -lc "curl -fsS http://127.0.0.1:7860/sdapi/v1/options >/dev/null && echo sd-webui: ok"
wsl -e bash -lc "curl -fsS http://127.0.0.1:8888/healthz && echo  searxng: ok"
wsl -e bash -lc "sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
```

### 10.3 Logs

```powershell
.\scripts\wsl-run.ps1 "sudo docker compose logs --tail=200 rag-server"
.\scripts\wsl-run.ps1 "sudo docker compose logs --tail=200 open-webui"
.\scripts\wsl-run.ps1 "sudo docker compose logs -f sd-webui"   # follow first boot
```

Caption pipeline log:

```powershell
wsl -e bash -lc "tail -f /mnt/d/Projects/local-llm/storage/nemo-parse/unified-caption.log"
```

### 10.4 Changing configuration

`docker-compose.yml` env changes need a container recreate (a plain `restart` does not pick them up):

```powershell
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && sudo docker compose up -d rag-server"
```

Open WebUI persists effective config in `storage/open-webui/webui.db`. Env vars seed values only when the DB doesn't already have them — once a value is changed in the Admin panel, env changes for that key are ignored until the DB is reset.

### 10.5 GPU coexistence

`qwen3-vl:8b` (captioning) and SDXL (image generation) both need GPU. On a 16 GB card:

| Loaded together | Approx VRAM | Notes |
|---|---|---|
| `gemma4:e4b` + SDXL | ~10 GB | Comfortable headroom |
| `gemma4:e4b` + SDXL mid-generation | ~13 GB | Fine, tight on activations |
| `nemotron-3-nano:30b` (deep) + SDXL | exceeds 16 GB | OOM if concurrent |
| `qwen3-vl:8b` captioning + anything else | tight | Stop sd-webui during long caption runs |

Practical pattern: image generation on top of fast-model replies; deep profile for hard contrastive questions; stop sd-webui before a full corpus caption run.

### 10.6 Image generation triggers

Open WebUI does not parse the LLM's reply for image-generation intent. Pick one of:

- **Integrations → Images** (chat input toggle) — auto-image per reply. Recommended.
- **Per-message image button** — on-demand per reply.
- **Tool calling** — Admin Panel → Settings → Models → Function Calling = Native, then per-model toggle Image Generation. Requires reliable native function calling from the model.

The model emitting DALL·E-shaped JSON or text like "I'll generate an image" does nothing on its own.

## 11. Configuration Invariants for Other Machines

Keep these invariant unless intentionally changed:

1. WSL Ubuntu 24.04, Docker via `docker-ce`, Ollama via the official installer.
2. Chroma image `chromadb/chroma:0.5.5` (coupled to `/api/v1` calls in `server.js`).
3. Collection folder convention: `data/<name>/` with PDFs, `.rag-cache/`, `.rag-images/`, `.rag-md/`.
4. Default ports: `11434` (Ollama), `8000` (Chroma), `3000` (rag-server), `7860` (sd-webui), `8008` (reranker), `8080` (open-webui), `8888` (searxng).
5. Storage paths: `storage/chroma`, `storage/open-webui`, `storage/open-webui-playwright`, `storage/reranker`, `storage/sd-webui`, `storage/nemo-parse`.
6. The `sd-webui-entrypoint.sh` host-mount + entrypoint wiring (Blackwell PyTorch upgrade).
7. The `open-webui-entrypoint.sh` host-mount + entrypoint wiring (Web Search bug patch — see service spec in §4).
8. Pre-pointed Open WebUI image-gen + Web Search env vars in compose (`ENABLE_IMAGE_GENERATION`, `AUTOMATIC1111_BASE_URL`, `ENABLE_WEB_SEARCH`, `WEB_SEARCH_ENGINE`, `SEARXNG_QUERY_URL`, `WEB_LOADER_ENGINE`, …).
9. Curated `searxng/settings.yml` engine list (committed; the matching `SEARXNG_SECRET` is in `.env`, gitignored).
10. Code-RAG env wiring on rag-server: `EMBEDDING_MODEL_CODE=qwen3-embedding:0.6b` and `EMBED_CODE_COLLECTIONS=<comma-separated>` list of collections that route through the code embedder. Source trees live under `data/<name>/` with either `.git-source.yaml` (link mode → cloned to `storage/code-cache/<name>/`) or as a direct in-place clone.

All PowerShell scripts derive the repo path from `$PSScriptRoot` — no hardcoded paths. The repo is relocatable across drives or directories.

Both extractor venvs (`.venv`, `.venv-nemo`) and all `.rag-cache/` directories are machine-local and rebuildable — safe to gitignore.
