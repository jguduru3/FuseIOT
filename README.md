# FuseIoT

**Deterministic device control SDK for Python.**
Control relays, sensors, motors and smart devices with command confirmation
and consistent state. There's no platform lock-in and no cloud is required.

Every command is checked by reading the device back. A result is only
`confirmed` when the device actually reports the state you asked for.

---

## Features

- ✅ **Deterministic control.** Each command is verified by polling the
  device's state (you can turn this off per call with `confirm=False`).
- ✅ **Multiple protocols.** HTTP/REST, HTTPS (CA bundle, mutual TLS), MQTT,
  MQTTS, WebSocket, and Serial (RS-232/RS-485/USB).
- ✅ **Async support.** Every command has an awaitable `*_async` twin, and
  `Hub.batch()` runs many commands with bounded concurrency.
- ✅ **Resilience.** Per-device circuit breakers, rate limiting, and retries
  with exponential backoff.
- ✅ **Security.** TLS, Basic, bearer and certificate auth for devices. The
  server uses token auth and only allows each device's declared commands.
- ✅ **Observability.** Structured logging (structlog, JSON optional),
  Prometheus `/metrics`, and `/health` and `/ready` endpoints.
- ✅ **Persistence.** State caching in SQLite or Redis, plus state history.
- ✅ **Event system.** State-change notifications by device, wildcard pattern,
  or for all devices, with a live SSE stream.
- ✅ **Batch operations.** Control dozens of devices concurrently.
- ✅ **Automation.** Edge-triggered rules, plus schedules: fixed times,
  sunrise/sunset with offsets, weekdays and cron.
- ✅ **Phone notifications.** ntfy, Telegram, email or webhooks, with
  automatic alerts when a device goes offline or a command fails.
- ✅ **Phone app and accounts.** An installable web app (PWA) with logins and
  viewer, operator and admin roles.
- ✅ **ESP32 firmware.** Ready-made firmware for relays, dimmers, RGB strips,
  sensors, locks and servos, discovered automatically on your network.
- ✅ **Server and dashboard.** `fuseiot serve` gives you a REST API and a web
  UI, ready for Docker or systemd.
- ✅ **Cloud ready.** A generic MQTT bridge, AWS IoT Core and Azure IoT Hub
  (all optional).
- ✅ **Simulation.** Virtual devices and a simulated HTTP device for testing
  without hardware.

---

## Quick start

### Installation

```bash
pip install fuseiot                 # core + HTTP
pip install "fuseiot[all]"          # + async HTTP, MQTT, WebSocket, Serial, Redis, mDNS

# From source
git clone https://github.com/fuseiot/fuseiot.git
cd fuseiot
pip install -e ".[dev]"
```

Requires Python 3.9 or later.

### Try it without hardware

```bash
fuseiot simulate switchable --port 8001          # terminal 1: a fake relay over HTTP
python examples/basic_relay.py http://127.0.0.1:8001 on   # terminal 2
```

Or run the full demo, with eight simulated devices, a rule, the dashboard and
the API:

```bash
fuseiot -c examples/demo.yaml serve      # then open http://127.0.0.1:8080
```

### Basic usage

```python
from fuseiot import Hub, HTTP, Switchable

hub = Hub()

plug = hub.add_device(
    HTTP("http://192.168.1.45/api"),
    Switchable,
    device_id="living_room_light",
)

result = plug.on()                      # confirmation is on by default
if result.confirmed:
    print(f"✓ Light is ON (verified in {result.latency_ms:.1f}ms)")
else:
    print(f"✗ {result.status.name}: {result.error}")

print(f"Current state: {'ON' if plug.is_on else 'OFF'}")   # cached for default_ttl
plug.off(confirm=False)                 # fire-and-forget: ACKNOWLEDGED, not verified
plug.on().raise_for_error()             # or raise instead of checking
```

Your device has to expose `GET /state` and `POST /on` / `POST /off`. See the
[device contract](docs/device-contract.md) for every capability and protocol.

### Async usage

```python
import asyncio
from fuseiot import Hub, HTTP, Switchable

async def control_multiple():
    async with Hub() as hub:
        lights = [
            hub.add_device(HTTP(f"http://192.168.1.{40 + i}/api"), Switchable, device_id=f"light_{i:02d}")
            for i in range(1, 5)
        ]
        results = await asyncio.gather(*[light.on_async() for light in lights])
        print(f"Controlled {sum(r.success for r in results)}/{len(lights)} lights")

asyncio.run(control_multiple())
```

### Configuration file

