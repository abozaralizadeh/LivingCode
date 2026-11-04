"""The web demo's front: the page, the live stream, the limits, and the LLM relay.

It's the only process that holds the Azure key and the only one with network. It never runs
the AI's code: each run happens in runner.py, which reaches the LLM only through the relay
socket here, and every relay call is checked against the run and the daily budget.

    PYTHONPATH=src python3 -m livingcode.webdemo.front

Settings come from the environment and .env, see .env.example.
"""
from __future__ import annotations

import http.server
import json
import logging
import logging.handlers
import os
import queue
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from ..common.lab import PROJECT_ROOT
from ..llm import FakeLLM, LLMError
from ..llm.azure import AzureOpenAIClient, FallbackClient
from . import settings as settings_module
from .limits import Limits
from .runner import RELAY_ENV
from .scenarios import SCENARIOS

STATIC = Path(__file__).resolve().parent / "static"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
MAX_BODY = 2048
MAX_RELAY_BODY = 2 * 1024 * 1024
MAX_CALLS_PER_RUN = 12
MAX_EVENTS_PER_RUN = 3000
EVENT_KINDS = ("step", "line", "ai", "done")
log = logging.getLogger("livingcode.web")


class Hub:
    """Sends run events to every page that's watching, and keeps the current run for late comers."""

    def __init__(self, limit: int):
        self.lock = threading.Lock()
        self.clients: set = set()
        self.limit = limit
        self.history: list = []

    def subscribe(self):
        with self.lock:
            if len(self.clients) >= self.limit:
                return None, []
            box = queue.Queue(maxsize=MAX_EVENTS_PER_RUN + 100)
            self.clients.add(box)
            return box, list(self.history)

    def unsubscribe(self, box) -> None:
        with self.lock:
            self.clients.discard(box)

    def publish(self, name: str, data: dict, keep: bool = True) -> None:
        event = (name, data)
        with self.lock:
            if keep:
                self.history.append(event)
            for box in list(self.clients):
                try:
                    box.put_nowait(event)
                except queue.Full:
                    self.clients.discard(box)

    def restart(self, events: list) -> None:
        with self.lock:
            self.history = list(events)


