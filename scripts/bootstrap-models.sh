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
ollama pull nomic-embed-text

echo "Installed models:"
ollama list
