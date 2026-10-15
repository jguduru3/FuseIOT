"""
A real HTTP server that simulates a device.

Useful as a reference implementation of the device contract for firmware
authors, for end-to-end tests, and for demos::

    fuseiot simulate switchable --port 8001
    # then: HTTP("http://127.0.0.1:8001") + Switchable

or from Python::

    with SimulatedHTTPDevice("dimmable") as dev:
        hub.add_device(HTTP(dev.url), Dimmable)
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlparse

from ..exceptions import ProtocolError
from .virtual_device import VirtualDevice, VirtualDeviceConfig


class SimulatedHTTPDevice:
    """Serve a :class:`VirtualDevice` over HTTP on ``host:port`` (0 = random port)."""

    def __init__(
        self,
        device_type: str = "switchable",
        host: str = "127.0.0.1",
        port: int = 0,
        config: Optional[VirtualDeviceConfig] = None,
        auth_token: Optional[str] = None,
        **overrides: Any,
    ):
        self.device = VirtualDevice(device_type, config, **overrides)
        self.device.connect()
        self.auth_token = auth_token
        device, token = self.device, auth_token

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self, status: int, body: Dict[str, Any]) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                if token and self.headers.get("Authorization") != f"Bearer {token}":
                    self._reply(401, {"error": "unauthorized"})
                    return
                parsed = urlparse(self.path)
                body: Dict[str, Any] = dict(parse_qsl(parsed.query))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        loaded = json.loads(self.rfile.read(length) or b"{}")
                    except json.JSONDecodeError:
                        self._reply(400, {"error": "invalid json"})
                        return
                    if isinstance(loaded, dict):
                        body.update(loaded)
                path = parsed.path.rstrip("/") or "/"
                if method == "GET" and path == "/":
                    self._reply(200, {"device_type": device.device_type, "status": "ok"})
                    return
                try:
                    self._reply(200, device.handle(method, path, body))
                except ProtocolError as e:
                    self._reply(503, {"error": e.message})

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def log_message(self, format: str, *args: Any) -> None:  # silence
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None
        self._serving = False

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        host = self._server.server_address[0]
        return f"http://{host}:{self.port}"

    def start(self, poll_interval: float = 0.5) -> "SimulatedHTTPDevice":
        self._serving = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        kwargs={"poll_interval": poll_interval},
                                        name=f"sim-{self.device.device_type}")
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._serving = True
        self._server.serve_forever()

    def stop(self) -> None:
        if self._serving:
            self._server.shutdown()  # only valid while serve_forever() is running
            self._serving = False
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "SimulatedHTTPDevice":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
