#!/usr/bin/env python3
"""Static file server with HTTP Range support (needed for video seeking)."""
import http.server
import mimetypes
import os
import sys

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "ui")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8791


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._send(True)

    def do_GET(self):
        self._send(False)

    def _send(self, head_only):
        path = self.path.split("?")[0]
        if path == "/":
            path = "/index.html"
        full = os.path.abspath(os.path.join(ROOT, path.lstrip("/")))
        if not full.startswith(ROOT) or not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-", 1)
                start = int(a) if a else 0
                end = int(b) if b else size - 1
                end = min(end, size - 1)
                status = 206
            except Exception:
                start, end, status = 0, size - 1, 200
        if start > end:
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head_only:
            return
        with open(full, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                left -= len(chunk)


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("serving %s on :%d" % (ROOT, PORT), flush=True)
    server.serve_forever()
