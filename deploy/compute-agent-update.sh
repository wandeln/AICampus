#!/usr/bin/env bash
# Compute-Agent-Update auf einem Compute-Server:
#
# Holt neue Commits und baut das Agent-Image NUR dann neu, wenn sich
# `compute_agent/` upstream geändert hat. AICampus-Backend-Updates, die
# den Agent-Code nicht berühren, bleiben damit wirkungslos (das ist die
# Norm, da der Agent seltener geändert wird als das Backend).
#
# Aufruf (von überall, cd'ed automatisch in den Repo-Root):
#   bash deploy/compute-agent-update.sh
#
# Voraussetzungen: Repo per Git (sparse: compute_agent + deploy),
# s. docs/installation.md §4.1. GPU-Overlay wird automatisch mitgebaut,
# wenn der Host den nvidia-Docker-Runtime registriert.
#
# Ignorierte lokale Dateien (deploy/.env, data/) bleiben unangetastet.
set -euo pipefail
cd "$(dirname "$0")/.."

git fetch origin

if [ "$(git rev-parse HEAD:compute_agent 2>/dev/null)" = \
     "$(git rev-parse origin/main:compute_agent)" ]; then
  echo "compute_agent/ aktuell — nichts zu tun."
  exit 0
fi

echo "compute_agent/ geändert — Agent-Image wird aktualisiert …"
git reset --hard origin/main

EXTRA=""
if docker info --format '{{json .Runtimes}}' | grep -q '"nvidia"'; then
  EXTRA="-f deploy/compose.compute-only.gpu.yml"
fi
# shellcheck disable=SC2086
docker compose -f deploy/compose.compute-only.yml $EXTRA up -d --build
echo "Fertig."
