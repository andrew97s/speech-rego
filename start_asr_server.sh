#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python asr_server.py "$@"
fi
exec python3 asr_server.py "$@"
