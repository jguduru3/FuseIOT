"""Cloud connectors: command validation, state mirroring and the MQTT bridge."""
import json
from typing import Any, Callable, Dict, List

import pytest

from fuseiot import Hub, HubConfig, Switchable, Thermostat, VirtualDevice
from fuseiot.cloud import AWSIoT, AzureIoT, CloudConnector, GCPIoT, MQTTBridge, connector_from_config
from fuseiot.exceptions import CloudError, ConfigurationError
from fuseiot.protocols.mqtts import MQTTS
from tests.helpers import fast_config


class DummyConnector(CloudConnector):
    def __init__(self, hub, connect_ok=True):
        super().__init__(hub)
        self.connect_ok = connect_ok
        self.published: List[Any] = []
        self.command_callback: Callable[[Dict[str, Any]], Any] = None  # type: ignore[assignment]
        self.disconnected = False

    def connect(self):
        self._connected = self.connect_ok
        return self.connect_ok

    def disconnect(self):
        self.disconnected = True
        self._connected = False

    def publish_state(self, device_id, state):
        self.published.append((device_id, state))
        return True

    def subscribe_commands(self, callback):
        self.command_callback = callback


@pytest.fixture
def cloud_hub(hub):
    hub.add_device(VirtualDevice(), Switchable, device_id="lamp", config=fast_config())
    hub.add_device(VirtualDevice("thermostat"), Thermostat, device_id="thermo", config=fast_config())
    return hub


class TestHandleCommand:
    def test_valid_command(self, cloud_hub):
        result = DummyConnector(cloud_hub).handle_command(
            {"device_id": "lamp", "command": "on", "params": {"timeout": 1}})
        assert result["success"] is True and result["status"] == "CONFIRMED"
        assert cloud_hub["lamp"].protocol.state["power"] is True

    @pytest.mark.parametrize("raw", [
        json.dumps({"device_id": "thermo", "command": "set_temperature", "params": {"temperature": 24}}),
        json.dumps({"device_id": "thermo", "command": "set_temperature",
                    "params": {"temperature": 24}}).encode(),
    ])
    def test_json_text_and_bytes(self, cloud_hub, raw):
        assert DummyConnector(cloud_hub).handle_command(raw)["success"] is True

    @pytest.mark.parametrize("message,error", [
        ("{not json", "invalid JSON"),
        ({"device_id": "lamp"}, "invalid command message"),
        ({"device_id": "lamp", "command": "On"}, "invalid command message"),
        ({"device_id": "lamp", "command": "on", "sudo": True}, "invalid command message"),
        ({"device_id": "ghost", "command": "on"}, "unknown device"),
        ({"device_id": "lamp", "command": "read_state"}, "not allowed"),
        ({"device_id": "lamp", "command": "disconnect"}, "not allowed"),
        ({"device_id": "lamp", "command": "on", "params": {"volume": 11}}, "bad params"),
        ({"device_id": "thermo", "command": "set_mode", "params": {"mode": "plasma"}}, "bad params"),
    ])
    def test_rejections(self, cloud_hub, message, error):
        result = DummyConnector(cloud_hub).handle_command(message)
        assert result["success"] is False and error in result["error"]
        assert cloud_hub["lamp"].protocol.state["power"] is False


class TestAttach:
    def test_mirrors_state_and_routes_commands(self, cloud_hub):
        conn = DummyConnector(cloud_hub)
        conn.attach()
        cloud_hub["lamp"].on()
        assert conn.published == [("lamp", {"power": True})]
        assert conn.command_callback({"device_id": "lamp", "command": "off"})["success"] is True
        conn.detach()
        cloud_hub["lamp"].on()
        assert conn.published == [("lamp", {"power": True}), ("lamp", {"power": False})]

    def test_attach_requires_event_bus(self):
        with Hub(HubConfig(enable_events=False)) as h:
            with pytest.raises(CloudError):
                DummyConnector(h).attach()

    def test_context_manager(self, cloud_hub):
        with DummyConnector(cloud_hub) as conn:
            assert conn.is_connected
        assert conn.disconnected
        with pytest.raises(CloudError, match="failed to connect"):
            with DummyConnector(cloud_hub, connect_ok=False):
                pass


