#!/usr/bin/env bash
# Auto-reloads the server and the agent when agent.py / tools.py change.
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
exec adk web --reload --reload_agents
