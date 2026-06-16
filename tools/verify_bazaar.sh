#!/usr/bin/env bash
# verify_bazaar.sh — end-to-end Bazaar readiness check.
#   1) confirms the LIVE 402 carries extensions.bazaar + serviceName/tags
#   2) shows the latest Bazaar lines from Railway logs
#   3) polls discovery for the agentpay listing
#
# Usage: ./tools/verify_bazaar.sh

set -uo pipefail
# Override URL to check any paid resource's live 402, e.g.:
#   URL=https://agentpay.tools/tools/verified_route/call ./tools/verify_bazaar.sh
URL="${URL:-https://agentpay.tools/v1/session/create}"
SERVICE="${SERVICE:-gateway}"

echo "1) LIVE 402 extension check ($URL)"
curl -si -m 15 "$URL" | grep -i '^payment-required:' | cut -d' ' -f2 | python3 -c '
import sys, base64, json
raw = sys.stdin.read().strip()
if not raw:
    print("   ✗ no PAYMENT-REQUIRED header"); sys.exit(0)
d = json.loads(base64.b64decode(raw + "=="))
ext = (d.get("extensions") or {}).get("bazaar")
res = d.get("resource", {})
print("   extensions.bazaar :", "✓ present" if ext else "✗ MISSING")
print("   bazaar keys       :", list(ext.keys()) if ext else "-")
print("   serviceName       :", res.get("serviceName"))
print("   tags              :", res.get("tags"))
'

echo
echo "2) Latest Bazaar lines from Railway ($SERVICE)"
railway logs --service "$SERVICE" 2>/dev/null \
  | grep -iE "Bazaar extension response|CDP JWT built|\[BASE\] Settle response" | tail -6
echo "   (empty = no settle since last deploy; run tools/index_bazaar.py)"

echo
echo "3) Discovery search"
for q in agentpay "stateful spending session"; do
  curl -s -m 20 "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search?query=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$q")" \
    | python3 -c "import sys,json;rs=json.load(sys.stdin).get('resources',[]);print('   [%-26s] %d results | agentpay:'%('$q', len(rs)), any('agentpay' in json.dumps(x).lower() for x in rs))"
done