class TestMQTTBridge:
    def test_bridge_round_trip(self, cloud_hub, fake_mqtt):
        bridge = MQTTBridge(cloud_hub, "broker.test", client_id="hub-1", tls=False)
        assert bridge.base_topic == "fuseiot/hub-1"
        assert bridge.publish_state("lamp", {"power": True}) is False  # not connected yet
        assert bridge.connect() is True
        bridge.attach()
        client = fake_mqtt.instances[0]
        assert client.connect_args[1] == 1883 and client.tls_args is None

        cloud_hub["lamp"].on()
        assert fake_mqtt.retained["fuseiot/hub-1/state/lamp"] == b'{"power": true}'

        fake_mqtt.publish("fuseiot/hub-1/commands",
                          json.dumps({"device_id": "lamp", "command": "off"}))
        results = fake_mqtt.messages("fuseiot/hub-1/results")
        assert results and results[0]["success"] is True and results[0]["command"] == "off"
        fake_mqtt.publish("fuseiot/hub-1/commands", json.dumps({"device_id": "lamp", "command": "rm"}))
        assert "not allowed" in fake_mqtt.messages("fuseiot/hub-1/results")[-1]["error"]

        bridge.detach()
        bridge.disconnect()
        assert not bridge.is_connected

    def test_tls_defaults(self, cloud_hub, fake_mqtt):
        bridge = MQTTBridge(cloud_hub, "broker.test", base_topic="/site/a/", ca_certs="/ca.pem")
        assert isinstance(bridge._mqtt, MQTTS) and bridge._mqtt.port == 8883
        assert bridge.base_topic == "site/a"
        assert bridge.connect()
        assert fake_mqtt.instances[0].tls_args["ca_certs"] == "/ca.pem"
        bridge.disconnect()

    def test_connect_failure(self, cloud_hub, fake_mqtt):
        fake_mqtt.refuse_connect = True
        bridge = MQTTBridge(cloud_hub, "broker.test", tls=False)
        assert bridge.connect() is False
        with pytest.raises(CloudError, match="not connected"):
            bridge.subscribe_commands(lambda m: None)

    def test_aws_iot_is_an_mqtts_bridge(self, cloud_hub, fake_mqtt):
        aws = AWSIoT(cloud_hub, endpoint="abc-ats.iot.eu-west-1.amazonaws.com", client_id="thing-1",
                     cert_path="/c.pem", key_path="/k.pem", ca_path="/ca.pem")
        assert aws.base_topic == "fuseiot/thing-1"
        assert isinstance(aws._mqtt, MQTTS) and aws._mqtt.port == 8883
        assert aws.connect()
        args = fake_mqtt.instances[0].tls_args
        assert (args["certfile"], args["keyfile"], args["ca_certs"]) == ("/c.pem", "/k.pem", "/ca.pem")
        aws.disconnect()


class TestOtherConnectors:
    def test_gcp_is_retired(self, cloud_hub):
        gcp = GCPIoT(cloud_hub, project="p")
        with pytest.raises(CloudError, match="retired"):
            gcp.connect()
        with pytest.raises(CloudError):
            gcp.publish_state("lamp", {})
        with pytest.raises(CloudError):
            gcp.subscribe_commands(lambda m: None)
        gcp.disconnect()
        assert not gcp.is_connected

    def test_azure_without_sdk(self, cloud_hub):
        try:
            import azure.iot.device  # noqa: F401
            pytest.skip("azure-iot-device is installed")
        except ImportError:
            pass
        az = AzureIoT(cloud_hub, connection_string="HostName=x;DeviceId=y;SharedAccessKey=z")
        with pytest.raises(CloudError, match="azure-iot-device"):
            az.connect()
        assert az.publish_state("lamp", {}) is False
        with pytest.raises(CloudError, match="not connected"):
            az.subscribe_commands(lambda m: None)

    def test_connector_from_config(self, cloud_hub, fake_mqtt):
        bridge = connector_from_config(cloud_hub, "MQTT", {"broker": "b", "tls": False})
        assert isinstance(bridge, MQTTBridge)
        assert isinstance(connector_from_config(cloud_hub, "gcp", {}), GCPIoT)
        with pytest.raises(ConfigurationError, match="Unknown cloud_provider"):
            connector_from_config(cloud_hub, "ibm", {})
        with pytest.raises(ConfigurationError, match="Unknown cloud_provider"):
            connector_from_config(cloud_hub, None, {})  # type: ignore[arg-type]
        with pytest.raises(ConfigurationError, match="Invalid cloud_options"):
            connector_from_config(cloud_hub, "aws", {"endpoint": "x"})
