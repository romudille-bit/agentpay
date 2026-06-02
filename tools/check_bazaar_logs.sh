#!/usr/bin/env bash
# check_bazaar_logs.sh — surface the Bazaar indexing verdict from Railway logs.
#
# Shows, in order, the three lines that tell you whether a session_create
# settle triggered indexing:
#   [BASE] CDP JWT built for key ...        -> credentials loaded
#   [BASE] Bazaar extension response: {...} -> CDP accept/reject verdict
#   [BASE] Settle response: success=True    -> on-chain settle ok
#
# Usage:
#   ./tools/check_bazaar_logs.sh           # one-shot: scan recent logs
#   ./tools/check_bazaar_logs.sh -f        # follow (live tail) until Ctrl-C
#
# Requires: railway CLI, logged in, linked to the project.

set -euo pipefail

SERVICE="${SERVICE:-gateway}"
PATTERN='Bazaar extension response|CDP JWT built|\[BASE\] Settle response|bazaar'

if [[ "${1:-}" == "-f" || "${1:-}" == "--follow" ]]; then
  echo "Following $SERVICE logs for Bazaar lines (Ctrl-C to stop)..."
  railway logs --service "$SERVICE" --follow 2>/dev/null | grep --line-buffered -iE "$PATTERN"
else
  echo "Recent $SERVICE log lines matching Bazaar indexing:"
  railway logs --service "$SERVICE" 2>/dev/null | grep -iE "$PATTERN" | tail -20
  echo "---"
  echo "(no lines above = no recent settle; run tools/index_bazaar.py, then re-run this)"
fi
