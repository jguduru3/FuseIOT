"""Web dashboard (served by :class:`fuseiot.server.FuseIoTServer`)."""
from typing import Any

from ..server.app import FuseIoTServer


def run_dashboard(hub: Any, host: str = "127.0.0.1", port: int = 8080, **kwargs: Any) -> None:
    """Serve the dashboard and REST API for ``hub`` until interrupted."""
    server = FuseIoTServer(hub, host=host, port=port, **kwargs)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
