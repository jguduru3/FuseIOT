"""In-process metrics with Prometheus text exposition."""
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..logging_config import get_logger

logger = get_logger("monitoring.metrics")

LabelKey = Tuple[Tuple[str, str], ...]


@dataclass
class MetricValue:
    value: float
    timestamp: float
    labels: Dict[str, str] = field(default_factory=dict)


def _labels_key(labels: Optional[Dict[str, str]]) -> LabelKey:
    return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_labels(key: LabelKey, extra: Optional[Dict[str, str]] = None) -> str:
    items = list(key) + sorted((extra or {}).items())
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in items) + "}"


class MetricsCollector:
    """
    Thread-safe counters, gauges and timers.

    Timers keep a bounded window of recent samples (for min/avg/max) plus
    running ``count``/``sum`` totals for Prometheus summaries.
    """

    def __init__(self, max_samples: int = 1000):
        self._lock = threading.Lock()
        self._max_samples = max_samples
        self._counters: Dict[str, Dict[LabelKey, float]] = defaultdict(dict)
        self._gauges: Dict[str, Dict[LabelKey, float]] = defaultdict(dict)
        self._timers: Dict[str, Dict[LabelKey, Dict[str, Any]]] = defaultdict(dict)
        self._help: Dict[str, str] = {}

    def describe(self, name: str, help_text: str) -> None:
        self._help[name] = help_text

    def increment(self, name: str, value: float = 1, labels: Optional[Dict[str, str]] = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            series = self._counters[name]
            series[key] = series.get(key, 0) + value

    def gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self._gauges[name][_labels_key(labels)] = float(value)

    def timer(self, name: str, duration_ms: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            entry = self._timers[name].setdefault(key, {"count": 0, "sum": 0.0, "samples": []})
            entry["count"] += 1
            entry["sum"] += duration_ms
            samples: List[float] = entry["samples"]
            samples.append(duration_ms)
            if len(samples) > self._max_samples:
                del samples[: len(samples) - self._max_samples]

    # Backwards-compatible alias
    histogram = timer

    def get_stats(self) -> Dict[str, Any]:
        """JSON-friendly snapshot."""
        def name_with(name: str, key: LabelKey) -> str:
            return name + _fmt_labels(key)

        with self._lock:
            timers = {}
            for name, series in self._timers.items():
                for key, entry in series.items():
                    s = entry["samples"]
                    timers[name_with(name, key)] = {
                        "count": entry["count"],
                        "avg_ms": round(entry["sum"] / entry["count"], 3) if entry["count"] else 0.0,
                        "min_ms": round(min(s), 3) if s else 0.0,
                        "max_ms": round(max(s), 3) if s else 0.0,
                    }
            return {
                "counters": {name_with(n, k): v for n, ser in self._counters.items() for k, v in ser.items()},
                "gauges": {name_with(n, k): v for n, ser in self._gauges.items() for k, v in ser.items()},
                "timers": timers,
            }

    def export_prometheus(self) -> str:
        """Render all metrics in the Prometheus text exposition format (v0.0.4)."""
        lines: List[str] = []
        with self._lock:
            for name, series in sorted(self._counters.items()):
                if name in self._help:
                    lines.append(f"# HELP {name} {self._help[name]}")
                lines.append(f"# TYPE {name} counter")
                for key, value in series.items():
                    lines.append(f"{name}{_fmt_labels(key)} {value}")
            for name, series in sorted(self._gauges.items()):
                if name in self._help:
                    lines.append(f"# HELP {name} {self._help[name]}")
                lines.append(f"# TYPE {name} gauge")
                for key, value in series.items():
                    lines.append(f"{name}{_fmt_labels(key)} {value}")
            for name, series in sorted(self._timers.items()):
                if name in self._help:
                    lines.append(f"# HELP {name} {self._help[name]}")
                lines.append(f"# TYPE {name} summary")
                for key, entry in series.items():
                    samples = sorted(entry["samples"])
                    for q in (0.5, 0.9, 0.99):
                        if samples:
                            idx = min(len(samples) - 1, round(q * (len(samples) - 1)))
                            lines.append(f"{name}{_fmt_labels(key, {'quantile': str(q)})} {samples[idx]}")
                    lines.append(f"{name}_sum{_fmt_labels(key)} {entry['sum']}")
                    lines.append(f"{name}_count{_fmt_labels(key)} {entry['count']}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._timers.clear()


class PrometheusMetrics(MetricsCollector):
    """
    Standalone ``/metrics`` endpoint.

    Prefer ``fuseiot serve``, which already exposes ``/metrics``; use this when
    embedding FuseIoT in your own application.
    """

    def __init__(self, port: int = 9090, host: str = "127.0.0.1", source: Optional[MetricsCollector] = None):
        super().__init__()
        self.port = port
        self.host = host
        self._source = source or self
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None

    def export(self) -> str:
        return self._source.export_prometheus()

    def start_server(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        exporter = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.split("?")[0] != "/metrics":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = exporter.export().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("prometheus_server_started", host=self.host, port=self.port, started=time.time())

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
