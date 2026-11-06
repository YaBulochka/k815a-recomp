#!/bin/sh
# Local server for the WebHID page (WebHID requires localhost/https).
#   sh tools/serve_webhid.sh [port]
set -eu
PORT="${1:-8765}"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
echo "open http://127.0.0.1:$PORT/webhid/"
exec env K815_PORT="$PORT" python3 "$HERE/tools/serve_webhid.py"