def clean_event(raw: str) -> dict | None:
    """Only the fields the page shows, with sane sizes. The runner's output is never trusted."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("t") not in EVENT_KINDS:
        return None
    event = {"t": data["t"], "text": str(data.get("text", ""))[:2000]}
    if data["t"] == "done":
        event = {"t": "done", "ok": data.get("ok") is True, "summary": str(data.get("summary", ""))[:600]}
    return event


class Front:
    def __init__(self, settings):
        self.settings = settings
        for folder in (settings.state_dir, settings.relay_socket.parent):
            folder.mkdir(parents=True, exist_ok=True)
        self.limits = Limits(settings.state_dir, settings.runs_per_ip_per_day, settings.daily_run_cap,
                             settings.daily_token_budget)
        self.hub = Hub(settings.max_watchers)
        self.lock = threading.Lock()
        self.current: dict | None = None
        self.stopper = None
        self.client = None
        if settings.llm == "azure":
            main = AzureOpenAIClient(settings.primary, timeout=90)
            self.client = FallbackClient(main, AzureOpenAIClient(settings.fallback, timeout=90)) if settings.fallback else main
        self.last_file = settings.state_dir / "last-run.json"
        try:
            saved = json.loads(self.last_file.read_text())
            self.hub.restart([(name, data) for name, data in saved.get("events", [])])
        except (OSError, ValueError, TypeError):
            pass

    # The page

    def state(self) -> dict:
        with self.lock:
            current = self.current
        busy = current is not None
        return {
            "busy": busy,
            "scenario": current["scenario"].id if busy else None,
            "title": current["scenario"].title if busy else None,
            "seconds": int(time.time() - current["started"]) if busy else 0,
            "llm": self.settings.llm,
            "timeout": self.settings.run_timeout,
            "scenarios": [item.public() for item in SCENARIOS.values()],
        }

    def visitor_state(self, ip: str) -> dict:
        state = self.state()
        state.update(self.limits.summary(ip))
        refusal = None if state["busy"] else self.limits.refusal(ip)
        state["message"] = refusal
        return state

    def start(self, scenario_id, ip: str) -> tuple[int, str]:
        scenario = SCENARIOS.get(scenario_id) if isinstance(scenario_id, str) else None
        if scenario is None:
            return 400, "Pick one of the bugs on the page."
        with self.lock:
            if self.current is not None:
                return 409, "Someone else's run is going. You can watch it, and start yours when it ends."
            refusal = self.limits.refusal(ip)
            if refusal:
                return 429, refusal
            self.limits.record_run(ip)
            run_id = uuid.uuid4().hex[:12]
            client = FakeLLM(scenario.fake_rules) if self.settings.llm == "fake" else self.client
            self.current = {"id": run_id, "scenario": scenario, "started": time.time(), "tokens": 0,
                            "calls": 0, "client": client, "events": 0}
        self.hub.restart([])
        self.hub.publish("run", {"t": "start", "run": run_id, "scenario": scenario.id, "title": scenario.title,
                                 "llm": self.settings.llm})
        self.hub.publish("state", self.state(), keep=False)
        log.info("run %s started: %s (visitor %s)", run_id, scenario.id, self.limits.visitor_id(ip))
        threading.Thread(target=self._run, args=(self.current,), daemon=True, name=f"run-{run_id}").start()
        return 202, "Started."

    def _open(self, request: str):
        """Starts the runner and returns a line reader and a function that stops it."""
        if self.settings.runner_socket:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(self.settings.runner_socket)
            sock.settimeout(None)
            sock.sendall(request.encode())

            def stop() -> None:
                for step in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
                    try:
                        step()
                    except OSError:
                        pass

            return sock.makefile("r", encoding="utf-8", errors="replace"), stop
        # Locally the runner is a child process. It gets a clean environment, never the key.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tempfile.gettempdir(),
            "TMPDIR": tempfile.gettempdir(),
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C.UTF-8",
            RELAY_ENV: str(self.settings.relay_socket),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "livingcode.webdemo.runner"], cwd=PROJECT_ROOT, env=env, text=True,
            errors="replace", stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True,
        )
        process.stdin.write(request)
        process.stdin.close()

        def stop() -> None:
            # Ask first, so the runner stops the sample app and deletes its lab, then make sure.
            for sig, wait in ((signal.SIGTERM, 8), (signal.SIGKILL, None)):
                if process.poll() is not None:
                    break
                try:
                    os.killpg(process.pid, sig)
                    process.wait(timeout=wait)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            process.wait()

        return process.stdout, stop

    def _run(self, current: dict) -> None:
        ending = None
        timed_out = threading.Event()
        stop = reader = None
        try:
            reader, stop = self._open(json.dumps({"scenario": current["scenario"].id}) + "\n")
            self.stopper = stop

            def too_long() -> None:
                timed_out.set()
                stop()

            timer = threading.Timer(self.settings.run_timeout, too_long)
            timer.daemon = True
            timer.start()
            try:
                for raw in reader:
                    event = clean_event(raw)
                    if event is None or current["events"] >= MAX_EVENTS_PER_RUN:
                        continue
                    current["events"] += 1
                    if event["t"] == "done":
                        ending = event
                        break
                    self.hub.publish("run", event)
                    log.info("run %s | %s", current["id"], event["text"][:300])
            finally:
                timer.cancel()
        except OSError as exc:
            log.error("run %s couldn't start the runner: %s", current["id"], exc)
            ending = {"t": "done", "ok": False, "summary": "The demo's runner isn't available right now. Please try again later."}
        finally:
            if stop:
                stop()
            if reader:
                try:
                    reader.close()
                except OSError:
                    pass
        if ending is None:
            summary = (f"The run took longer than {self.settings.run_timeout} seconds, so it was stopped and thrown away."
                       if timed_out.is_set() else "The run ended without a result, so nothing more happened.")
            ending = {"t": "done", "ok": False, "summary": summary}
        ending["tokens"] = current["tokens"]
        ending["seconds"] = int(time.time() - current["started"])
        self.hub.publish("run", ending)
        log.info("run %s ended: ok=%s tokens=%s seconds=%s %s", current["id"], ending["ok"], ending["tokens"],
                 ending["seconds"], ending["summary"][:200])
        self._save_last()
        with self.lock:
            self.current = None
            self.stopper = None
        self.hub.publish("state", self.state(), keep=False)

    def _save_last(self) -> None:
        with self.hub.lock:
            events = [[name, data] for name, data in self.hub.history]
        tmp = self.last_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ended": time.time(), "events": events}))
        os.replace(tmp, self.last_file)

    # The relay

    def relay(self, body) -> tuple[int, dict]:
        with self.lock:
            current = self.current
        if current is None:
            return 403, {"error": "no run is going, so no LLM call is allowed"}
        if current["calls"] >= MAX_CALLS_PER_RUN:
            return 429, {"error": "this run has used all its LLM calls"}
        if current["tokens"] >= self.settings.run_token_cap:
            return 429, {"error": "this run has used all its AI tokens"}
        if self.settings.llm == "azure" and self.limits.tokens_left() == 0:
            return 429, {"error": "today's AI budget is used up"}
        if not isinstance(body, dict):
            return 400, {"error": "bad request"}
        system, messages, max_tokens = body.get("system"), body.get("messages"), body.get("max_tokens")
        if not isinstance(system, str) or not isinstance(messages, list) or not isinstance(max_tokens, int):
            return 400, {"error": "bad request"}
        if not all(isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
                   for m in messages):
            return 400, {"error": "bad messages"}
        current["calls"] += 1
        try:
            reply = current["client"].complete(system, [{"role": m["role"], "content": m["content"]} for m in messages],
                                               max(256, min(max_tokens, 8000)))
        except LLMError as exc:
            log.warning("run %s LLM call failed: %s", current["id"], exc)
            return 502, {"error": str(exc)[:300]}
        used = reply.input_tokens + reply.output_tokens
        current["tokens"] += used
        if self.settings.llm == "azure":
            self.limits.add_tokens(used)
        return 200, {"text": reply.text, "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                     "model": reply.model}


class WebHandler(http.server.BaseHTTPRequestHandler):
    server_version = "LivingCodeDemo"
    sys_version = ""
    timeout = 30

    @property
    def front(self) -> Front:
        return self.server.front

    def log_message(self, *args) -> None:
        pass

    def client_ip(self) -> str:
        peer = self.client_address[0]
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded and peer in self.front.settings.trust_proxy:
            return forwarded.split(",")[-1].strip()
        return peer

    def _send(self, status: int, body: bytes, kind: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in FILES:
            name, kind = FILES[path]
            cache = "no-cache" if name == "index.html" else "public, max-age=300"
            self._send(200, (STATIC / name).read_bytes(), kind, cache)
        elif path == "/api/health":
            self._json(200, {"ok": True})
        elif path == "/api/state":
            self._json(200, self.front.visitor_state(self.client_ip()))
        elif path == "/api/events":
            self._events()
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/run":
            self._json(404, {"error": "not found"})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._json(415, {"error": "send JSON"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self._json(413, {"error": "request too big"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            body = None
        scenario = body.get("scenario") if isinstance(body, dict) else None
        status, message = self.front.start(scenario, self.client_ip())
        self._json(status, {"ok": status == 202, "message": message})

    def _events(self) -> None:
        box, history = self.front.hub.subscribe()
        if box is None:
            self._json(503, {"error": "too many people are watching right now"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self._event("state", self.front.state())
            for name, data in history:
                self._event(name, data)
            self.wfile.flush()
            while True:
                try:
                    name, data = box.get(timeout=15)
                    self._event(name, data)
                except queue.Empty:
                    self.wfile.write(b": still here\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.front.hub.unsubscribe(box)

    def _event(self, name: str, data: dict) -> None:
        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())


class RelayHandler(http.server.BaseHTTPRequestHandler):
    server_version = "LivingCodeRelay"
    sys_version = ""
    timeout = 300

    def address_string(self) -> str:
        return "relay"

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        status, data = 404, {"error": "not found"}
        if self.path == "/complete":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_RELAY_BODY:
                status, data = 413, {"error": "request too big"}
            else:
                try:
                    body = json.loads(self.rfile.read(length))
                except ValueError:
                    body = None
                status, data = self.server.front.relay(body)
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class WebServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


class RelayServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def start_relay(front: Front) -> RelayServer:
    path = front.settings.relay_socket
    path.unlink(missing_ok=True)
    old = os.umask(0o177)
    try:
        server = RelayServer(str(path), RelayHandler)
    finally:
        os.umask(old)
    server.front = front
    threading.Thread(target=server.serve_forever, daemon=True, name="relay").start()
    return server


def setup_logging(settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    to_file = logging.handlers.RotatingFileHandler(settings.log_dir / "livingcode-web.log",
                                                   maxBytes=settings.log_max_bytes, backupCount=2)
    to_journal = logging.StreamHandler(sys.stdout)
    for handler in (to_file, to_journal):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def main() -> int:
    try:
        settings = settings_module.load()
    except settings_module.SettingsError as exc:
        print(f"Settings problem: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings)
    front = Front(settings)
    relay = start_relay(front)
    web = WebServer((settings.host, settings.port), WebHandler)
    web.front = front
    where = settings.primary.deployment if settings.primary else "the scripted fake"
    if settings.fallback:
        where += f", then {settings.fallback.deployment}"
    runner = settings.runner_socket or "a child process"
    log.info("LivingCode web demo on http://%s:%s, AI: %s, runner: %s", settings.host, settings.port, where, runner)

    def bye(*_args) -> None:
        threading.Thread(target=web.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    try:
        web.serve_forever()
    finally:
        if front.stopper:
            front.stopper()
        relay.shutdown()
        settings.relay_socket.unlink(missing_ok=True)
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
