# REST API

Base URL: the address `fuseiot serve` listens on. All responses are JSON.

## Authentication and roles

When the server has user accounts (`users_db`) or a service token
(`api_token`), every `/api/*` request needs `Authorization: Bearer <token>`.
There are three kinds of token:

- a **session token** from `POST /api/auth/login`, which expires after
  `session_days` (default 30)
- a **personal API token**, from the app or `POST /api/tokens`, which lasts
  until revoked
- the **service token** from the config, which has admin rights

Only `/api/events`, which browsers open without custom headers, also accepts
`?token=<token>`. Other routes reject a token in the URL, so it can't leak into
proxy logs.

| Role | Allowed |
|---|---|
| viewer | Every `GET` below, plus own tokens and password |
| operator | Also device commands and `POST /api/schedules/{name}/run` |
| admin | Also enable/disable rules and schedules, `/api/users`, `/api/notifications` |

A request with too low a role gets **403**. A missing or invalid token gets **401**.

| Method | Path | Description |
|---|---|---|
| GET | `/api/auth/status` | Public: `{"auth_required", "users_enabled", "version"}` |
| POST | `/api/auth/login` | Public: `{"username", "password"}` returns `{"token", "expires_at", "user"}`; 401 wrong credentials; 429 after 5 failures in 5 min |
| GET | `/api/auth/me` | `{"username", "role", "token_kind"}` |
| POST | `/api/auth/logout` | Revokes the current session token |
| POST | `/api/me/password` | `{"current", "new"}`; signs out the user's other sessions |
| GET, POST | `/api/tokens` | List own API tokens; create with `{"name", "ttl_days"?}`. The token is returned once. |
| DELETE | `/api/tokens/{id}` | Revoke own token |
| GET, POST | `/api/users` | (admin) list; create with `{"username", "password", "role"}` |
| POST | `/api/users/{name}` | (admin) `{"role"?, "disabled"?, "password"?}`; 400 if it would remove the last admin |
| DELETE | `/api/users/{name}` | (admin) delete the user |

## Public endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Web dashboard |
| GET | `/health` | `{"status": "healthy\|degraded\|unhealthy", "devices", "devices_connected", "disconnected": [...], "uptime_seconds", "version"}`. Returns 503 when unhealthy. |
| GET | `/ready` | `{"ready": true}` |
| GET | `/metrics` | Prometheus text format |

## Devices

### `GET /api/devices`

Lists every device:

```json
{"devices": [{
  "device_id": "desk_lamp", "name": "Desk lamp", "category": "dimmable",
  "protocol": "http", "endpoint": "http://192.168.1.40", "connected": true,
  "commands": ["on", "off", "toggle", "set_brightness", "fade_to"],
  "state": {"power": true, "brightness": 40}, "state_error": null,
  "metrics": {"commands_total": 12, "commands_success": 12, "avg_latency_ms": 85.1},
  "circuit": {"state": "CLOSED", "failure_count": 0}
}]}
```

`state` is the last known state, read without contacting the device.

### `GET /api/devices/{id}`

Returns the same object as one entry of `/api/devices`. Add `?fresh=1` to poll
the device now. If the device can't be read, the response is still 200: `state`
holds the last known state and `state_error` explains the failure.

### `GET /api/devices/{id}/state`

Returns `{"device_id", "state"}`. Add `?fresh=1` to poll the device now. If the
device can't be read, the response is 502 with `{"error": "..."}`.

### `GET /api/devices/{id}/history?limit=100`

Returns state changes, newest first. Requires `history_enabled: true`.

### `POST /api/devices/{id}/commands/{command}`

The JSON body holds the command's parameters, for example:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -d '{"brightness": 70}' http://localhost:8080/api/devices/desk_lamp/commands/on
```

The response is the full command result:

```json
{"success": true, "confirmed": true, "status": "CONFIRMED", "device_id": "desk_lamp",
 "command": "on", "state_before": {"power": false, "brightness": 0},
 "state_after": {"power": true, "brightness": 70}, "latency_ms": 84.2, "error": null, ...}
```

| HTTP status | Meaning |
|---|---|
| 200 | `CONFIRMED`, or `ACKNOWLEDGED` when `{"confirm": false}` |
| 400 | Unknown command or invalid parameters (`allowed` lists the valid commands) |
| 401 | Missing or invalid token |
| 404 | Unknown device |
| 429 | `RATE_LIMITED` |
| 502 | `FAILED` (transport error) |
| 503 | `CIRCUIT_OPEN` (the device was disabled after repeated failures) |
| 504 | `TIMEOUT` (the device acknowledged but its state never matched) |

Common parameters for every command: `confirm` (bool) and `timeout` (seconds).

| Capability | Commands and parameters |
|---|---|
| switchable | `on`, `off`, `toggle` |
| dimmable | `on {brightness?}`, `off`, `toggle`, `set_brightness {brightness}`, `fade_to {brightness, duration_ms?}` |
| rgb_light | `on {color?: [r,g,b], brightness?}`, `off`, `toggle`, `set_color {color: [r,g,b], brightness?}`, `set_hex {hex_color}`, `set_hsv {h, s, v}`, `set_brightness`, `set_effect {effect, speed?}` |
| sensor | `calibrate {reference_value}` |
| motor | `move_to {position, speed?}`, `set_speed {speed}`, `stop`, `home` |
| thermostat | `set_temperature {temperature}`, `set_mode {mode}`, `fan_on`, `fan_off` |
| lock | `lock`, `unlock {user_code?}` |
| energy_monitor | `reset_counter` |
| smart_plug | `on`, `off`, `toggle`, `reset_counter` |

## Other endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/stats` | Hub statistics (cache, events, command counters) |
| GET | `/api/rules` | Rules with `enabled`, `last_outcome`, `trigger_count` (404 when no rules are configured or the server runs with `--no-rules`) |
| POST | `/api/rules/{name}/enable` and `/api/rules/{name}/disable` | Turn a rule on or off at runtime |
| GET | `/api/events` | Server-Sent Events: `event: state` (a `StateChangeEvent`), and `event: status` (`{"device_id", "status": {"online", "last_seen", …}}`) when the device monitor sees a device go offline or come back |
| GET | `/api/schedules` | `{"schedules": [{name, at/cron, days, enabled, next_run, last_run, last_ok, run_count}], "location", "sun": {sunrise, sunset}}` |
| POST | `/api/schedules/{name}/run` | (operator) run now, returns `{"ok"}` |
| POST | `/api/schedules/{name}/enable` and `/disable` | (admin) |
| GET | `/api/notifications` | (admin) channels, alert routing, the last 20 notifications |
| POST | `/api/notifications/test` | (admin) `{"channel"}`: 200 sent, 400 unknown channel, 502 delivery failed |

Device objects from `/api/devices` also include `"status"`, the monitor's
`{online, failures, last_seen, last_error}`, and `/health` lists
`offline_devices`.
