#!/usr/bin/env bash
set -euo pipefail

echo "Ensuring Ollama service is running..."
if ! pgrep -x ollama >/dev/null 2>&1; then
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  sleep 2
fi

echo "Pulling required models..."
ollama pull llama3.1:8b-instruct-q8_0
ollama pull qwen2.5-coder:32b-instruct-q4_K_M
ollama pull deepseek-r1:14b
# Gemma 4 (Google, 2026): e4b ~9.6GB fits 16GB VRAM fully; 26b is MoE-A4B
# ~18GB so it CPU-offloads on 16GB but only 4B params are active per token.
ollama pull gemma4:e4b
ollama pull gemma4:26b
# Qwen 3.6 (Alibaba, Apr 2026, Apache 2.0): 27b is dense (~17GB at Q4_K_M,
# minor spillover on 16GB cards), 35b-a3b is MoE with 3B active per token
# (~24GB at Q4_K_M, ~8GB spillover but MoE keeps tokens/s competitive).
# Hybrid-thinking, 256K-1M context. Strong coding + reasoning vs qwen2.5.
ollama pull qwen3.6:27b
ollama pull qwen3.6:35b-a3b
# Nemotron 3 Nano (NVIDIA, 2026): nemotron_h_moe hybrid Transformer-Mamba MoE,
# 30B total / 3B active, reasoning-tuned. ~24GB at Q4_K_M; partial system-RAM
# spillover on 16GB cards. Designed for grounded retrieval / agentic reasoning.
ollama pull nemotron-3-nano:30b-a3b-q4_K_M
ollama pull nomic-embed-text
# Qwen3 Embedding 0.6B (~640MB): small + fast code-aware embedder used by
# the source-tree RAG path (extract-code.py). Co-resides comfortably with
# the chat models on a 16 GB GPU. Swap to qwen3-embedding:4b or :8b for
# better recall on nuanced architectural questions once GPU headroom
# allows (env var EMBEDDING_MODEL_CODE on rag-server controls which is
# used). Per-collection routing via EMBED_CODE_COLLECTIONS env.
ollama pull qwen3-embedding:0.6b

echo "Installed models:"
ollama list
