#!/usr/bin/env bash
# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Smart Classroom Linux setup entry point (task 0.A.3 / 0.B.2).
# Replaces setup-smart-classroom.ps1. For now it provisions the Node.js toolchain
# the UI build needs; venv bootstrap / pip install / config prompts are layered on
# in 0.A.3.
#
# Usage:
#   ./setup.sh
set -euo pipefail

export HTTP_PROXY="${HTTP_PROXY:-${http_proxy:-}}"
export HTTPS_PROXY="${HTTPS_PROXY:-${https_proxy:-}}"
export NO_PROXY="${NO_PROXY:-${no_proxy:-}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; exit 1; }

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    [[ -n "$(ss -ltnH "sport = :${port}" 2>/dev/null)" ]]
  else
    (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null
  fi
}

# Prints the first free TCP port in [start, start+49]; returns 1 if none is free.
find_free_port() {
  local port="$1" last=$(( $1 + 49 ))
  while (( port <= last )); do
    if ! port_in_use "$port"; then
      echo "$port"
      return 0
    fi
    port=$(( port + 1 ))
  done
  return 1
}

# Prints the TCP port a PID is listening on; returns 1 if it cannot be determined.
pid_listen_port() {
  local port
  port="$(ss -ltnpH 2>/dev/null | awk -v p="pid=$1," 'index($0, p) { n = split($4, a, ":"); print a[n]; exit }')"
  [[ -n "$port" ]] && echo "$port"
}


if git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "Initializing git submodules (edge-ai-libraries, performance-tools)…"
  git -C "$SCRIPT_DIR" submodule sync -- edge-ai-libraries
  git -C "$SCRIPT_DIR" submodule update --init edge-ai-libraries
  git -C "$SCRIPT_DIR/edge-ai-libraries" sparse-checkout init --no-cone
  git -C "$SCRIPT_DIR/edge-ai-libraries" sparse-checkout set --no-cone \
    '/*' \
    '!/*' \
    '/microservices/' \
    '!/microservices/*/' \
    '!/microservices/isolation-forest' \
    '!/microservices/random-forest' \
    '/microservices/audio-analyzer/' \
    '/microservices/model-download/'
  git -C "$SCRIPT_DIR" submodule update --init performance-tools
else
  log "Not a git checkout — skipping submodule init."
fi

log "Checking Node.js toolchain for the UI build…"
NODE_SETUP="${SCRIPT_DIR}/scripts/node-setup.sh"
[[ -f "$NODE_SETUP" ]] || die "Missing ${NODE_SETUP}"
bash "$NODE_SETUP"

ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ENV_EXAMPLE" ]]; then
    log "Creating .env from .env.example…"
    cp -n "$ENV_EXAMPLE" "$ENV_FILE"
  else
    log "⚠️  Missing ${ENV_EXAMPLE} — skipping .env bootstrap; create .env manually."
  fi
else
  log ".env already exists — leaving it untouched."
fi


CONFIG_YAML="${SCRIPT_DIR}/config.yaml"
if [[ -f "$CONFIG_YAML" ]] && command -v python3 >/dev/null 2>&1; then
  DIARIZATION_ENABLED="$(python3 - "$CONFIG_YAML" <<'PY'
import sys, yaml
try:
    with open(sys.argv[1]) as f:
        cfg = yaml.safe_load(f) or {}
    print(bool(((cfg.get("models") or {}).get("asr") or {}).get("diarization")))
except Exception:
    print("False")
PY
)"
  HF_TOKEN_SET="false"
  if [[ -f "$ENV_FILE" ]] && grep -qE '^HF_TOKEN=.+' "$ENV_FILE"; then
    HF_TOKEN_SET="true"
  fi
  if [[ "$DIARIZATION_ENABLED" == "True" && "$HF_TOKEN_SET" == "false" ]]; then
    log "⚠️  models.asr.diarization is enabled in config.yaml but HF_TOKEN is not set in .env."
    log "    Diarization (pyannote) and some gated text-gen models require a Hugging Face"
    log "    token with access granted to the relevant model repo(s). Set HF_TOKEN=<token>"
    log "    in ${ENV_FILE} before running 'docker compose up', or diarization/model"
    log "    downloads will fail with a 401 at startup."
  fi
else
  log "⚠️  Could not check config.yaml for diarization — if you use speaker diarization or"
  log "    gated text-gen models, set HF_TOKEN in .env manually."
fi

