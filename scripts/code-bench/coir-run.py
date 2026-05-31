#!/usr/bin/env python3
"""
Score our pipeline's embedder (qwen3-embedding:0.6b via Ollama) on the
official CoIR benchmark.

CoIR evaluates an embedder by:
  1. Embedding the corpus (~14k Python functions for CSN-Python).
  2. Embedding the test queries (~1k docstring-style queries).
  3. Computing cosine similarity, ranking, scoring nDCG@10.

This bypasses the full rag-server pipeline (query expansion, Chroma,
reranker) and measures the embedder alone -- which is what the
leaderboard at archersama.github.io/coir scores. That makes the number
directly comparable to published entries.

Tasks (small to large, picked for an end-to-end pass under 1h):
  - cosqa      (~500 queries vs 20k corpus)
  - codesearchnet-python (~1k queries vs 14k corpus)
  - codetrans-contest (small, fast)

Usage:
  python coir-run.py <task1> [<task2> ...]
  python coir-run.py cosqa CodeSearchNet-python

If no args, runs cosqa as a smoke test.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import List, Dict

import numpy as np
from tqdm.auto import tqdm

import coir
from coir.data_loader import get_tasks
from coir.evaluation import COIR


OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("COIR_EMBED_MODEL", "qwen3-embedding:0.6b")
BATCH = int(os.environ.get("COIR_BATCH", "32"))
MAX_RETRIES = 3


def _embed_batch(texts: List[str]) -> np.ndarray:
    """One POST to Ollama /api/embed with a batch of strings."""
    body = json.dumps({"model": MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA}/api/embed",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                d = json.loads(r.read())
            return np.asarray(d["embeddings"], dtype=np.float32)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Ollama embed failed after {MAX_RETRIES} retries: {last_err}")


class OllamaEmbedder:
    """CoIR-compatible adapter for an Ollama-served embedder.

    CoIR's DRES wrapper calls:
      encode_queries(queries: List[str], batch_size=N, **kwargs) -> np.ndarray
      encode_corpus(corpus: List[Dict[str, str]], batch_size=N, **kwargs) -> np.ndarray

    `corpus` items have keys `text` (the doc) and `title` (often empty).
    We concatenate them the same way as the BEIR reference adapter.
    """

    def _encode(self, texts: List[str], batch_size: int, desc: str) -> np.ndarray:
        out: List[np.ndarray] = []
        for i in tqdm(range(0, len(texts), batch_size), desc=desc, unit="batch"):
            chunk = texts[i:i + batch_size]
            # Empty strings break Ollama; substitute a single space.
            chunk = [t if t and t.strip() else " " for t in chunk]
            out.append(_embed_batch(chunk))
        return np.vstack(out)

    def encode_queries(self, queries: List[str], batch_size: int = BATCH,
                       **kwargs) -> np.ndarray:
        return self._encode(queries, batch_size, desc="queries")

    def encode_corpus(self, corpus: List[Dict[str, str]],
                      batch_size: int = BATCH, **kwargs) -> np.ndarray:
        texts = []
        for doc in corpus:
            title = (doc.get("title") or "").strip()
            text = (doc.get("text") or "").strip()
            if title and text:
                texts.append(f"{title}\n{text}")
            else:
                texts.append(title or text)
        return self._encode(texts, batch_size, desc="corpus")


def main(argv: List[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        # Small smoke task: CoIR's cosqa is ~500 queries vs 20k corpus,
        # ~10 minutes end-to-end at qwen3-0.6b throughput.
        tasks_to_run = ["cosqa"]
    else:
        tasks_to_run = args

    print(f"Model:  {MODEL} via {OLLAMA}")
    print(f"Batch:  {BATCH}")
    print(f"Tasks:  {tasks_to_run}")
    print()

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "coir-results", MODEL.replace(":", "-").replace("/", "-"))
    os.makedirs(out_dir, exist_ok=True)

    print(f"Loading {len(tasks_to_run)} task(s) from HuggingFace...")
    tasks = get_tasks(tasks=tasks_to_run)
    if not tasks:
        print("ERROR: no tasks loaded (HuggingFace dataset names case-sensitive)",
              file=sys.stderr)
        return 2

    print(f"Loaded tasks: {list(tasks.keys())}")
    for name, (corpus, queries, qrels) in tasks.items():
        print(f"  {name}: corpus={len(corpus)} queries={len(queries)} qrels={len(qrels)}")
    print()

    model = OllamaEmbedder()
    runner = COIR(tasks=tasks, batch_size=BATCH)
    results = runner.run(model, output_folder=out_dir)

    print()
    print("=" * 60)
    print(f"Results for {MODEL}")
    print("=" * 60)
    for task_name, metrics in results.items():
        ndcg10 = metrics["NDCG"].get("NDCG@10", "?")
        recall10 = metrics["Recall"].get("Recall@10", "?")
        print(f"  {task_name:40} nDCG@10={ndcg10:.4f}  R@10={recall10:.4f}")
    print()
    print(f"Per-task JSON: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
