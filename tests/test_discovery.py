"""Turning mDNS results for FuseIoT firmware into device config entries."""
from fuseiot.discovery.mdns import DiscoveryResult, config_entries
from fuseiot.registry import parse_device

FUSE = "_fuseiot._tcp.local."


def result(name="kitchen-light._fuseiot._tcp.local.", address="192.168.1.20", port=80, device_type=FUSE,
           hostname="kitchen-light.local.", **props):
    return DiscoveryResult(name=name, address=address, port=port, device_type=device_type, protocol="http",
                           properties={k: str(v) for k, v in props.items()}, hostname=hostname,
                           addresses=[address])


def test_basic_entry_uses_hostname_and_default_port():
    entries = config_entries([result(id="kitchen_light", cap="switchable")])
    assert entries == [{"id": "kitchen_light", "name": "Kitchen light", "capability": "switchable",
                        "protocol": "http", "endpoint": "http://kitchen-light.local"}]


def test_non_default_port_kept():
    entries = config_entries([result(port=8080, id="a", cap="dimmable")])
    assert entries[0]["endpoint"] == "http://kitchen-light.local:8080"


def test_port_zero_omitted():
    assert config_entries([result(port=0, id="a", cap="sensor")])[0]["endpoint"] == "http://kitchen-light.local"


def test_falls_back_to_address_and_brackets_ipv6():
    assert config_entries([result(hostname=None, id="a", cap="sensor")])[0]["endpoint"] == "http://192.168.1.20"
    v6 = config_entries([result(hostname=None, address="fe80::2", port=81, id="b", cap="sensor")])
    assert v6[0]["endpoint"] == "http://[fe80::2]:81"


def test_auth_flag_adds_token_placeholder():
    entries = config_entries([result(id="front-door", cap="lock", auth=1),
                              result(name="x._fuseiot._tcp.local.", id="garage", cap="switchable", auth=0)])
    assert entries[0]["options"] == {"bearer_token": "${FRONT_DOOR_TOKEN}"}
    assert entries[0]["name"] == "Front door"
    assert "options" not in entries[1]


def test_id_defaults_to_instance_name():
    entries = config_entries([result(name="porch._fuseiot._tcp.local.", cap="switchable")])
    assert entries[0]["id"] == "porch"


def test_skips_other_services_unknown_capabilities_and_duplicates():
    results = [
        result(device_type="_http._tcp.local.", id="printer", cap="switchable"),
        result(id="toaster", cap="toaster"),
        result(id="nocap"),
        result(id="lamp", cap="switchable"),
        result(name="lamp2._fuseiot._tcp.local.", id="lamp", cap="dimmable"),
    ]
    entries = config_entries(results)
    assert [(e["id"], e["capability"]) for e in entries] == [("lamp", "switchable")]


def test_empty():
    assert config_entries([]) == []


def test_entries_are_valid_device_config():
    entries = config_entries([result(id="kitchen_light", cap="switchable", auth=1),
                              result(name="t._fuseiot._tcp.local.", hostname=None, address="10.0.0.5", port=8080,
                                     id="hall_temp", cap="sensor")])
    for entry in entries:
        assert parse_device(entry).device_id == entry["id"]
