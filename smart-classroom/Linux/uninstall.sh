#!/usr/bin/env bash
# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Smart Classroom Linux uninstall entry point. Reverses setup.sh: stops the UI dev
# server and the Docker stack, then removes its containers, network, volumes
# (including downloaded models), images and UI dependencies. Session and
# Content Search data in storage/ is kept unless --remove-storage is given.
# The source tree and .env are left in place.
#
# Usage:
#   ./uninstall.sh [--yes] [--remove-storage] [--prune-build-cache] [--remove-node]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
UI_DIR="${SCRIPT_DIR}/ui"

log()  { printf '\033[1;32m[uninstall]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[uninstall]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[uninstall]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./uninstall.sh [options]

Reverses setup.sh: stops the UI dev server and the Docker stack, then removes its
containers, network, volumes (including downloaded models), images and UI
dependencies. Session and Content Search data in storage/ is kept by default.
The source tree and .env are left in place.

Options:
  -y, --yes             Skip the confirmation prompt
  --remove-storage      Also delete storage/ (session and Content Search data)
  --prune-build-cache   Also run 'docker builder prune -af' (affects every Docker
                        project on this host)
  --remove-node         Also remove Node.js (NodeSource apt/dnf package or ~/.nvm),
                        including a Node.js you installed yourself
  -h, --help            Show this help
EOF
}

ASSUME_YES=0
REMOVE_STORAGE=0
PRUNE_BUILD_CACHE=0
REMOVE_NODE=0
while (( $# )); do
  case "$1" in
    -y|--yes)           ASSUME_YES=1 ;;
    --remove-storage)   REMOVE_STORAGE=1 ;;
    --prune-build-cache) PRUNE_BUILD_CACHE=1 ;;
    --remove-node)      REMOVE_NODE=1 ;;
    -h|--help)          usage; exit 0 ;;
    *)                  usage >&2; die "Unknown option: $1" ;;
  esac
  shift
done

STATUS=0

run_root() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    return 1
  fi
}

# Last value of KEY in .env, without inline comments or surrounding quotes.
env_value() {
  [[ -f "$ENV_FILE" ]] || return 0
  grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- \
    | sed -e 's/[[:space:]]*#.*$//' -e "s/^[\"']//" -e "s/[\"']\$//" || true
}

PROJECT="${COMPOSE_PROJECT_NAME:-$(env_value COMPOSE_PROJECT_NAME)}"
PROJECT="${PROJECT:-smart-classroom}"

