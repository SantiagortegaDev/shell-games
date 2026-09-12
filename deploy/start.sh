#!/usr/bin/env bash
# Comando del Service en AlwaysData: levanta el tunnel de Cloudflare en
# background y deja el servidor de chat como proceso principal (foreground).
set -e
cd "$(dirname "$0")/.."

./deploy/cloudflared tunnel --config deploy/config.yml run &

exec ./venv/bin/python3 server.py 8888
