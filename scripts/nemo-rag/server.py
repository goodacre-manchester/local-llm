#!/usr/bin/env python3
"""
NVIDIA Nemotron RAG sidecar — embed + rerank in one process.

Loads two HF models into GPU memory and exposes a tiny HTTP API for the
rag-server (app/server.js) to call when a collection is opted in to the
Nemotron retrieval stack via env-var routing.

  GET  /health                   -> {"ok":true,"embed":"<id>","rerank":"<id>","dim":<int>}
  POST /embed   {"inputs":[str], "kind":"query"|"document"}  -> {"embeddings":[[float,...]]}
  POST /rerank  {"query":str, "documents":[str]}             -> {"scores":[float]}

Why in-process HF transformers, not vLLM:
  Phase 3a (Nemotron Parse) proved vLLM's chat-completions API doesn't apply
  the bundled GenerationConfig for these Nemotron multimodal models, producing
  token-collapse output across vLLM v0.14.1 and v0.21.0. Embed/rerank don't
  hit that specific pathway, but the underlying brittleness class is the same
  family — so we apply the same lesson: load via AutoModel + trust_remote_code
  in a small Python sidecar that mirrors the existing scripts/reranker/server.py
  pattern. Reuses the .venv-nemo built for extract-nemo.py (transformers 5.x,
  torch cu128, Blackwell-ready).

Models:
  EMBED_MODEL_ID   nvidia/llama-nemotron-embed-vl-1b-v2  (bi-encoder, 2048-dim)
  RERANK_MODEL_ID  nvidia/llama-nemotron-rerank-1b-v2    (LLM-as-reranker, raw logits)
Combined VRAM ~7-8 GB in bfloat16.

Env overrides:
  NEMO_RAG_PORT          8009
  NEMO_RAG_DEVICE        cuda:0
  EMBED_MODEL_ID         nvidia/llama-nemotron-embed-vl-1b-v2
  RERANK_MODEL_ID        nvidia/llama-nemotron-rerank-1b-v2
  EMBED_MAX_LENGTH       8192
  RERANK_MAX_LENGTH      512
  HF_HOME                ~/.cache/huggingface (point at storage/nemo-parse/hf-cache
                         to share cache with Parse; or storage/nemo-rag/hf-cache)
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoModel, AutoProcessor, AutoTokenizer, AutoModelForSequenceClassification

EMBED_MODEL_ID    = os.environ.get("EMBED_MODEL_ID",    "nvidia/llama-nemotron-embed-vl-1b-v2")
RERANK_MODEL_ID   = os.environ.get("RERANK_MODEL_ID",   "nvidia/llama-nemotron-rerank-1b-v2")
DEVICE            = os.environ.get("NEMO_RAG_DEVICE",   "cuda:0")
PORT              = int(os.environ.get("NEMO_RAG_PORT", "8009"))
EMBED_MAX_LENGTH  = int(os.environ.get("EMBED_MAX_LENGTH",  "8192"))
RERANK_MAX_LENGTH = int(os.environ.get("RERANK_MAX_LENGTH", "512"))

# flash_attention_2 is recommended in the model card but is hard to install on
# Blackwell (sm_120) and is optional. eager/sdpa works fine for our scale.
ATTN_IMPL = os.environ.get("NEMO_RAG_ATTN", "sdpa")


def _log(msg: str):
    print(f"[nemo-rag] {msg}", flush=True)


_embed_model = None
_rerank_model = None
_rerank_tokenizer = None
_embed_dim: int | None = None


def _load_embed():
    global _embed_model, _embed_dim
    _log(f"loading embed model {EMBED_MODEL_ID} on {DEVICE} (attn={ATTN_IMPL}) ...")
    t = time.time()
    _embed_model = AutoModel.from_pretrained(
        EMBED_MODEL_ID,
        trust_remote_code=True,
        torch_dtype=torch.bfloat16,
        attn_implementation=ATTN_IMPL,
    ).to(DEVICE).eval()
    # Set text-only max length per the model card.
    try:
        _embed_model.processor.p_max_length = EMBED_MAX_LENGTH
    except Exception as exc:
        _log(f"WARNING: couldn't set processor.p_max_length: {exc}")
    _log(f"embed model loaded in {time.time() - t:.1f}s")


def _load_rerank():
    global _rerank_model, _rerank_tokenizer
    _log(f"loading rerank model {RERANK_MODEL_ID} on {DEVICE} ...")
    t = time.time()
    _rerank_tokenizer = AutoTokenizer.from_pretrained(
        RERANK_MODEL_ID,
        trust_remote_code=True,
        padding_side="left",
    )
    if _rerank_tokenizer.pad_token is None:
        _rerank_tokenizer.pad_token = _rerank_tokenizer.eos_token
    _rerank_model = AutoModelForSequenceClassification.from_pretrained(
        RERANK_MODEL_ID,
        trust_remote_code=True,
        torch_dtype=torch.bfloat16,
    ).to(DEVICE).eval()
    if _rerank_model.config.pad_token_id is None:
        _rerank_model.config.pad_token_id = _rerank_tokenizer.eos_token_id
    _log(f"rerank model loaded in {time.time() - t:.1f}s")


def _embed_inputs(inputs: list[str], kind: str) -> list[list[float]]:
    """kind = 'query' or 'document'. Returns L2-normalized embeddings."""
    global _embed_dim
    if not inputs:
        return []
    with torch.inference_mode():
        if kind == "query":
            emb = _embed_model.encode_queries(inputs)
        else:
            emb = _embed_model.encode_documents(texts=inputs)
        emb = emb / (emb.norm(p=2, dim=-1, keepdim=True) + 1e-12)
    if _embed_dim is None:
        _embed_dim = int(emb.shape[-1])
        _log(f"embed dim = {_embed_dim}")
    return emb.to(torch.float32).cpu().tolist()


_RERANK_TEMPLATE = "question:{q} \n \n passage:{p}"


def _rerank_pairs(query: str, documents: list[str]) -> list[float]:
    if not documents:
        return []
    texts = [_RERANK_TEMPLATE.format(q=query, p=d) for d in documents]
    batch = _rerank_tokenizer(
        texts,
        padding=True,
        truncation=True,
        return_tensors="pt",
        max_length=RERANK_MAX_LENGTH,
    )
    batch = {k: v.to(DEVICE) for k, v in batch.items()}
    with torch.inference_mode():
        logits = _rerank_model(**batch).logits
    return logits.view(-1).to(torch.float32).cpu().tolist()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {
                "ok": True,
                "embed":  EMBED_MODEL_ID,
                "rerank": RERANK_MODEL_ID,
                "dim":    _embed_dim,
                "device": DEVICE,
            })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            self._send(400, {"error": f"bad request: {exc}"})
            return

        try:
            if self.path == "/embed":
                inputs = req.get("inputs") or []
                kind   = str(req.get("kind", "document")).lower()
                if kind not in ("query", "document"):
                    self._send(400, {"error": "kind must be 'query' or 'document'"})
                    return
                if not isinstance(inputs, list):
                    self._send(400, {"error": "inputs must be a list of strings"})
                    return
                inputs = [str(x) for x in inputs]
                embs = _embed_inputs(inputs, kind)
                self._send(200, {"embeddings": embs, "kind": kind, "dim": _embed_dim})
                return

            if self.path == "/rerank":
                query = str(req.get("query", ""))
                docs  = req.get("documents") or []
                if not query or not isinstance(docs, list) or not docs:
                    self._send(400, {"error": "query and non-empty documents[] required"})
                    return
                docs = [str(d) for d in docs]
                scores = _rerank_pairs(query, docs)
                self._send(200, {"scores": scores})
                return

            self._send(404, {"error": "not found"})

        except Exception as exc:
            traceback.print_exc()
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


def main():
    if DEVICE.startswith("cuda") and not torch.cuda.is_available():
        sys.exit("CUDA not available -- set NEMO_RAG_DEVICE=cpu to override "
                 "(but inference will be impractically slow).")
    _load_embed()
    _load_rerank()
    _log(f"ready on :{PORT}  (embed={EMBED_MODEL_ID}, rerank={RERANK_MODEL_ID})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
