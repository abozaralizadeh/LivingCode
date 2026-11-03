"""Throwaway sample web app. It exists only so LivingCode has something safe to change."""
import logging
import logging.handlers
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import routes

try:
    import livingcode
except ImportError:
    livingcode = None

log = logging.getLogger("sample")


def setup_logging():
    handler = logging.handlers.RotatingFileHandler(
        os.environ.get("LOG_FILE", "app.log"), maxBytes=1024 * 1024, backupCount=2
    )
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        started = time.time()
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            status, body = routes.handle(parsed.path, query)
        except Exception:
            log.exception("request failed: %s", self.path)
            status, body = 500, "something went wrong"
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        if livingcode:
            livingcode.record_request(
                parsed.path, status, (time.time() - started) * 1000,
                self.headers.get("Cookie", ""), self.headers.get("User-Agent", ""),
            )

    def log_message(self, *args):
        pass


def main():
    setup_logging()
    if livingcode:
        livingcode.start()
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "18080"))), Handler)
    log.info("sample app started")
    server.serve_forever()


if __name__ == "__main__":
    main()
