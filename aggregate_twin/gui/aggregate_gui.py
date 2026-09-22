"""
Fleet console for the aggregate twin.

    from aggregate_twin.gui.aggregate_gui import start_gui
    start_gui(twin, port=8081)

Serves the static console and the JSON API in console_api. Reads go straight to the
twin's public state; writes go through ConsoleGateway onto the twin's step thread.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from aggregate_twin.gui.comm_monitor import CommMonitor
from aggregate_twin.gui.console_api import Console, api
from aggregate_twin.gui.console_gateway import ConsoleGateway
from aggregate_twin.gui.http_router import ApiError, Request

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
try:
    import core_msgs
    SHARED_STATIC_DIR: str | None = os.path.join(
        os.path.dirname(os.path.abspath(core_msgs.__file__)), "gui")
except Exception:
    SHARED_STATIC_DIR = None

CONTENT_TYPES = {
    ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
    ".svg": "image/svg+xml", ".json": "application/json", ".ico": "image/x-icon",
}


def start_gui(twin, host: str = "0.0.0.0", port: int = 8081) -> ThreadingHTTPServer:
    """Start the console in a daemon thread and return the server."""
    monitor = CommMonitor(twin)
    console = Console(twin, monitor, ConsoleGateway(twin, monitor))
    server = ThreadingHTTPServer((host, port), _handler_for(console))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="aggregate-gui", daemon=True).start()
    logger.info("fleet console listening on http://%s:%d", host, port)
    return server


def _static_file(path: str) -> tuple[bytes, str] | None:
    relative = "index.html" if path == "/" else path.lstrip("/")
    ctype = CONTENT_TYPES.get(os.path.splitext(relative)[1])
    if ctype is None:
        return None
    for base in filter(None, (STATIC_DIR, SHARED_STATIC_DIR)):
        full = os.path.normpath(os.path.join(base, relative))
        if full.startswith(base + os.sep) and os.path.isfile(full):
            with open(full, "rb") as fh:
                return fh.read(), f"{ctype}; charset=utf-8"
    return None


def _handler_for(console: Console) -> type[BaseHTTPRequestHandler]:

    class ConsoleHandler(BaseHTTPRequestHandler):
        server_version = "AggregateConsole/2.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            #logger.debug("gui " + fmt, *args)
            pass

        def do_GET(self) -> None:                           # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:                          # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                body = self._body() if method == "POST" else {}
                route = api.match(method, path)
                if route is None:
                    static = _static_file(path) if method == "GET" else None
                    if static is None:
                        return self._json({"error": "not found"}, 404)
                    return self._send(200, *static)
                handler, params = route
                result = handler(console, Request(method, path, params, body))
                payload, status = result if isinstance(result, tuple) else (result, 200)
                self._json(payload, status)
            except ApiError as exc:
                self._json(exc.payload(), exc.status)
            except (ValueError, KeyError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                logger.exception("%s %s", method, path)
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ApiError("Expected a JSON object.")
            return data

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload, default=str).encode(),
                       "application/json; charset=utf-8")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return ConsoleHandler
