# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.1.0] - 2026-10-15

Home automation for DIY ESP32 devices.

### Added

- **ESP32 firmware** (`firmware/esp32`, PlatformIO/Arduino) implementing the
  device contract for relays, dimmers, RGB strips, sensors, locks and servos.
  It supports mDNS discovery, OTA updates, token auth and a wall button.
  `fuseiot discover --yaml` turns the boards it finds into config entries.
- **Schedules:** fixed times, sunrise/sunset with offsets, weekday filters and
  cron (`schedules:` in the config, `fuseiot schedules`,
  `/api/schedules`). Sun times are calculated locally. Runs missed while the
  server was down are skipped rather than replayed late.
- **Notifications:** ntfy, Telegram, webhook, email and log channels. Rules and
  schedules can use `notify` actions with templated messages
  (`{device.value}`). There are automatic alerts for device offline/online,
  command failures and server start, with a cooldown, plus
  `fuseiot notify test`.
- **Device monitor:** polls devices every `monitor_interval` seconds, so
  wall-switch changes reach the app and rules, and offline devices are
  detected. Their status is shown in the app, `/health` and SSE.
- **User accounts:** viewer, operator and admin roles; scrypt password hashes;
  expiring sessions; personal API tokens; login throttling; and protection
  against removing the last admin. Managed with `fuseiot user ...` and in the
  app.
- **Installable phone app (PWA):** login, device switches, automations, alerts
  and accounts, with a manifest, service worker and icons.
- `fuseiot.runtime.Runtime` assembles devices, rules, schedules, monitor,
  notifications, users and cloud from a config. `validate_config()` checks
  everything offline.
- `tzdata` dependency, so time zones work on Windows and slim containers.

## [2.0.0] - 2026-10-15

Version 1.0.0 couldn't execute a confirmed command. This release fixes the core
command pipeline and adds everything needed to run FuseIoT as a service.

### Fixed

- **Commands crashed.** `CommandResult` was missing `device_id`, `command` and
  related fields, so every confirmed command raised `TypeError`.
- **Confirmation polled the wrong thing.** It sent `{"read_state": true}` to
  the protocol (a `POST /` over HTTP), so it practically never matched a real
  device. It now reads the device through the capability's own state endpoint
  and compares numbers with a tolerance.
- **`confirm=` was ignored.** `on(confirm=False)` still confirmed, and `off()`
  ignored the flag entirely.
- **`device_id` wasn't used.** The ID passed to `Hub.add_device` never reached
  the capability, so events, cache entries and `hub.on_state_change(device_id=…)`
  used an internal random ID instead.
- **The circuit breaker never opened**, because failures were returned as
  results rather than raised. Failed and unconfirmed commands now count as
  failures, and an open breaker returns `CIRCUIT_OPEN`.
- **The rate limiter crashed.** It referenced attributes that don't exist.
- **HTTP connect failed on most devices**, because it required a successful
  `HEAD` request. Any response below 500 except 401 now counts as reachable. The `timeout`
  argument was also silently ignored and now applies. HTTPS `ca_bundle` is now
  used, and 401/403 raise `AuthenticationError` on both the sync and async
  paths.
- **MQTT couldn't read device state.** Requests now follow a documented topic
  convention with retained state. Both paho-mqtt 1.x and 2.x are supported.
- **MQTTS crashed whenever TLS was configured.**
- **WebSocket was unreliable.** Sync use broke under a running event loop, and
  replies weren't matched to requests. It now runs on a dedicated loop and
  matches replies by `id`.
- **Serial ignored its timeout** and wasn't thread-safe.
- **The simulator's device state overwrote its connection state.**
  `VirtualDevice` now supports every capability, plus latency, failures and
  slow actuators.
- **Restored cache state had meaningless ages after a restart**, because
  persisted timestamps were process-relative. SQLite storage no longer needs
  SQLite 3.38+.
- **Two modules couldn't be imported:** `fuseiot.state.history` imported a
  name that doesn't exist, and `fuseiot.dashboard` imported cloud modules from
  the wrong package.
- `Hub.batch(continue_on_error=False)` leaked un-awaited tasks.
- `Hub(Config)` ignored the README's keys: `circuit_breaker_enabled`,
  `rate_limit_enabled`, `log_level`, `persistence_backend` and so on.
- `Hub.remove()` cleared the wrong cache key.
- The CLI had no way to register devices, so `on`, `off` and `status` always
  failed.
- The library printed DEBUG logs to stdout on import. It now stays quiet
  (warnings only) until `configure_logging()` is called.

### Added

- **`fuseiot serve`**, a production server built on the standard library:
  - REST API, web dashboard, and a live event stream (SSE)
  - `/health`, `/ready` and Prometheus `/metrics`
  - bearer-token auth, refusing to listen publicly without a token
  - a command allowlist and bounded request bodies
- **Declarative config** with `devices:` and `rules:` sections,
  `${ENV:-default}` expansion, and `FUSEIOT_*` overrides. Adds
  `fuseiot validate` and `fuseiot.registry.load_devices()`.
- **CLI:** `devices`, `status`, `on`, `off`, `toggle`, `cmd` (any command),
  `stats`, `validate`, `serve` and `simulate`, plus `--json` output and
  meaningful exit codes.
- **Automatic `*_async` methods** for every command (`plug.on_async()`), as
  shown in the README.
- **`fuseiot simulate` and `SimulatedHTTPDevice`**, a reference HTTP device
  that implements the device contract.
- **Rules engine:** config-file rules, edge- and level-triggered modes,
  cooldowns, enabling and disabling at runtime, and background execution.
- **State history** in SQLite, exposed at `/api/devices/{id}/history`.
- **Cloud:**
  - `MQTTBridge` for any MQTT broker
  - a real AWS IoT Core connector (MQTT over mutual TLS)
  - Azure IoT Hub through the official SDK
  - validated, allowlisted remote commands
- **New commands:** `RGBLight.set_hex()` and `toggle()`; `Color.from_hsv()`
  with validation; `CommandResult.raise_for_error()` now returns the result
  so it can be chained.
- **Deployment:** Dockerfile (non-root, healthcheck), docker-compose with an
  optional Mosquitto broker, a hardened systemd unit, and GitHub Actions for
  CI (lint, test matrix, wheel and Docker smoke tests) and releases (PyPI
  trusted publishing, multi-arch images on GHCR).
- **Docs:** device contract, deployment guide and REST API reference.

### Changed (breaking)

- Capabilities implement `_fetch_state()`. `read_state(fresh=False)` now raises
  `StateError` when a device can't be read, instead of returning made-up
  defaults (for example, a sensor value of `0.0`).
- `Hub.add_device(..., connect=True)` fails fast. Pass `connect=False` to
  connect lazily.
- Python 3.9+ is required, as 3.8 has reached end of life. pydantic v2 is
  required.
- `GCPIoT` raises `CloudError`, because Google Cloud IoT Core was retired on
  2023-08-16. Use `MQTTBridge` instead.
- `requests` is now a core dependency; `aiohttp` is optional
  (`fuseiot[async]`).

## [1.0.0]

- Initial release.
