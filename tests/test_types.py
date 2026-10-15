"""Value types in fuseiot.types."""
import dataclasses

import pytest

from fuseiot.types import Color, DeviceInfo, HealthStatus, StateVector


class TestColor:
    def test_valid(self):
        c = Color(1, 2, 3)
        assert (c.r, c.g, c.b) == (1, 2, 3)

    @pytest.mark.parametrize("args", [(256, 0, 0), (0, -1, 0), (0, 0, 300), (1.5, 0, 0), ("1", 0, 0)])
    def test_rejects_invalid_components(self, args):
        with pytest.raises(ValueError):
            Color(*args)

    def test_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            Color(1, 2, 3).r = 5  # type: ignore[misc]

    @pytest.mark.parametrize("text", ["#FF8000", "ff8000", "  #ff8000  "])
    def test_from_hex(self, text):
        assert Color.from_hex(text) == Color(255, 128, 0)

    @pytest.mark.parametrize("text", ["#fff", "#12345", "#1234567", "#gg0000"])
    def test_from_hex_invalid(self, text):
        with pytest.raises(ValueError):
            Color.from_hex(text)

    def test_to_hex_roundtrip(self):
        assert Color(0, 171, 255).to_hex() == "#00abff"
        assert Color.from_hex(Color(10, 20, 30).to_hex()) == Color(10, 20, 30)

    @pytest.mark.parametrize("h,expected", [
        (0, Color(255, 0, 0)), (120, Color(0, 255, 0)), (240, Color(0, 0, 255)),
        (360, Color(255, 0, 0)), (480, Color(0, 255, 0)),
    ])
    def test_from_hsv(self, h, expected):
        assert Color.from_hsv(h, 1.0, 1.0) == expected

    def test_from_hsv_value_scales(self):
        assert Color.from_hsv(0, 0.0, 0.5) == Color(128, 128, 128)

    @pytest.mark.parametrize("s,v", [(1.5, 1.0), (1.0, -0.1)])
    def test_from_hsv_rejects_out_of_range(self, s, v):
        with pytest.raises(ValueError):
            Color.from_hsv(0, s, v)

    def test_to_hsv(self):
        h, s, v = Color(0, 0, 255).to_hsv()
        assert (h, s, v) == (240, 1.0, 1.0)
        assert Color(0, 0, 0).to_hsv() == (0, 0, 0)


def test_state_vector_access():
    sv = StateVector(values={"power": True}, timestamp=1.0, source="poll", confidence=0.9)
    assert sv.get("power") is True
    assert sv.get("missing", 5) == 5
    assert "power" in sv and "x" not in sv
    assert sv.to_dict()["confidence"] == 0.9


def test_device_info_defaults():
    info = DeviceInfo(device_id="d", category="switchable", protocol="http",
                      endpoint="http://x", capabilities=["on"])
    assert info.metadata == {}
    assert info.health is HealthStatus.UNKNOWN
