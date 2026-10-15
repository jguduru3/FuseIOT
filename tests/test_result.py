"""CommandResult, BatchResult and the exception hierarchy."""
import json

import pytest

from fuseiot.exceptions import (
    AuthenticationError,
    CircuitOpenError,
    CommandError,
    ConfigurationError,
    ConfirmationError,
    DeviceError,
    FuseIoTError,
    ProtocolError,
    RateLimitError,
    StateError,
    ValidationError,
)
from fuseiot.result import BatchResult, CommandResult, CommandStatus


def make(status=CommandStatus.CONFIRMED, success=True, confirmed=True, **kw):
    return CommandResult(success=success, confirmed=confirmed, status=status, **kw)


class TestCommandResult:
    def test_truthiness_follows_success(self):
        assert bool(make()) is True
        failed = make(CommandStatus.FAILED, success=False, confirmed=False)
        assert bool(failed) is False
        assert failed.failed is True

    def test_timestamp_defaults_to_now(self):
        assert make().timestamp > 0
        assert make(timestamp=12.5).timestamp == 12.5

    @pytest.mark.parametrize("before,after,expected", [
        (None, {"power": True}, None),
        ({"power": True}, None, None),
        ({"power": False}, {"power": True}, True),
        ({"power": True}, {"power": True}, False),
    ])
    def test_state_changed(self, before, after, expected):
        assert make(state_before=before, state_after=after).state_changed is expected

    def test_raise_for_error_returns_self_on_success(self):
        r = make()
        assert r.raise_for_error() is r

    def test_raise_for_error_rate_limited(self):
        r = make(CommandStatus.RATE_LIMITED, success=False, confirmed=False, device_id="d",
                 metadata={"limit": 5, "window_seconds": 1.0, "retry_after": 0.4})
        with pytest.raises(RateLimitError) as exc:
            r.raise_for_error()
        assert exc.value.limit == 5
        assert exc.value.retry_after == 0.4
        assert exc.value.error_code == "RATE_LIMITED"

    def test_raise_for_error_circuit_open(self):
        r = make(CommandStatus.CIRCUIT_OPEN, success=False, confirmed=False, device_id="relay",
                 metadata={"open_until": 123.0})
        with pytest.raises(CircuitOpenError) as exc:
            r.raise_for_error()
        assert exc.value.device_id == "relay"
        assert exc.value.open_until == 123.0

    def test_raise_for_error_timeout_is_confirmation_error(self):
        r = make(CommandStatus.TIMEOUT, success=False, confirmed=False, device_id="d",
                 command="on", error="not confirmed")
        with pytest.raises(ConfirmationError) as exc:
            r.raise_for_error()
        assert isinstance(exc.value, CommandError)
        assert exc.value.result is r
        assert exc.value.command == "on"
        assert "not confirmed" in str(exc.value)

    def test_raise_for_error_failed_is_command_error(self):
        r = make(CommandStatus.FAILED, success=False, confirmed=False, device_id="d", command="off")
        with pytest.raises(CommandError) as exc:
            r.raise_for_error()
        assert not isinstance(exc.value, ConfirmationError)
        assert exc.value.result is r
        assert exc.value.details["device_id"] == "d" and exc.value.details["command"] == "off"

    def test_command_error_details_include_failed_result(self):
        r = make(CommandStatus.FAILED, success=False, confirmed=False, device_id="d", command="off")
        with pytest.raises(CommandError) as exc:
            r.raise_for_error()
        assert exc.value.details["result"]["status"] == "FAILED"

    def test_to_dict_is_json_serialisable(self):
        r = make(device_id="d", command="on", latency_ms=12.34567, raw_response=b"\xffok",
                 metadata={"k": 1})
        d = r.to_dict()
        assert d["status"] == "CONFIRMED"
        assert d["latency_ms"] == 12.346
        assert isinstance(d["raw_response"], str) and d["raw_response"].endswith("ok")
        json.dumps(d)

    def test_repr_variants(self):
        assert "SUCCESS" in repr(make(device_id="a"))
        assert "UNCONFIRMED" in repr(make(CommandStatus.ACKNOWLEDGED, confirmed=False, device_id="a"))
        failed = make(CommandStatus.FAILED, success=False, confirmed=False, error="boom")
        assert "FAILED" in repr(failed) and "boom" in repr(failed)


class TestBatchResult:
    def _batch(self):
        results = [
            make(device_id="a"),
            make(CommandStatus.ACKNOWLEDGED, confirmed=False, device_id="b"),
            make(CommandStatus.FAILED, success=False, confirmed=False, device_id="c"),
        ]
        return BatchResult(results=results, total=3, successful=2, failed=1, confirmed=1,
                           total_latency_ms=5.0)

    def test_aggregates(self):
        b = self._batch()
        assert b.success_rate == pytest.approx(66.666, rel=1e-3)
        assert b.any_failed and not b.all_succeeded
        assert [r.device_id for r in b.get_failures()] == ["c"]
        assert b.get_by_device("b").status == CommandStatus.ACKNOWLEDGED
        assert b.get_by_device("zzz") is None
        assert len(b) == 3 and [r.device_id for r in b] == ["a", "b", "c"]
        assert "2/3" in repr(b)

    def test_empty_batch_success_rate_is_zero(self):
        assert BatchResult([], 0, 0, 0, 0, 0.0).success_rate == 0.0

    def test_to_dict(self):
        d = self._batch().to_dict()
        assert d["total"] == 3 and len(d["results"]) == 3
        json.dumps(d)


class TestExceptions:
    def test_hierarchy(self):
        assert issubclass(ProtocolError, DeviceError)
        assert issubclass(StateError, DeviceError)
        assert issubclass(DeviceError, FuseIoTError)
        assert issubclass(ValidationError, ConfigurationError)
        assert issubclass(AuthenticationError, FuseIoTError)

    def test_str_and_to_dict(self):
        e = FuseIoTError("bad thing", details={"x": 1})
        assert str(e) == "[FuseIoTError] bad thing (details: {'x': 1})"
        assert str(FuseIoTError("plain")) == "[FuseIoTError] plain"
        assert e.to_dict() == {"error_type": "FuseIoTError", "error_code": "FuseIoTError",
                               "message": "bad thing", "details": {"x": 1}}

    def test_protocol_error_details(self):
        e = ProtocolError("down", "http", "http://x", original_error=OSError("refused"), status=503)
        assert e.error_code == "PROTOCOL_HTTP_ERROR"
        assert e.details["status"] == 503
        assert e.details["original_error"] == "refused"
        assert e.endpoint == "http://x"

    def test_validation_error_field(self):
        e = ValidationError("bad", field="port")
        assert e.field == "port" and e.details == {"field": "port"}