# --- Keep startup-models.yaml/.gpu.yaml in sync with config.yaml ------------
if command -v python3 >/dev/null 2>&1; then
  log "Syncing startup-models.yaml/.gpu.yaml model names from config.yaml…"
  (cd "$SCRIPT_DIR" && python3 -m utils.sync_startup_models --write) \
    || log "⚠️  Could not sync startup-models.yaml — check it manually against config.yaml."
else
  log "⚠️  python3 not found — skipping startup-models.yaml sync; check it manually against config.yaml."
fi

if command -v python3 >/dev/null 2>&1; then
  log "Deriving COMPOSE_PROFILES from config.yaml features:…"
  (cd "$SCRIPT_DIR" && python3 -m utils.compose_profiles --write-env) \
    || log "⚠️  Could not derive COMPOSE_PROFILES automatically — set it manually in .env before 'docker compose up'."
else
  log "⚠️  python3 not found — skipping COMPOSE_PROFILES derivation; set it manually in .env before 'docker compose up'."
fi

# --- Require Intel GPU support for Content Search reranking ----------------
COMPOSE_FILES=(-f docker/docker-compose.yml)
[[ -d /dev/dri ]] || die "Intel GPU device directory /dev/dri is required for Content Search reranking."
RENDER_GID="$(getent group render | cut -d: -f3 || true)"
[[ "$RENDER_GID" =~ ^[0-9]+$ ]] || die "Host 'render' group not found; GPU access requires it."
if grep -qE '^RENDER_GID=' "$ENV_FILE" 2>/dev/null; then
  sed -i "s/^RENDER_GID=.*/RENDER_GID=${RENDER_GID}/" "$ENV_FILE"
else
  printf 'RENDER_GID=%s\n' "$RENDER_GID" >> "$ENV_FILE"
fi
log "GPU required: /dev/dri detected; set RENDER_GID=${RENDER_GID} in .env."
COMPOSE_FILES+=(-f docker/docker-compose.gpu.yml)

# --- Image source: config.yaml REGISTRY (true = Docker Hub, false = local build) ---
REGISTRY="true"
if command -v python3 >/dev/null 2>&1; then
  REGISTRY="$(cd "$SCRIPT_DIR" && python3 -m utils.registry_mode)" \
    || die "Could not resolve REGISTRY from config.yaml (must be true or false) — see the error above."
else
  log "⚠️  python3 not found — defaulting to REGISTRY=true (pull Docker Hub images)."
fi

if [[ "$REGISTRY" == "true" ]]; then
  log "REGISTRY=true: pulling audio-analyzer, model-downloader and metrics-collector images from Docker Hub."
else
  log "REGISTRY=false: building audio-analyzer, model-downloader and metrics-collector images locally."
  for build_ctx in edge-ai-libraries/microservices/audio-analyzer edge-ai-libraries/microservices/model-download performance-tools; do
    [[ -f "${SCRIPT_DIR}/${build_ctx}/docker/Dockerfile" ]] \
      || die "Missing ${build_ctx}/docker/Dockerfile — initialize the git submodules (git submodule update --init) or set REGISTRY: true in config.yaml."
  done
  COMPOSE_FILES+=(-f docker/docker-compose.local-build.yml)
fi

