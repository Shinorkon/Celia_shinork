#!/usr/bin/env bash
# webhook_setup.sh — Register/check Telegram webhook for the bot
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Load .env if present
if [ -f "$ROOT/.env" ]; then
    set -a; source "$ROOT/.env"; set +a
fi

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
WEBHOOK_URL="${1:-}"

if [ -z "$TOKEN" ]; then
    echo "❌ TELEGRAM_BOT_TOKEN not set. Add it to .env or export it."
    exit 1
fi

API="https://api.telegram.org/bot$TOKEN"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Telegram Bot Webhook Manager"
echo "  Bot: @${TELEGRAM_BOT_USERNAME:-unknown}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Bot info ──────────────────────────────────────
echo "▶ Bot info:"
curl -sf "$API/getMe" | python3 -m json.tool 2>/dev/null || echo "  ❌ Failed to get bot info"
echo ""

# ── Current webhook status ────────────────────────
echo "▶ Current webhook info:"
curl -sf "$API/getWebhookInfo" | python3 -m json.tool 2>/dev/null
echo ""

# ── Set webhook if URL provided ───────────────────
if [ -n "$WEBHOOK_URL" ]; then
    echo "▶ Setting webhook to: $WEBHOOK_URL"
    curl -sf "$API/setWebhook?url=$WEBHOOK_URL&allowed_updates[]=message" | python3 -m json.tool
    echo ""
    echo "✅ Webhook set!"
elif [ -z "${1:-}" ]; then
    echo ""
    echo "Usage:"
    echo "  Check status:  bash scripts/webhook_setup.sh"
    echo "  Set webhook:   bash scripts/webhook_setup.sh https://your-domain.com/webhook"
    echo ""
    echo "  For local dev with ngrok:"
    echo "    ngrok http 8101"
    echo "    bash scripts/webhook_setup.sh https://xxxx.ngrok-free.app/webhook"
    echo ""
    echo "  For remote server:"
    echo "    bash scripts/webhook_setup.sh https://37.60.229.74:8101/webhook"
fi
