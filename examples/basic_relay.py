"""
Basic relay control with confirmation.

    python examples/basic_relay.py                       # against a simulated relay
    python examples/basic_relay.py http://192.168.1.45   # against your device
    python examples/basic_relay.py http://192.168.1.45 toggle
"""
import sys

from fuseiot import HTTP, Hub, SimulatedHTTPDevice, Switchable


def run(url: str, command: str) -> int:
    with Hub() as hub:
        relay = hub.add_device(HTTP(url, timeout=2.0), Switchable, device_id="lab_relay")
        print(f"Connected to {url}; relay is {'ON' if relay.is_on else 'OFF'}")

        result = getattr(relay, command)()
        if result.confirmed:
            print(f"{command}: confirmed in {result.latency_ms:.0f} ms -> {result.state_after}")
            return 0
        print(f"{command}: FAILED [{result.status.name}] {result.error}")
        return 1


def main() -> int:
    args = sys.argv[1:]
    command = args[1] if len(args) > 1 else "toggle"
    if command not in Switchable.commands:
        print(f"Unknown command {command!r}; use one of {Switchable.commands}")
        return 2
    if args:
        return run(args[0], command)
    with SimulatedHTTPDevice("switchable", settle_ms=100) as device:
        print(f"(simulated relay at {device.url})")
        return run(device.url, command)


if __name__ == "__main__":
    sys.exit(main())
