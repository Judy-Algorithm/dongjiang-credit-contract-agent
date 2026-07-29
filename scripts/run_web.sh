#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m dongjiang_agent.cli serve --host 127.0.0.1 --port 8765
