"""MQTT / MQTTS transports against an in-memory fake paho client and broker."""
import json
import ssl

import pytest

from fuseiot import MQTT, MQTTS, Sensor, Switchable
from fuseiot.exceptions import ProtocolError
from fuseiot.protocols.base import ConnectionState
from tests.helpers import MQTTDevice, fast_config


def make_mqtt(**kw):
    kw.setdefault("topic_prefix", "home/plug")
    kw.setdefault("timeout", 0.3)
    return MQTT("broker.test", **kw)


class TestMQTTConnection:
    def test_connect_subscribes_to_prefix(self, fake_mqtt):
        m = make_mqtt(username="u", password="p", port=1884, keepalive=30)
        assert m.connect() is True
        client = fake_mqtt.instances[0]
        assert client.connect_args == ("broker.test", 1884, 30)
        assert "home/plug/#" in client.subscriptions
        assert client.credentials == ("u", "p")
        assert client.callback_api_version is not None  # paho 2.x API selected
        assert client.loop_running and m.is_connected
        assert m.connect() is True and len(fake_mqtt.instances) == 1
        m.disconnect()
        assert not m.is_connected and not client.loop_running

    def test_endpoint_and_topics(self, fake_mqtt):
        m = make_mqtt(topic_prefix="/home/plug/")
        assert m.topic_prefix == "home/plug"
        assert m.endpoint == "mqtt://broker.test:1883/home/plug"
        assert m.topic("/set/on/") == "home/plug/set/on"
        assert m.topic("") == "home/plug"
        assert m.client_id.startswith("fuseiot-")

    def test_connack_refused(self, fake_mqtt):
        fake_mqtt.connack_rc = 5
        m = make_mqtt()
        assert m.connect() is False
        assert m.connection_state is ConnectionState.FAILED
        assert "refused" in m.last_error.lower() or "timed out" in m.last_error.lower()

    def test_tcp_failure(self, fake_mqtt):
        fake_mqtt.refuse_connect = True
        m = make_mqtt()
        assert m.connect() is False
        assert "ConnectionRefusedError" in m.last_error
        with pytest.raises(ProtocolError, match="Not connected"):
            m.send({"_path": "/on"})

    def test_unexpected_disconnect(self, fake_mqtt):
        m = make_mqtt()
        m.connect()
        fake_mqtt.instances[0].drop(rc=7)
        assert m.connection_state is ConnectionState.RECONNECTING
        assert "Unexpected disconnect" in m.last_error

    def test_reconnect_on_send_after_drop(self, fake_mqtt):
        fake_mqtt.devices.append(MQTTDevice("home/plug"))
        m = make_mqtt()
        m.connect()
        fake_mqtt.instances[0].drop()
        assert m.send({"_method": "POST", "_path": "/on", "power": True})["published"] is True

    @pytest.mark.parametrize("rc,expected", [(0, 0), (5, 5)])
    def test_rc_value_accepts_ints_and_reason_codes(self, rc, expected):
        class Reason:
            def __init__(self, value):
                self.value = value

        assert MQTT._rc_value(rc) == expected
        assert MQTT._rc_value(Reason(rc)) == expected

    def test_rc_value_non_numeric(self):
        class Failure:
            value = "bad"
            is_failure = True

        assert MQTT._rc_value(Failure()) == 1


