# Device contract

FuseIoT confirms every command by **reading the device back**. For that to
work, a device has to expose its state and accept commands in a predictable
shape. This page is what firmware needs to implement.

`fuseiot simulate <type>` runs a reference implementation over HTTP. Use it to
compare against your firmware.

## How a command flows

1. FuseIoT sends the command (for example `POST /on {"power": true}`).
2. It reads the state endpoint (for example `GET /state`) until the expected
   values appear or the timeout expires.
3. The result is `CONFIRMED` (the state matched), `TIMEOUT` (the device
   acknowledged but never reached the state), or `FAILED` (a transport error).

Command responses are informational; only the state read counts. A device that
answers `200 OK` but never actually switches is reported as a failure. That's
the point of the confirmation step.

Numbers are compared with a small tolerance: ±0.5 for motor position, ±0.05 °C
for thermostat targets, and ±0.001 for energy.

## HTTP / HTTPS

All paths are relative to the configured base URL (for example
`http://192.168.1.45/api`). Request and response bodies are JSON. `GET`
parameters, if any, are sent as a query string.

- **Reachability:** `GET <base_url>` may return anything below 500 except 401
  (404 is fine; 401 means the credentials are wrong).
- **Auth:** HTTP Basic (`auth`), bearer token (`bearer_token`), or mutual TLS
  (`client_cert`). `401` and `403` are reported as authentication errors.

| Capability | State read | Commands |
|---|---|---|
| `switchable` | `GET /state` → `{"power": bool}` (`"state": "on"` also accepted) | `POST /on`, `POST /off` |
| `dimmable` | `GET /state` → `{"power": bool, "brightness": 0-100}` | `POST /on {"brightness"?}`, `POST /off`, `POST /brightness {"brightness"}`, `POST /fade {"brightness","duration_ms"}` |
| `rgb_light` | `GET /state` → `{"power", "brightness", "r", "g", "b", "effect"?}` | `POST /on {"r","g","b","brightness"?}`, `POST /off`, `POST /color {"r","g","b","brightness"?}`, `POST /brightness`, `POST /effect {"effect","speed"}` |
| `sensor` | `GET /reading` → `{"value": float, "unit"?, "timestamp"?, "calibrated"?}` | `POST /calibrate {"reference"}` (then report `"calibrated": true`) |
| `motor` | `GET /status` → `{"position", "speed", "moving", "homed"}` | `POST /move {"position","speed"?}`, `POST /speed {"speed"}`, `POST /stop`, `POST /home` |
| `thermostat` | `GET /status` → `{"temperature", "target_temperature", "mode", "fan_running", "heating"?, "cooling"?}` | `POST /target {"temperature"}`, `POST /mode {"mode"}`, `POST /fan {"on": bool}` |
| `lock` | `GET /status` → `{"lock_state": "locked\|unlocked\|jammed", "battery_level"?, "door_open"?}` | `POST /lock {"action": "lock"\|"unlock", "user_code"?}` |
| `energy_monitor` | `GET /energy` → `{"power": W, "voltage": V, "current": A, "energy": kWh, "frequency"?, "power_factor"?}` | `POST /reset {"counter": "energy"}` |
| `smart_plug` | both `GET /state` and `GET /energy` | `POST /on`, `POST /off`, `POST /reset` |

`set_hsv` and `set_hex` are converted to RGB on the client, so devices only
need to support `/color`.

## MQTT / MQTTS

Each device has a base topic. The endpoint
`mqtt://broker:1883/home/plug1` gives the base topic `home/plug1`.

| What | Topic | Payload |
|---|---|---|
| State (publish whenever it changes, **retained**) | `<base>/state` (or `<base>/reading`, `<base>/status`, `<base>/energy`, matching the HTTP path) | the same JSON as the HTTP state response |
| Commands to the device | `<base>/set/<command>`, e.g. `home/plug1/set/on` | the same JSON body as the HTTP command |
| Optional state request | `<base>/<state path>/get` (empty), e.g. `home/plug1/state/get` or `home/hall/reading/get` | reply by publishing state |

FuseIoT subscribes to `<base>/#`, so retained state is available immediately
after connecting. If no state has been seen yet, it publishes to
`.../get` and waits for the device to reply.

## WebSocket

Each message is one JSON object. FuseIoT sends the HTTP-style payload plus an
`id`, for example
`{"_method": "GET", "_path": "/state", "id": "7"}`. Reply with the same JSON
you would return over HTTP, including the same `"id"`. Messages without an
`id` are matched to the oldest pending request. Messages that arrive with no
request pending are kept as the device's latest pushed state.

## Serial (RS-232 / RS-485 / USB-CDC)

The protocol is JSON lines. Each request is one line:

```
{"method": "GET", "path": "/state"}
{"method": "POST", "path": "/on", "power": true}
```

Each reply is one JSON line, using the same bodies as HTTP. The default line
settings are 9600 8N1, configurable through `options`. pyserial URLs such as
`socket://host:port` (for serial-over-TCP adapters) also work.

## Writing your own capability

If a device can't follow this contract, subclass `Capability`: implement
`category` and `_fetch_state()`, and declare `commands`. See
[examples/custom_capability.py](../examples/custom_capability.py).
