"""MetricsCollector and the standalone Prometheus exporter."""
import urllib.error
import urllib.request

import pytest

from fuseiot.monitoring.metrics import MetricsCollector, PrometheusMetrics


def test_counters_gauges_timers():
    m = MetricsCollector()
    m.increment("requests_total", labels={"b": "2", "a": "1"})
    m.increment("requests_total", value=2, labels={"a": "1", "b": "2"})
    m.gauge("temperature", 21.5)
    for v in (10.0, 30.0, 20.0):
        m.timer("latency_ms", v, labels={"device": "lamp"})
    m.histogram("latency_ms", 40.0, labels={"device": "lamp"})
    stats = m.get_stats()
    assert stats["counters"] == {'requests_total{a="1",b="2"}': 3}
    assert stats["gauges"] == {"temperature": 21.5}
    assert stats["timers"]['latency_ms{device="lamp"}'] == {"count": 4, "avg_ms": 25.0,
                                                            "min_ms": 10.0, "max_ms": 40.0}


def test_prometheus_exposition():
    m = MetricsCollector()
    m.describe("hits_total", "Number of hits")
    m.increment("hits_total", labels={"path": '/a"b\\c\nd'})
    m.gauge("up", 1)
    for v in range(1, 11):
        m.timer("lat_ms", float(v))
    text = m.export_prometheus()
    lines = text.splitlines()
    assert text.endswith("\n")
    assert "# HELP hits_total Number of hits" in lines
    assert "# TYPE hits_total counter" in lines
    assert 'hits_total{path="/a\\"b\\\\c\\nd"} 1' in lines
    assert "# TYPE up gauge" in lines and "up 1.0" in lines
    assert "# TYPE lat_ms summary" in lines
    assert 'lat_ms{quantile="0.5"} 6.0' in lines or 'lat_ms{quantile="0.5"} 5.0' in lines
    assert 'lat_ms{quantile="0.99"} 10.0' in lines
    assert "lat_ms_sum 55.0" in lines and "lat_ms_count 10" in lines


def test_timer_window_is_bounded_but_totals_are_not():
    m = MetricsCollector(max_samples=5)
    for v in range(100):
        m.timer("t", float(v))
    entry = m._timers["t"][()]
    assert len(entry["samples"]) == 5 and entry["samples"][0] == 95.0
    assert entry["count"] == 100 and entry["sum"] == sum(range(100))
    assert m.get_stats()["timers"]["t"]["min_ms"] == 95.0


def test_reset():
    m = MetricsCollector()
    m.increment("a")
    m.gauge("b", 1)
    m.timer("c", 1)
    m.reset()
    assert m.get_stats() == {"counters": {}, "gauges": {}, "timers": {}}
    assert m.export_prometheus() == "\n"


def test_prometheus_server():
    source = MetricsCollector()
    source.increment("fuseiot_test_total")
    exporter = PrometheusMetrics(port=0, source=source)
    exporter.start_server()
    try:
        assert exporter.port != 0
        base = f"http://127.0.0.1:{exporter.port}"
        with urllib.request.urlopen(base + "/metrics?x=1", timeout=5) as resp:
            body = resp.read().decode()
            assert resp.headers["Content-Type"].startswith("text/plain")
        assert "fuseiot_test_total 1" in body
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/other", timeout=5)
        assert exc.value.code == 404
        exc.value.close()
    finally:
        exporter.stop()
    assert exporter._server is None
    exporter.stop()  # idempotent


def test_prometheus_exports_itself_by_default():
    p = PrometheusMetrics()
    p.gauge("g", 2)
    assert "g 2.0" in p.export()
