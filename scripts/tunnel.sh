#!/bin/sh
# Expose a running ReallymyRadar through ngrok, for use away from home.
# The server requires ?token=<access_token> for anything arriving through the tunnel, so set one first
# (viewer ⚙ → Remote access, or:  curl -X PUT localhost:8600/api/settings -d '{"server":{"access_token":"long-secret"}}')
# Then open  https://<your-ngrok-url>/?token=long-secret  once; a cookie remembers it.
#   PORT=8600 NGROK_DOMAIN=you.ngrok-free.app scripts/tunnel.sh      (NGROK_DOMAIN optional: stable URL)
set -e
PORT="${PORT:-8600}"
TOKEN=$(curl -sf "http://127.0.0.1:$PORT/api/settings" | python3 -c "import json,sys; print(json.load(sys.stdin)['server']['access_token'] or '')") || {
  echo "ReallymyRadar isn't answering on port $PORT — start it first."; exit 1; }
if [ -z "$TOKEN" ]; then
  echo "No access token set — refusing to open the radar to the internet without one."
  exit 1
fi
if [ -n "$NGROK_DOMAIN" ]; then
  exec ngrok http "$PORT" --url "https://$NGROK_DOMAIN"
else
  exec ngrok http "$PORT"
fi
