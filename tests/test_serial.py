"""Serial (JSON lines) transport with a fake pyserial port."""
import json

import pytest

import fuseiot.protocols.serial as serial_module
from fuseiot import Motor, Serial, VirtualDevice
from fuseiot.exceptions import ProtocolError
from fuseiot.protocols.base import ConnectionState
from tests.helpers import FakeSerialPort, fast_config, virtual_serial_responder


@pytest.fixture
def fake_port(monkeypatch):
    """Patch serial_for_url; returns a holder with the opened ports and call kwargs."""

    class Holder:
        device = VirtualDevice("switchable")
        ports = []
        calls = []
        fail = False
        responder = None

    holder = Holder()

    def serial_for_url(url, **kwargs):
        holder.calls.append((url, kwargs))
        if holder.fail:
            raise serial_module.pyserial.SerialException(f"could not open port {url}")
        port = FakeSerialPort(holder.responder or virtual_serial_responder(holder.device))
        holder.ports.append(port)
        return port

    monkeypatch.setattr(serial_module.pyserial, "serial_for_url", serial_for_url)
    return holder


def test_connect_passes_line_settings(fake_port):
    s = Serial("COM7", baudrate=115200, timeout=0.5, parity="E", stopbits=2, bytesize=7)
    assert s.connect() is True and s.is_connected
    url, kwargs = fake_port.calls[0]
    assert url == "COM7"
    assert kwargs == {"baudrate": 115200, "bytesize": 7, "parity": "E", "stopbits": 2,
                      "timeout": 0.5, "write_timeout": 0.5}
    assert s.endpoint == "serial:COM7"
    assert s.connect() is True and len(fake_port.calls) == 1
    s.disconnect()
    assert not fake_port.ports[0].is_open
    assert s.connection_state is ConnectionState.DISCONNECTED


def test_send_writes_json_line(fake_port):
    s = Serial("loop://")
    assert s.send({"_method": "GET", "_path": "/state"}) == {"power": False}
    s.send({"_method": "POST", "_path": "/on", "power": True, "_internal": "x"})
    port = fake_port.ports[0]
    assert port.written[1].endswith(b"\n")
    assert port.requests()[1] == {"power": True, "method": "POST", "path": "/on"}
    assert port.resets == 2 and port.flushed == 2
    assert fake_port.device.state["power"] is True


def test_connect_failure(fake_port):
    fake_port.fail = True
    s = Serial("/dev/ttyUSB9")
    assert s.connect() is False
    assert s.connection_state is ConnectionState.FAILED
    assert "could not open port" in s.last_error
    with pytest.raises(ProtocolError, match="Not connected"):
        s.send({"_path": "/on"})


@pytest.mark.parametrize("reply,error", [(b"", "No response"), (b"{oops\n", "Invalid JSON")])
def test_bad_replies(fake_port, reply, error):
    fake_port.responder = lambda request: reply
    with pytest.raises(ProtocolError, match=error):
        Serial("loop://").send({"_path": "/on"})


def test_scalar_reply(fake_port):
    fake_port.responder = lambda request: b"17\n"
    assert Serial("loop://").send({"_method": "GET", "_path": "/x"}) == {"value": 17}


def test_io_error(fake_port):
    s = Serial("loop://")
    s.connect()
    fake_port.ports[0].fail_write = True
    with pytest.raises(ProtocolError, match="Serial I/O error"):
        s.send({"_path": "/on"})
    assert s.connection_state is ConnectionState.FAILED
    assert s.health_check()["last_error"] == "device unplugged"


def test_io_error_counted_once(fake_port):
    s = Serial("loop://")
    s.connect()
    fake_port.ports[0].fail_write = True
    with pytest.raises(ProtocolError):
        s.send({"_path": "/on"})
    assert s.health_check()["metrics"]["errors"] == 1


def test_motor_over_serial(hub, fake_port):
    fake_port.device = VirtualDevice("motor")
    motor = hub.add_device(Serial("loop://"), Motor, device_id="blinds", config=fast_config(),
                           position_range=[0, 100])
    r = motor.move_to(250, speed=20)
    assert r.confirmed and r.state_after["position"] == 100.0
    moves = [json.loads(w) for w in fake_port.ports[0].written if b'"/move"' in w]
    assert moves == [{"position": 100.0, "speed": 20.0, "method": "POST", "path": "/move"}]
