#!/bin/sh
# aria2 daemon (RPC jen na localhost uvnitř kontejneru) + FastAPI v jednom kontejneru.
# Secret pro RPC se generuje při každém startu — ven z kontejneru se nikdy nedostane.
set -e
export ARIA2_SECRET=$(head -c 24 /dev/urandom | base64 | tr -d '/+=')
mkdir -p /data /downloads/filmy /downloads/serialy /downloads/.incomplete
touch /data/aria2.session
aria2c --enable-rpc --rpc-listen-all=false --rpc-listen-port=6800 --rpc-secret="$ARIA2_SECRET" \
  --dir=/downloads --continue=true --max-connection-per-server=16 --split=16 --min-split-size=4M \
  --max-concurrent-downloads=3 --file-allocation=none --disk-cache=64M --auto-file-renaming=true \
  --save-session=/data/aria2.session --input-file=/data/aria2.session --save-session-interval=30 \
  --disable-ipv6=true --console-log-level=warn --summary-interval=0 --on-download-complete=/app/on_complete.py --daemon=false &
exec uvicorn app:app --host 0.0.0.0 --port 8097 --log-level warning
