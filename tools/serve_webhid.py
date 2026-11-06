#!/usr/bin/env python3
"""Static server for the WebHID page plus a local `POST /reset` endpoint.

The K815 only applies a new config after a USB re-enumeration.  WebHID cannot
reset a device, so the page calls this local server after a verified write; the
server performs `USBDEVFS_RESET` on the K815 (see `tools/k815_reset.py`).

Run:

    python3 tools/serve_webhid.py
    # http://127.0.0.1:8765/webhid/

Access to `/dev/bus/usb/...` comes from the udev rule; install it once with
`sudo sh tools/install_udev.sh`.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k815_reset             # noqa: E402

PORT = int(os.environ.get("K815_PORT", "8765"))
HOST = os.environ.get("K815_HOST", "127.0.0.1")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/reset":
            self.send_error(404, "not found")
            return
        try:
            dev = k815_reset.reset()
            body = json.dumps({"ok": True, "path": dev}).encode()
            status = 200
        except Exception as e:      # noqa: BLE001 - report the reason to the page
            body = json.dumps({"ok": False, "error": str(e)}).encode()
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Avoid a stale session while developing the generated template.
        if self.path.endswith((".js", ".html", ".css")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("webhid: " + (fmt % args) + "\n")


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"WebHID server: http://{HOST}:{PORT}/webhid/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