```yaml
# fuseiot.yaml
default_ttl: 5.0
persistence_enabled: true
persistence_backend: sqlite
log_level: INFO

# Resilience (each device gets its own breaker/limiter)
circuit_breaker_enabled: true
rate_limit_enabled: true

devices:
  - id: living_room_light
    capability: switchable
    protocol: http
    endpoint: http://192.168.1.45/api
```

```python
from fuseiot import Hub, auto_config, load_devices

config = auto_config("fuseiot.yaml")   # file + FUSEIOT_* overrides; from_yaml() reads the file only
hub = Hub(config)
load_devices(hub, config.devices)
hub["living_room_light"].on()
```

Strings can reference environment variables (`${MQTT_PASSWORD}`). With
`auto_config()`, which the CLI and server use, any top-level key can also be
overridden with `FUSEIOT_<KEY>`. A fully commented template covering
every protocol is in [examples/config.yaml](examples/config.yaml).

### CLI usage

```bash
fuseiot discover                                  # mDNS scan of the local network
fuseiot -c fuseiot.yaml validate                  # check config without touching devices
fuseiot -c fuseiot.yaml devices                   # list configured devices
fuseiot -c fuseiot.yaml on living_room_light      # confirmed by default
fuseiot -c fuseiot.yaml off living_room_light --no-confirm
fuseiot -c fuseiot.yaml status living_room_light
fuseiot -c fuseiot.yaml cmd desk_lamp set_brightness brightness=40
fuseiot -c fuseiot.yaml --json status blinds      # machine-readable output
fuseiot -c fuseiot.yaml stats
```

Exit codes are `0` for success, `1` when a command failed or wasn't confirmed,
and `2` for a usage or configuration error. You can also set `FUSEIOT_CONFIG`
instead of passing `-c`.

### Server, dashboard and API

```bash
export FUSEIOT_API_TOKEN=$(openssl rand -hex 32)
fuseiot -c fuseiot.yaml serve --host 0.0.0.0 --port 8080
curl -H "Authorization: Bearer $FUSEIOT_API_TOKEN" -X POST \
     http://localhost:8080/api/devices/living_room_light/commands/on
```

This gives you the dashboard at `/`, the REST API at `/api/*` (see
[docs/rest-api.md](docs/rest-api.md)), `/health`, `/ready`, Prometheus
`/metrics`, and an SSE stream at `/api/events`. Rules from the config run in
the background.

---

## Advanced features

### Event handling

```python
def on_state_change(event):
    print(f"{event.device_id}: {event.old_state} -> {event.new_state}")

hub.on_state_change(on_state_change, device_id="light_01")     # one device
hub.on_state_change(on_state_change, pattern="living_room_*")   # wildcard
unsubscribe = hub.on_state_change(on_state_change)              # all devices
```

### Batch operations

```python
results = await hub.batch(
    [(light, "on", {"confirm": True}) for light in lights],
    max_concurrent=10,
)
print(f"Success rate: {results.success_rate:.1f}%")
for failure in results.get_failures():
    print(failure.device_id, failure.error)
```

From synchronous code, use `hub.batch_sync(...)`.

### Circuit breaker and rate limiting

```python
from fuseiot import CapabilityConfig, CircuitBreaker, RateLimiter

light = hub.add_device(
    HTTP("http://unstable-device/api"),
    Switchable,
    device_id="fragile",
    config=CapabilityConfig(
        circuit_breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=30),
        rate_limiter=RateLimiter(requests=10, window=1.0),
        retry_attempts=3,
        timeout=2.0,
    ),
)
# After 5 failed or unconfirmed commands the circuit opens: commands return
# status CIRCUIT_OPEN immediately for 30 s, then a trial call is allowed.
```

### RGB light control

```python
from fuseiot import RGBLight, Color

light = hub.add_device(HTTP("http://192.168.1.50/api"), RGBLight, device_id="color_bulb")

light.set_color(Color(255, 0, 128), brightness=80)
light.set_hex("#00ff88")
light.set_hsv(h=120, s=1.0, v=0.8)      # green; converted to RGB client-side
light.set_effect("rainbow", speed=50)
```

### Automation rules

```python
from fuseiot import RuleEngine, Condition, Action

rules = RuleEngine(hub)
rules.add_simple(
    "frost_protection",
    Condition("outdoor_temp", "value", "<", 2.0),
    then_actions=[Action("pipe_heater", "on")],
    else_actions=[Action("pipe_heater", "off")],
)
rules.start_background(interval=5.0)    # or: await rules.evaluate()
```

Rules are edge-triggered by default, so actions run when the outcome changes
rather than on every evaluation. They can also be declared under `rules:` in
the config file.

