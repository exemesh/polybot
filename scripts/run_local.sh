#!/usr/bin/env bash
# run_local.sh — Manually run PolyBot once on the Mac mini (outside launchd)
#
# Usage:
#   chmod +x scripts/run_local.sh
#   ./scripts/run_local.sh
#
# This script:
#   1. Activates the Python virtual environment
#   2. Loads environment variables from .env
#   3. Runs python3 main.py --export-dashboard
#   4. Appends timestamped output to ~/polybot/logs/polybot_manual.log

set -euo pipefail

POLYBOT_DIR="${HOME}/polybot"
VENV_DIR="${HOME}/polybot-env"
LOG_DIR="${POLYBOT_DIR}/logs"
LOG_FILE="${LOG_DIR}/polybot_manual.log"
DOT_ENV="${POLYBOT_DIR}/.env"

# ── Validate pre-requisites ──────────────────────────────────────────────────
if [ ! -d "${VENV_DIR}" ]; then
    echo "ERROR: Virtual environment not found at ${VENV_DIR}"
    echo "       Run scripts/setup_mac_mini.sh first."
    exit 1
fi

if [ ! -f "${DOT_ENV}" ]; then
    echo "ERROR: .env file not found at ${DOT_ENV}"
    echo "       Copy .env.example to .env and fill in your secrets."
    exit 1
fi

# ── Create log directory ─────────────────────────────────────────────────────
mkdir -p "${LOG_DIR}"

# ── Load .env into shell (only non-comment, non-empty lines) ─────────────────
set -a
# shellcheck disable=SC1090
source "${DOT_ENV}"
set +a

# ── Activate virtual environment ─────────────────────────────────────────────
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

# ── Run the bot, tee output to log with timestamp prefix ─────────────────────
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================================"  | tee -a "${LOG_FILE}"
echo " PolyBot manual run started at ${TIMESTAMP}"            | tee -a "${LOG_FILE}"
echo "======================================================"  | tee -a "${LOG_FILE}"

cd "${POLYBOT_DIR}"

python3 main.py --export-dashboard 2>&1 | while IFS= read -r line; do
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${line}" | tee -a "${LOG_FILE}"
done

echo "------------------------------------------------------"  | tee -a "${LOG_FILE}"
echo " PolyBot run finished at $(date '+%Y-%m-%d %H:%M:%S')"  | tee -a "${LOG_FILE}"
echo ""                                                         | tee -a "${LOG_FILE}"
