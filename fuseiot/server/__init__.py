"""HTTP server exposing a hub: REST API, dashboard, health and metrics."""
from fuseiot.server.app import FuseIoTServer, run_server

__all__ = ["FuseIoTServer", "run_server"]
