#!/usr/bin/env python3
"""
Tiny cross-encoder reranker sidecar for the RAG server.

POST /rerank  {"query": "...", "documents": ["...", ...]}
  -> {"scores": [float, ...]}   # higher = more relevant, aligned to input order
GET  /health  -> {"ok": true, "model": "..."}

The RAG server (server.js) over-fetches + dedupes candidates, then calls this
to reorder them so the right clause beats vocabulary-colliding distractors
(e.g. IEEE 802.1Q TAS "transmission gate" vs PSFP "stream gate"). Best-effort
on the caller side: if this service is down, RAG degrades to fused order.

Runs CPU-only by design (no GPU container plumbing); a base reranker over a
few dozen ~1KB candidates is ~1-3s, acceptable for the deep path.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sentence_transformers import CrossEncoder

MODEL = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-base")
PORT = int(os.environ.get("PORT", "8008"))
MAX_DOC_CHARS = int(os.environ.get("RERANK_MAX_DOC_CHARS", "2000"))

print(f"[reranker] loading {MODEL} (CPU) ...", flush=True)
_model = CrossEncoder(MODEL, max_length=512, device="cpu")
print("[reranker] ready", flush=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
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
            self._send(200, {"ok": True, "model": MODEL})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/rerank":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            query = str(req.get("query", ""))
            docs = req.get("documents") or []
            if not query or not isinstance(docs, list) or not docs:
                self._send(400, {"error": "query and non-empty documents[] required"})
                return
            pairs = [[query, str(d)[:MAX_DOC_CHARS]] for d in docs]
            scores = _model.predict(pairs, batch_size=16)
            self._send(200, {"scores": [float(s) for s in scores]})
        except Exception as exc:  # never 5xx-crash the caller's pipeline silently
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    print(f"[reranker] listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