STORAGE_PATH=""
if (( REMOVE_STORAGE )); then
  storage_dir="${STORAGE_DIR:-$(env_value STORAGE_DIR)}"
  storage_dir="${storage_dir:-../storage}"
  # Compose resolves relative bind-mount paths against docker/, next to the base compose file.
  [[ "$storage_dir" = /* ]] || storage_dir="${SCRIPT_DIR}/docker/${storage_dir}"
  STORAGE_PATH="$(realpath -m "$storage_dir")"
  if [[ "${SCRIPT_DIR}/" == "${STORAGE_PATH%/}/"* || "$STORAGE_PATH" == "$HOME" ]]; then
    die "Refusing to delete ${STORAGE_PATH}: it contains this checkout or your home directory. Fix STORAGE_DIR."
  fi
fi

docker_ok() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

stop_ui() {
  local pid_file="${UI_DIR}/vite-dev.pid" pid
  [[ -f "$pid_file" ]] || return 0
  pid="$(cat "$pid_file")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && grep -qa vite "/proc/${pid}/cmdline" 2>/dev/null; then
    log "Stopping UI dev server (PID ${pid})…"
    kill "$pid" 2>/dev/null || true
  fi
}

remove_stack() {
  # Compose interpolation requires RENDER_GID; its value is irrelevant for 'down'.
  export RENDER_GID="${RENDER_GID:-0}"
  local base=(-f "${SCRIPT_DIR}/docker/docker-compose.yml" -f "${SCRIPT_DIR}/docker/docker-compose.gpu.yml")
  local common=(-p "$PROJECT" --profile '*')
  [[ -f "$ENV_FILE" ]] && common+=(--env-file "$ENV_FILE")

  log "Removing containers, network, volumes and images (docker compose down -v --rmi all)…"
  docker compose "${common[@]}" "${base[@]}" down -v --rmi all --remove-orphans \
    || { warn "docker compose down failed — see the output above."; STATUS=1; }

  # REGISTRY=false swaps the Docker Hub images for locally built ones; remove those too.
  local local_build="${SCRIPT_DIR}/docker/docker-compose.local-build.yml"
  if [[ -f "$local_build" ]]; then
    docker compose "${common[@]}" "${base[@]}" -f "$local_build" down -v --rmi all --remove-orphans \
      || { warn "docker compose down (local-build) failed — see the output above."; STATUS=1; }
  fi

  local leftover
  leftover="$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT}")"
  if [[ -n "$leftover" ]]; then
    log "Removing leftover volumes…"
    xargs docker volume rm <<<"$leftover" >/dev/null \
      || { warn "Could not remove some volumes — they may still be in use."; STATUS=1; }
  fi
}

remove_storage() {
  if [[ ! -d "$STORAGE_PATH" ]]; then
    log "No storage directory at ${STORAGE_PATH} — nothing to remove."
    return 0
  fi
  log "Removing ${STORAGE_PATH}…"
  if rm -rf "$STORAGE_PATH" 2>/dev/null; then
    return 0
  fi
  log "Some files belong to the container user — retrying with sudo."
  run_root rm -rf "$STORAGE_PATH" \
    || { warn "Could not remove ${STORAGE_PATH}; run: sudo rm -rf '${STORAGE_PATH}'"; STATUS=1; }
}

remove_node() {
  local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
  if command -v dpkg >/dev/null 2>&1 && dpkg -s nodejs >/dev/null 2>&1; then
    log "Removing Node.js (apt)…"
    run_root apt-get purge -y nodejs || { warn "apt-get purge nodejs failed."; STATUS=1; }
    run_root rm -f /etc/apt/sources.list.d/nodesource.list /etc/apt/sources.list.d/nodesource.sources \
      /etc/apt/keyrings/nodesource.gpg /usr/share/keyrings/nodesource.gpg || true
  elif command -v rpm >/dev/null 2>&1 && rpm -q nodejs >/dev/null 2>&1; then
    log "Removing Node.js (dnf)…"
    run_root dnf remove -y nodejs || { warn "dnf remove nodejs failed."; STATUS=1; }
    run_root rm -f /etc/yum.repos.d/nodesource*.repo || true
  fi
  if [[ -d "$nvm_dir" ]]; then
    log "Removing ${nvm_dir}…"
    rm -rf "$nvm_dir"
    warn "Delete the nvm lines from your shell profile (for example ~/.bashrc)."
  fi
}

# --- Confirm -------------------------------------------------------------------
log "This will remove Smart Classroom from this host:"
log "  - UI dev server and ${UI_DIR}/node_modules"
log "  - Docker project '${PROJECT}': containers, network, volumes (downloaded models) and images"
if (( REMOVE_STORAGE )); then
  log "  - ${STORAGE_PATH} (session and Content Search data)"
else
  log "  - (kept) storage/ session and Content Search data; use --remove-storage to delete it"
fi
(( PRUNE_BUILD_CACHE )) && log "  - Docker build cache (all projects)"
(( REMOVE_NODE )) && log "  - Node.js and nvm"

if (( ! ASSUME_YES )); then
  [[ -t 0 ]] || die "Not an interactive terminal — rerun with --yes to confirm."
  read -r -p "Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]] || die "Aborted."
fi

# --- Uninstall -----------------------------------------------------------------
stop_ui

if docker_ok; then
  remove_stack
  if (( PRUNE_BUILD_CACHE )); then
    log "Pruning Docker build cache…"
    docker builder prune -af || { warn "docker builder prune failed."; STATUS=1; }
  fi
else
  warn "Docker is not installed or its daemon is unreachable — skipping container, volume and image cleanup."
  STATUS=1
fi

(( REMOVE_STORAGE )) && remove_storage

log "Removing UI dependencies…"
rm -rf "${UI_DIR}/node_modules" "${UI_DIR}/vite-dev.log" "${UI_DIR}/vite-dev.pid"

(( REMOVE_NODE )) && remove_node

# --- Wrap up -------------------------------------------------------------------
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
log "Kept ${ENV_FILE} (may contain HF_TOKEN) and the source tree."
log "To remove them too: rm -rf '${REPO_ROOT}'"
log "Check remaining Docker disk usage with: docker system df"
if (( STATUS )); then
  die "Uninstall finished with errors — see the warnings above."
fi
log "✅ uninstall complete."