class TestMQTTMessaging:
    def test_post_publishes_to_set_topic(self, fake_mqtt):
        m = make_mqtt()
        result = m.send({"_method": "POST", "_path": "/on", "power": True})
        assert result == {"published": True, "topic": "home/plug/set/on"}
        assert fake_mqtt.messages("home/plug/set/on") == [{"power": True}]

    def test_post_without_path(self, fake_mqtt):
        m = make_mqtt()
        assert m.send({"x": 1})["topic"] == "home/plug/set"

    def test_get_returns_retained_state(self, fake_mqtt):
        fake_mqtt.publish("home/plug/state", json.dumps({"power": True}), retain=True)
        m = make_mqtt()
        assert m.send({"_method": "GET", "_path": "/state"}) == {"power": True}
        assert fake_mqtt.messages("home/plug/state/get") == [], "retained state needs no request"

    def test_get_requests_state_when_none_cached(self, fake_mqtt):
        fake_mqtt.devices.append(MQTTDevice("home/plug", "sensor", initial_state={"value": 4.5}))
        m = make_mqtt()
        reading = m.send({"_method": "GET", "_path": "/reading"})
        assert reading["value"] == 4.5
        assert len(fake_mqtt.messages("home/plug/reading/get")) == 1

    def test_get_times_out_without_device(self, fake_mqtt):
        m = make_mqtt(timeout=0.1)
        with pytest.raises(ProtocolError, match="No message on home/plug/state"):
            m.send({"_method": "GET", "_path": "/state"})
        assert m.health_check()["metrics"]["errors"] == 1

    def test_explicit_topics(self, fake_mqtt):
        def responder(broker, topic, payload):
            if topic == "home/plug/rpc/ping":
                broker.publish("home/plug/rpc/pong", json.dumps({"pong": json.loads(payload)["n"]}))

        fake_mqtt.devices.append(responder)
        m = make_mqtt()
        assert m.send({"_topic": "rpc/ping", "_response_topic": "rpc/pong", "n": 3}) == {"pong": 3}
        assert m.send({"_topic": "rpc/fire", "n": 1}) == {"published": True, "topic": "home/plug/rpc/fire"}

    def test_unacknowledged_publish(self, fake_mqtt):
        fake_mqtt.ack_publishes = False
        m = make_mqtt()
        with pytest.raises(ProtocolError, match="not acknowledged"):
            m.send({"_path": "/on"})

    def test_qos0_does_not_wait_for_ack(self, fake_mqtt):
        fake_mqtt.ack_publishes = False
        m = make_mqtt(qos=0)
        assert m.send({"_path": "/on"})["published"] is True

    def test_non_json_and_scalar_messages(self, fake_mqtt):
        m = make_mqtt()
        m.connect()
        fake_mqtt.publish("home/plug/state", "on")
        assert m.latest("home/plug/state") == {"raw": "on"}
        fake_mqtt.publish("home/plug/level", "42")
        assert m.send({"_method": "GET", "_path": "/level"}) == {"value": 42}

    def test_subscribe_handlers(self, fake_mqtt):
        m = make_mqtt()
        got = []
        m.subscribe("events/+", lambda topic, payload: got.append((topic, payload)))
        m.connect()
        m.subscribe("alarms", lambda topic, payload: 1 / 0)  # errors are logged, not raised
        fake_mqtt.publish("home/plug/events/button", json.dumps({"pressed": True}))
        fake_mqtt.publish("home/plug/alarms", "{}")
        fake_mqtt.publish("home/plug/other", "{}")
        assert got == [("home/plug/events/button", {"pressed": True})]


class TestMQTTEndToEnd:
    def test_switchable_over_mqtt(self, hub, fake_mqtt):
        device = MQTTDevice("home/plug")
        fake_mqtt.devices.append(device)
        lamp = hub.add_device(make_mqtt(), Switchable, device_id="plug", config=fast_config())
        r = lamp.on()
        assert r.confirmed and r.state_after == {"power": True}
        assert device.virtual.state["power"] is True
        assert lamp.off().confirmed

    def test_sensor_over_mqtt(self, hub, fake_mqtt):
        fake_mqtt.devices.append(MQTTDevice("lab/temp", "sensor", initial_state={"value": 18.25}))
        temp = hub.add_device(make_mqtt(topic_prefix="lab/temp"), Sensor, device_id="t", config=fast_config())
        assert temp.read(fresh=True)["value"] == 18.25


class TestMQTTS:
    def test_tls_configuration(self, fake_mqtt):
        m = MQTTS("broker.test", ca_certs="/ca.pem", certfile="/c.pem", keyfile="/k.pem",
                  topic_prefix="x", timeout=0.3)
        assert m.port == 8883 and m.scheme == "mqtts"
        assert m.endpoint == "mqtts://broker.test:8883/x"
        assert m.connect()
        client = fake_mqtt.instances[0]
        assert client.tls_args["ca_certs"] == "/ca.pem"
        assert client.tls_args["certfile"] == "/c.pem" and client.tls_args["keyfile"] == "/k.pem"
        assert client.tls_args["cert_reqs"] == ssl.CERT_REQUIRED
        assert client.tls_insecure is None
        m.disconnect()

    def test_tls_insecure(self, fake_mqtt):
        m = MQTTS("broker.test", tls_insecure=True, timeout=0.3)
        m.connect()
        assert fake_mqtt.instances[0].tls_insecure is True
        m.disconnect()
