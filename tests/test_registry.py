"""Declarative device specs: validation, protocol construction and loading into a hub."""
import pytest

from fuseiot import HTTP, HTTPS, MQTT, MQTTS, Sensor, Serial, SmartPlug, Switchable, VirtualDevice, WebSocket
from fuseiot.exceptions import ConfigurationError
from fuseiot.registry import (
    add_device_from_spec,
    build_protocol,
    capability_classes,
    load_devices,
    parse_device,
    resolve_capability,
)
from fuseiot.utils.validation import CommandModel, DeviceModel, StateModel


def spec(**kw):
    base = {"id": "dev", "protocol": "virtual", "capability": "switchable"}
    base.update(kw)
    return base


class TestParseDevice:
    def test_valid_and_normalised(self):
        m = parse_device(spec(protocol=" HTTP ", capability="Switchable", endpoint="http://x/api",
                              name="Lamp", tags={"room": "hall"}))
        assert isinstance(m, DeviceModel)
        assert (m.device_id, m.protocol, m.capability) == ("dev", "http", "switchable")
        assert m.name == "Lamp" and m.tags == {"room": "hall"}

    def test_device_id_key_accepted(self):
        entry = {"device_id": "d2", "protocol": "virtual", "capability": "sensor"}
        assert parse_device(entry).device_id == "d2"

    @pytest.mark.parametrize("entry,fragment", [
        (spec(protocol="zigbee"), "unknown protocol"),
        (spec(capability="toaster"), "unknown capability"),
        (spec(id="bad id!"), "pattern"),
        (spec(id=""), "id"),
        (spec(protocol="http"), "'endpoint' is required"),
        (spec(protocol="http", endpoint="ftp://x"), "http://"),
        (spec(protocol="websocket", endpoint="http://x"), "ws://"),
        (spec(colour="red"), "colour"),
        (spec(command={"timeout": 0}), "command.timeout"),
        (spec(command={"retry_attempts": 99}), "retry_attempts"),
        (spec(command={"unknown": 1}), "unknown"),
        ({"protocol": "virtual", "capability": "switchable"}, "<missing id>"),
    ])
    def test_invalid(self, entry, fragment):
        with pytest.raises(ConfigurationError) as exc:
            parse_device(entry)
        assert fragment in str(exc.value)

    def test_non_mapping_entry(self):
        with pytest.raises(ConfigurationError):
            parse_device("lamp")


class TestResolveCapability:
    def test_names(self):
        assert resolve_capability(" SENSOR ") is Sensor
        assert set(capability_classes()) == {"switchable", "dimmable", "sensor", "motor", "thermostat",
                                             "lock", "rgb_light", "energy_monitor", "smart_plug"}
        with pytest.raises(ConfigurationError, match="Unknown capability"):
            resolve_capability("blender")


class TestBuildProtocol:
    def test_http_and_https(self):
        p = build_protocol(parse_device(spec(protocol="http", endpoint="http://h/api",
                                             options={"timeout": 2, "bearer_token": "t"})))
        assert isinstance(p, HTTP) and p.base_url == "http://h/api" and p.timeout == 2.0
        assert p.bearer_token == "t"
        s = build_protocol(parse_device(spec(protocol="https", options={"base_url": "h2",
                                                                         "ca_bundle": "/ca.pem"})))
        assert isinstance(s, HTTPS) and s.base_url == "https://h2" and s.verify_ssl == "/ca.pem"

    def test_mqtt_endpoint_parsing(self):
        p = build_protocol(parse_device(spec(
            protocol="mqtt", endpoint="mqtt://user:p%40ss@broker.lan:1884/home/hall/temp",
            options={"qos": 0})))
        assert isinstance(p, MQTT) and not isinstance(p, MQTTS)
        assert (p.broker, p.port, p.username, p.password) == ("broker.lan", 1884, "user", "p@ss")
        assert p.topic_prefix == "home/hall/temp" and p.qos == 0

    def test_mqtt_options_win_and_bare_host(self):
        p = build_protocol(parse_device(spec(protocol="mqtt", endpoint="broker.lan/a",
                                             options={"topic_prefix": "override"})))
        assert p.broker == "broker.lan" and p.port == 1883 and p.topic_prefix == "override"
        q = build_protocol(parse_device(spec(protocol="mqtt", options={"broker": "b2"})))
        assert q.broker == "b2"

    def test_mqtts(self):
        p = build_protocol(parse_device(spec(protocol="mqtts", endpoint="b.lan/x",
                                             options={"ca_certs": "/ca.pem"})))
        assert isinstance(p, MQTTS) and p.port == 8883 and p.ca_certs == "/ca.pem"

    def test_mqtt_without_broker(self):
        with pytest.raises(ConfigurationError, match="MQTT device needs an endpoint"):
            build_protocol(parse_device(spec(protocol="mqtt", endpoint="mqtt://:1883/x")))

    def test_websocket_serial(self):
        w = build_protocol(parse_device(spec(protocol="websocket", endpoint="ws://h:81/ws",
                                             options={"timeout": 1})))
        assert isinstance(w, WebSocket) and w.uri == "ws://h:81/ws" and w.config.timeout == 1.0
        s = build_protocol(parse_device(spec(protocol="serial", endpoint="COM3",
                                             options={"baudrate": 115200})))
        assert isinstance(s, Serial) and s.port == "COM3" and s.baudrate == 115200

    def test_virtual_device_type_resolution(self):
        assert build_protocol(parse_device(spec(capability="dimmable"))).device_type == "dimmable"
        assert build_protocol(parse_device(spec(endpoint="sensor"))).device_type == "sensor"
        v = build_protocol(parse_device(spec(options={"device_type": "lock", "latency_ms": 5})))
        assert isinstance(v, VirtualDevice) and v.device_type == "lock" and v.sim.latency_ms == 5

    @pytest.mark.parametrize("entry", [
        spec(options={"warp_speed": 9}),
        spec(protocol="http", endpoint="http://h", options={"nonsense": 1}),
        spec(protocol="serial", options={"baudrate": 9600}),
    ])
    def test_bad_options(self, entry):
        with pytest.raises(ConfigurationError, match="Invalid .* options"):
            build_protocol(parse_device(entry))


