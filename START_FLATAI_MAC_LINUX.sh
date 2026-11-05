#!/usr/bin/env bash
# FlatAI Docker Launcher (AI-Sana Draft)
set -e
cd "$(dirname "$0")"
echo "==================================================="
echo "  Starting FlatAI (PostgreSQL + AI NLP & Expert Core)"
echo "  Team Lead: Baygaskin Madi Dulatovich"
echo "==================================================="
( sleep 8; (xdg-open http://localhost:8000 || open http://localhost:8000) >/dev/null 2>&1 || true ) &
docker compose up --build