### Simulation and testing

```python
from fuseiot import HTTP, Dimmable, Hub, Motor, SimulatedHTTPDevice, VirtualDevice

hub = Hub()

lamp = hub.add_device(VirtualDevice("dimmable", latency_ms=20, settle_ms=150), Dimmable, device_id="lamp")

with SimulatedHTTPDevice("motor") as dev:           # a real HTTP server on 127.0.0.1
    motor = hub.add_device(HTTP(dev.url), Motor, device_id="motor")
```

### Custom protocols and capabilities

Subclass `Protocol` (implement `connect`, `disconnect` and `send`) or
`Capability` (implement `category` and `_fetch_state`, and declare
`commands`). Commands call `self._send_and_confirm(...)`, which gives them
confirmation, retries, circuit breaking, metrics and events for free. See
[examples/custom_capability.py](examples/custom_capability.py).

---

## Home automation with ESP32

Flash the [ESP32 firmware](firmware/esp32/README.md), then run
`fuseiot discover --yaml` to list your boards as config entries. Add schedules,
rules and notifications:

```yaml
location: {latitude: 12.97, longitude: 77.59, timezone: Asia/Kolkata}
schedules:
  - {name: porch_on, at: sunset-15m, then: {device: porch-light, command: "on"}}
  - {name: porch_off, at: "23:30", then: {device: porch-light, command: "off"}}
notifications:
  channels: {phone: {type: ntfy, topic: home-7f3k9q2xv1}}
  alerts: {device_offline: [phone], command_failed: [phone]}
users_db: ./data/users.db
```

```bash
fuseiot -c fuseiot.yaml user add admin --role admin
fuseiot -c fuseiot.yaml serve --host 0.0.0.0     # open it on your phone and "Add to Home Screen"
```

The [home automation guide](docs/home-automation.md) walks through every step,
including HTTPS so your phone can install the app.

---

## Deployment

```bash
export FUSEIOT_API_TOKEN=$(openssl rand -hex 32)
docker compose up -d                    # FuseIoT with ./config/fuseiot.yaml
docker compose --profile mqtt up -d     # + local Mosquitto broker
```

The image runs as non-root, has a healthcheck, logs JSON, and keeps its data
in a `/data` volume. The Compose file also makes the container filesystem
read-only and drops all Linux capabilities. A hardened systemd unit is also
included. The [deployment guide](docs/deployment.md) covers TLS, monitoring,
cloud bridging and backups.

---

## Testing

```bash
pip install -e ".[all,test]"
pytest                          # no hardware or network needed
pytest -m "not slow"            # skip running the example scripts
pytest --cov=fuseiot --cov-report=html
pytest tests/test_hub.py -v
```

---

## Architecture

```
┌──────────────────────────────────────────────┐
│ Application layer                            │
│ CLI · REST API + dashboard · rules · cloud   │
├──────────────────────────────────────────────┤
│ Hub (registry)                               │
│ devices · event bus · batch · metrics        │
├──────────────────────────────────────────────┤
│ Capabilities (what a device can do)          │
│ Switchable, Dimmable, Sensor, Motor, Lock,   │
│ RGBLight, Thermostat, EnergyMonitor, …       │
│   command → rate limit → breaker → send      │
│           → confirm (read back) → cache/event│
├──────────────────────────────────────────────┤
│ Protocols (how to reach it)                  │
│ HTTP(S), MQTT(S), WebSocket, Serial, Virtual │
├──────────────────────────────────────────────┤
│ State: cache · SQLite/Redis · history        │
└──────────────────────────────────────────────┘
```

---

## Documentation

- [Home automation guide](docs/home-automation.md): ESP32s, schedules, notifications, accounts, phone app
- [ESP32 firmware](firmware/esp32/README.md)
- [Device contract](docs/device-contract.md): what firmware must implement
- [REST API](docs/rest-api.md)
- [Deployment guide](docs/deployment.md)
- [Examples](examples/)
- [Changelog](CHANGELOG.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

---

## Contributing

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/amazing`).
3. Run `ruff check fuseiot examples` and `pytest`.
4. Commit your changes and open a pull request.

Please make sure all tests pass and code coverage doesn't decrease.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

## Support

- **Issues:** [github.com/fuseiot/fuseiot/issues](https://github.com/fuseiot/fuseiot/issues)
- **Discussions:** [github.com/fuseiot/fuseiot/discussions](https://github.com/fuseiot/fuseiot/discussions)
- **Email:** team@fuseiot.dev

---

*Built with ❤️ for reliable device automation.*
