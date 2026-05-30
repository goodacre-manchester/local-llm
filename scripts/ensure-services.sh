#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() {
  printf '[ensure-services] %s\n' "$*"
}

has_sudo() {
  sudo -n true >/dev/null 2>&1
}

start_docker() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    log "Docker daemon already reachable"
    return
  fi

  if has_sudo; then
    log "Starting Docker service"
    sudo service docker start >/dev/null 2>&1 || true
  else
    log "Docker is not reachable and passwordless sudo is unavailable"
    exit 1
  fi
}

start_ollama_if_needed() {
  if curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    log "Ollama API reachable"
    return
  fi

  log "Starting Ollama service"
  if command -v systemctl >/dev/null 2>&1 && has_sudo; then
    sudo systemctl start ollama >/dev/null 2>&1 || true
  fi

  if ! curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    nohup ollama serve >/tmp/ollama-startup.log 2>&1 &
    sleep 3
  fi
}

compose_cmd() {
  if has_sudo; then
    sudo docker compose "$@"
  else
    docker compose "$@"
  fi
}

wait_for_http() {
  local url="$1"
  local timeout_secs="$2"
  local elapsed=0

  while (( elapsed < timeout_secs )); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  return 1
}

main() {
  cd "$ROOT_DIR"

  start_docker
  start_ollama_if_needed

  log "Bringing up containers"
  # No service names → brings up EVERY service in docker-compose.yml. Newly
  # added services (reranker, sd-webui, …) auto-join without editing this
  # script. `up -d` is idempotent on already-running containers, so this is
  # safe to run on every boot.
  compose_cmd up -d

  log "Waiting for critical service health endpoints"
  # Only the user's critical path is waited on (chat must be reachable):
  #   - chroma + rag-server + open-webui  → blocked
  #   - reranker                         → started, not waited (5+ min first
  #     boot installs torch; rag-server degrades to fused order if it's down)
  #   - sd-webui                         → started, not waited (10+ min first
  #     boot clones A1111 + installs torch/xformers; blocking here would
  #     delay chat access. Image generation works as soon as it finishes
  #     booting in the background; users can verify with
  #     `curl http://127.0.0.1:7860/sdapi/v1/options`.)
  wait_for_http "http://127.0.0.1:8000/api/v1/heartbeat" 120 || {
    log "Chroma health check failed"
    exit 1
  }

  wait_for_http "http://127.0.0.1:3000/health" 180 || {
    log "RAG server health check failed; restarting rag-server"
    compose_cmd restart rag-server
    wait_for_http "http://127.0.0.1:3000/health" 120 || exit 1
  }

  wait_for_http "http://127.0.0.1:8080/health" 300 || {
    log "Open WebUI health check failed; restarting open-webui"
    compose_cmd restart open-webui
    wait_for_http "http://127.0.0.1:8080/health" 180 || exit 1
  }

  log "All critical local LLM services are healthy"

  # Non-blocking status probe of the background-starting services so the
  # autostart log makes it obvious whether image-gen is already usable.
  if curl -fsS --max-time 3 http://127.0.0.1:7860/sdapi/v1/options >/dev/null 2>&1; then
    log "sd-webui (image generation): ready"
  else
    log "sd-webui (image generation): still booting in background — try again in a few minutes"
    log "  first-boot install can take 10+ min; tail with: docker compose logs -f sd-webui"
  fi
  if curl -fsS --max-time 3 http://127.0.0.1:8008/health >/dev/null 2>&1; then
    log "reranker: ready"
  else
    log "reranker: still booting in background (rag-server degrades gracefully if it's not ready yet)"
  fi
  if curl -fsS --max-time 3 http://127.0.0.1:8888/healthz >/dev/null 2>&1; then
    log "searxng (web search): ready"
  else
    log "searxng (web search): still booting in background (Open WebUI Web Search will fail until ready)"
  fi

  # Keep this process alive so WSL remains Running and Docker stays reachable
  # from Windows.  WSL auto-terminates when no user-space session is open (even
  # with systemd + Docker active).  start-local-llm.ps1 waits for this script
  # to exit, so holding here keeps the wsl.exe session — and WSL — alive.
  log "Holding WSL session open to keep containers reachable from Windows"
  exec sleep infinity
}

main "$@"
