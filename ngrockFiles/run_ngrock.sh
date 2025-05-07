#!/usr/bin/env bash
# Simple helper to expose local FastAPI (http://localhost:8000) via ngrok and print the public URL.

set -euo pipefail

# Requires 'ngrok' binary in PATH and AUTHTOKEN configured (ngrok config add‑authtoken <token>)

PORT=${1:-8000}

# Ensure ngrok is installed
if ! command -v ngrok >/dev/null; then
  echo "🔄 ngrok not found, installing..."
  bash "$(dirname "$0")/install_ngrock.sh"
fi

ngrok http $PORT > /dev/null &
NGROK_PID=$!

sleep 2
PUBLIC_URL=$(curl --silent http://127.0.0.1:4040/api/tunnels | jq -r '.tunnels[0].public_url')

echo "✅ Ngrok tunnel established: $PUBLIC_URL"

# Update CALLBACK_URI_HOST in .env
ENV_FILE="$(dirname "$0")/../.env"
if [ -f "$ENV_FILE" ]; then
  sed -i -E "s|^CALLBACK_URI_HOST=.*|CALLBACK_URI_HOST=\"$PUBLIC_URL\"|" "$ENV_FILE"
  echo "🔄 Updated CALLBACK_URI_HOST in $ENV_FILE"
else
  echo "⚠️ .env file not found at $ENV_FILE, please update CALLBACK_URI_HOST manually."
fi

echo "Remember to set BASE_URL=$PUBLIC_URL in your .env before running the bot."

wait $NGROK_PID