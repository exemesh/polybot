#!/usr/bin/env bash
# Manual run of polybot v4. Pass --live to enable real orders.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [ ! -d venv ]; then
    echo "venv not found. Run: ./scripts/setup_mac.sh"
    exit 1
fi
# shellcheck disable=SC1091
source venv/bin/activate
exec python3 -m src.main "$@"