# --- Sanity-check the compose config before building anything --------------
log "Validating docker compose config (docker compose config --services)…"
(cd "$SCRIPT_DIR" && docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" config --services >/dev/null) \
  || die "docker compose config failed — check COMPOSE_PROFILES/.env and the compose files above before retrying."

# --- Build + start the stack -------------------------------------------------
if [[ "$REGISTRY" == "true" ]]; then
  log "Pulling Docker Hub images (docker compose pull --ignore-buildable)…"
  (cd "$SCRIPT_DIR" && docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" pull --ignore-buildable) \
    || die "docker compose pull failed — check network/proxy access to Docker Hub, or set REGISTRY: false in config.yaml to build locally."
fi

log "Starting the stack (docker compose up -d --build; the app and video-analytics images are always built locally)…"
(cd "$SCRIPT_DIR" && docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" up -d --build) \
  || die "docker compose up failed — check the build/service logs above."

# --- Wait for the app to become healthy -------------------------------------
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-1900}"
HEALTH_POLL_INTERVAL_S=10
log "Waiting for the app container to become healthy (timeout ${HEALTH_TIMEOUT_S}s — first run can be slow while models download)…"
elapsed=0
app_status=""
while (( elapsed < HEALTH_TIMEOUT_S )); do
  app_status="$(docker inspect -f '{{.State.Health.Status}}' smart-classroom-app 2>/dev/null || echo "unknown")"
  if [[ "$app_status" == "healthy" ]]; then
    break
  fi
  sleep "$HEALTH_POLL_INTERVAL_S"
  elapsed=$(( elapsed + HEALTH_POLL_INTERVAL_S ))
done

if [[ "$app_status" == "healthy" ]]; then
  ACTIVE_PROFILES="$(grep -E '^COMPOSE_PROFILES=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"
  log "✅ App is healthy: http://localhost:8000/"
  log "   Active profiles: ${ACTIVE_PROFILES:-none}"
else
  log "⚠️  App did not become healthy within ${HEALTH_TIMEOUT_S}s (last status: ${app_status})."
  log "    First-run model downloads can take a long time — it may still finish; re-check with:"
  log "      docker compose -f docker/docker-compose.yml ps"
  log "    Inspect logs with:"
  log "      docker compose -f docker/docker-compose.yml logs -f app"
  log "      docker compose -f docker/docker-compose.yml logs -f model-downloader audio-analyzer ovms"
  die "App startup failed; resolve the errors above before using the UI."
fi

CONTENT_SEARCH_ENABLED="$(python3 - "$CONFIG_YAML" <<'PY'
import sys, yaml
with open(sys.argv[1], encoding="utf-8-sig") as f:
    features = (yaml.safe_load(f) or {}).get("features")
print(features is None or any(
    features.get(name, {}).get("enabled", False)
    for name in ("topic_segmentation", "content_search", "qa")
))
PY
)"
if [[ "$CONTENT_SEARCH_ENABLED" == "True" ]]; then
  log "Waiting for Content Search (embeddings, reranker and database; first run downloads models)…"
  content_search_ready="false"
  while (( elapsed < HEALTH_TIMEOUT_S )); do
    if docker exec smart-classroom-app python -c \
      "import urllib.request; opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); opener.open('http://127.0.0.1:8000/api/v1/system/health', timeout=15)" \
      >/dev/null 2>&1; then
      content_search_ready="true"
      break
    fi
    sleep "$HEALTH_POLL_INTERVAL_S"
    elapsed=$(( elapsed + HEALTH_POLL_INTERVAL_S ))
  done
  if [[ "$content_search_ready" != "true" ]]; then
    log "Inspect Content Search readiness: http://localhost:8000/api/v1/system/health"
    log "Inspect service errors: docker logs --tail 200 smart-classroom-app"
    die "Content Search did not become ready within ${HEALTH_TIMEOUT_S}s."
  fi
  log "✅ Content Search is ready: http://localhost:8000/api/v1/system/health"
fi

# --- Start the local UI (Vite dev server) -----------------------------------
UI_DIR="${SCRIPT_DIR}/ui"
UI_LOG="${UI_DIR}/vite-dev.log"
UI_PID_FILE="${UI_DIR}/vite-dev.pid"
UI_PORT="${UI_PORT:-5173}"

if [[ -d "$UI_DIR" ]]; then
  log "Installing UI dependencies (npm install)…"
  (cd "$UI_DIR" && npm install) || die "npm install failed in ${UI_DIR}."

  if [[ -f "$UI_PID_FILE" ]] && kill -0 "$(cat "$UI_PID_FILE")" 2>/dev/null; then
    log "UI dev server already running (PID $(cat "$UI_PID_FILE")) — leaving it as is."
    if RUNNING_UI_PORT="$(pid_listen_port "$(cat "$UI_PID_FILE")")"; then
      log "✅ UI: http://localhost:${RUNNING_UI_PORT}/"
    else
      log "⚠️  Could not determine the UI port."
    fi
  else
    UI_PORT_REQUESTED="$UI_PORT"
    UI_PORT="$(find_free_port "$UI_PORT_REQUESTED")" \
      || die "No free UI port in ${UI_PORT_REQUESTED}-$(( UI_PORT_REQUESTED + 49 )) — set UI_PORT to a free port."
    if [[ "$UI_PORT" != "$UI_PORT_REQUESTED" ]]; then
      log "⚠️  UI port ${UI_PORT_REQUESTED} is already in use — using ${UI_PORT} instead."
    fi
    log "Starting UI dev server in the background (vite --port ${UI_PORT})…"
    (cd "$UI_DIR" && { nohup ./node_modules/.bin/vite --host 127.0.0.1 --port "$UI_PORT" --strictPort >"$UI_LOG" 2>&1 & echo $! >"$UI_PID_FILE"; })
    log "✅ UI: http://localhost:${UI_PORT}/"
  fi
else
  log "⚠️  ${UI_DIR} not found — skipping UI startup."
fi

log "✅ setup complete."
