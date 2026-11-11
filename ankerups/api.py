"""A small HTTP API for Siri / Apple Shortcuts (or anything else): GET /<route>?key=<API_KEY>.

.env: API_BIND=address:port to listen on (e.g. the VM's Tailscale address, 100.x.y.z:8765, so it is
reachable only from your own Tailscale devices), API_KEY=a long random secret. Without both, no API.
Every answer is one short line of plain text that Siri can read out.
"""

import hmac
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .common import log

Route = Callable[[dict[str, str]], str]


class Api:
    def __init__(self, bind: str, key: str, routes: dict[str, Route]):
        host, _, port = bind.rpartition(":")
        self.addr = (host, int(port))
        self.key = key
        self.routes = routes

    @classmethod
    def from_env(cls, env, routes: dict[str, Route]) -> "Api | None":
        bind, key = (env.get("API_BIND") or "").strip(), (env.get("API_KEY") or "").strip()
        if not bind or ":" not in bind or not bind.rpartition(":")[2].isdigit():
            return None
        if len(key) < 16:
            log.warning("API_BIND is set but API_KEY is missing or shorter than 16 characters: no API")
            return None
        return cls(bind, key, routes)

    def answer(self, path: str) -> tuple[int, str]:
        """(HTTP status, text) for a request path with its query."""
        url = urllib.parse.urlsplit(path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        if not hmac.compare_digest(query.pop("key", ""), self.key):
            return 403, "forbidden"
        route = self.routes.get(url.path.strip("/").lower())
        if route is None:
            return 404, "unknown: " + ", ".join(sorted(self.routes))
        try:
            return 200, route(query)
        except Exception:
            log.exception("API %s failed", url.path)
            return 500, "error"

    def start(self) -> None:
        """Serve in a thread; the address may not exist yet (Tailscale starting), so it retries."""
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the http.server name
                code, text = api.answer(self.path)
                body = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                log.info("API %s -> %s", self.path.split("?")[0], code)

            do_POST = do_GET

            def log_message(self, *args):  # the access log goes through ours (without the key)
                pass

        def serve() -> None:
            while True:
                try:
                    server = ThreadingHTTPServer(self.addr, Handler)
                    log.info("API on %s:%s", *self.addr)
                    server.serve_forever()
                except OSError as exc:
                    log.warning("API cannot listen on %s:%s yet (%s), retrying in 30 s", *self.addr, exc)
                    time.sleep(30)

        threading.Thread(target=serve, daemon=True, name="api").start()