class TestLoading:
    def test_add_device_from_spec_applies_overrides(self, hub):
        dev = add_device_from_spec(hub, spec(
            id="t", capability="sensor", name="Hall temp",
            command={"timeout": 0.5, "retry_attempts": 2, "poll_interval": 0.02, "confirm": False},
            capability_options={"unit": "fahrenheit"}))
        assert isinstance(dev, Sensor) and hub["t"] is dev
        assert dev.unit == "fahrenheit"
        cfg = dev.config
        assert (cfg.name, cfg.timeout, cfg.retry_attempts, cfg.poll_interval, cfg.confirm) == \
            ("Hall temp", 0.5, 2, 0.02, False)
        assert dev.protocol.is_connected

    def test_unreachable_device_is_still_registered(self, hub, closed_port):
        dev = add_device_from_spec(hub, spec(id="far", protocol="http",
                                             endpoint=f"http://127.0.0.1:{closed_port}",
                                             options={"timeout": 1}))
        assert "far" in hub and not dev.protocol.is_connected

    def test_connect_true_fails_fast(self, hub, closed_port):
        with pytest.raises(ConfigurationError, match="Cannot connect"):
            add_device_from_spec(hub, spec(id="far", protocol="http",
                                           endpoint=f"http://127.0.0.1:{closed_port}",
                                           options={"timeout": 1}), connect=True)

    def test_load_devices(self, hub):
        devices = load_devices(hub, [spec(id="a"), spec(id="plug", capability="smart_plug"),
                                     {"id": "m", "protocol": "virtual", "capability": "motor",
                                      "capability_options": {"position_range": [0, 50]}}])
        assert [d.id for d in devices] == ["a", "plug", "m"]
        assert isinstance(hub["plug"], SmartPlug) and isinstance(hub["a"], Switchable)
        assert hub["m"].move_to(80).state_after["position"] == 50.0

    def test_duplicate_ids_rejected_before_loading(self, hub):
        with pytest.raises(ConfigurationError, match="Duplicate device ids in config: a"):
            load_devices(hub, [spec(id="a"), spec(id="b"), spec(id="a")])
        assert len(hub) == 0

    def test_invalid_entry_rejected_before_loading(self, hub):
        with pytest.raises(ConfigurationError, match="bad"):
            load_devices(hub, [spec(id="good"), spec(id="bad", capability="toaster")])
        assert len(hub) == 0

    def test_bad_protocol_options_leave_hub_untouched(self, hub):
        with pytest.raises(ConfigurationError):
            load_devices(hub, [spec(id="good"), spec(id="bad", options={"warp_speed": 9})])
        assert len(hub) == 0


class TestValidationModels:
    def test_command_model(self):
        cmd = CommandModel(device_id="lamp", command="set_brightness", params={"brightness": 3})
        assert cmd.params == {"brightness": 3}
        for bad in ({"device_id": "x", "command": "On!"}, {"device_id": "x", "command": "_private"},
                    {"device_id": "x", "command": "on", "extra": 1}):
            with pytest.raises(Exception):
                CommandModel.model_validate(bad)

    def test_state_model(self):
        assert StateModel(device_id="a", timestamp=1.0, values={}).confidence == 1.0
        with pytest.raises(Exception):
            StateModel(device_id="a", timestamp=1.0, values={}, confidence=2)
