#!/usr/bin/env bash
# setup_openbb_providers.sh — Install free OpenBB data providers on the OpenBB LXC.
#
# Run this ON the OpenBB LXC (not Quantfolio).
# Usage: bash setup_openbb_providers.sh

set -euo pipefail

OPENBB_VENV="${OPENBB_VENV:-/opt/openbb/.venv}"
OPENBB_SETTINGS="${OPENBB_SETTINGS:-$HOME/.openbb_platform/user_settings.json}"

echo "=== Installing free OpenBB providers (no API keys needed) ==="
"$OPENBB_VENV/bin/pip" install --quiet \
  openbb-cboe \
  openbb-finviz \
  openbb-sec \
  openbb-famafrench \
  openbb-ecb \
  openbb-imf \
  openbb-federal-reserve \
  openbb-oecd \
  openbb-seeking-alpha \
  openbb-tmx \
  openbb-yfinance
echo "✓ 11 zero-key providers installed"

echo ""
echo "=== Installing free OpenBB providers (free API keys needed) ==="
echo "You must register for API keys before these will work."
echo "Skipping install — uncomment below after obtaining keys:"
echo "  # $OPENBB_VENV/bin/pip install openbb-fmp openbb-polygon openbb-tiingo openbb-fred openbb-tradier openbb-alpha-vantage"

echo ""
echo "=== Checking currently installed OpenBB extensions ==="
"$OPENBB_VENV/bin/pip" list 2>/dev/null | grep "^openbb-" || echo "(no openbb- extensions found)"

echo ""
echo "=== Configuration ==="
echo "Add API keys to: $OPENBB_SETTINGS"
echo ""
echo "Example for keyed providers (merge into existing JSON):"
cat <<'JSON'
{
  "openbb_platform": {
    "credentials": {
      "fmp_api_key": "YOUR_FMP_KEY",
      "polygon_api_key": "YOUR_POLYGON_KEY",
      "tiingo_token": "YOUR_TIINGO_TOKEN",
      "fred_api_key": "YOUR_FRED_KEY",
      "tradier_api_key": "YOUR_TRADIER_KEY",
      "alpha_vantage_api_key": "YOUR_ALPHA_VANTAGE_KEY"
    },
    "defaults": {
      "providers": ["cboe", "finviz", "yfinance", "fmp", "polygon", "tiingo"]
    }
  }
}
JSON

echo ""
echo "=== Restart OpenBB ==="
echo "  systemctl restart openbb-platform"
echo ""
echo "=== Verify (run on OpenBB LXC) ==="
echo "  curl -s http://localhost:6900/api/v1/equity/price/quote?symbol=AAPL&provider=cboe | python3 -m json.tool"
echo ""
echo "Done. Updated _PROBE_CANDIDATE_PROVIDERS in Quantfolio's openbb_provider.py"
echo "will auto-detect these on next restart."
