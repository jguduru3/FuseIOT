# Deploying FuseIoT

`fuseiot serve` runs everything a site needs in one process:

- REST API
- web dashboard
- `/health`, `/ready` and `/metrics` endpoints
- automation rules
- optional cloud bridge

## 1. Write the config

Start from [`examples/config.yaml`](../examples/config.yaml). It covers every
protocol and option. Check it without touching any devices:

```bash
fuseiot -c fuseiot.yaml validate
```

Keep secrets out of the file by referencing environment variables:
`api_token: ${FUSEIOT_API_TOKEN}`, `password: ${MQTT_PASSWORD}`. Any top-level
key can also be overridden with `FUSEIOT_<KEY>`, for example
`FUSEIOT_LOG_LEVEL=DEBUG`.

## 2. Choose how to run it

### Docker / Compose

```bash
export FUSEIOT_API_TOKEN=$(openssl rand -hex 32)
docker compose up -d                 # uses ./config/fuseiot.yaml
docker compose --profile mqtt up -d  # also starts a Mosquitto broker
```

The image:

- runs as a non-root user (uid 10001); the Compose file also makes the root
  filesystem read-only and drops all capabilities
- stores the last known state (`fuseiot.db`) and history (`history.db`) in the
  `/data` volume
- logs JSON to stdout
- has a `HEALTHCHECK` on `/ready`

To use serial devices, pass them through with `devices:` and `group_add: [dialout]`
(see the comments in `docker-compose.yml`).

### systemd (Raspberry Pi, Linux gateway)

[`deploy/systemd/fuseiot.service`](../deploy/systemd/fuseiot.service) contains
the install steps in its header. It validates the config before every start and
restarts on failure. It also runs with systemd hardening enabled.

### Plain process

```bash
pip install "fuseiot[all]"
FUSEIOT_API_TOKEN=... fuseiot -c fuseiot.yaml serve --host 0.0.0.0 --port 8080
```

## 3. Security checklist

- **Require sign-in.** Create user accounts
  (`fuseiot user add admin --role admin`, needs `users_db`) and/or set an API
  service token. The server refuses to listen on a non-loopback address with
  neither, unless you pass `--insecure`. Give household members the
  `operator` role and guests `viewer`. Repeated failed logins are throttled.
- **Put TLS in front.** The built-in server speaks plain HTTP. Use a reverse
  proxy such as Caddy, nginx or Traefik for HTTPS:

  ```
  # Caddyfile
  iot.example.com {
      reverse_proxy 127.0.0.1:8080
  }
  ```

  If you proxy the event stream through nginx, disable buffering for
  `/api/events`.
- Only the commands a capability declares can be invoked remotely, from the
  API, rules or cloud. Parameters are validated, and request bodies are capped
  at 64 KB.
- Never set `verify_ssl: false` on HTTPS devices in production. Use
  `ca_bundle` for private CAs instead.
- Use a network that isolates IoT devices from the rest of your LAN where you
  can.

## 4. Monitoring

| Endpoint | Purpose |
|---|---|
| `GET /health` | `healthy` (all devices connected), `degraded` (some connected), or `unhealthy` (none connected; HTTP 503) |
| `GET /ready` | 200 once the server is up; use it for liveness and readiness probes |
| `GET /metrics` | Prometheus: `fuseiot_commands_total{device,command,status}`, `fuseiot_command_latency_ms` (summary), `fuseiot_device_up{device}`, `fuseiot_devices_connected`, `fuseiot_http_requests_total` |

An alert worth setting up:
`increase(fuseiot_commands_total{status!="confirmed"}[15m]) > 0`.
It fires when commands are being sent but not confirmed.

Set `log_json: true` (the default in the container) to emit one JSON object
per line.

## 5. Cloud (optional)

```yaml
cloud_enabled: true
cloud_provider: mqtt        # mqtt | aws | azure
cloud_options:
  broker: broker.hivemq.cloud
  port: 8883
  username: ${CLOUD_USER}
  password: ${CLOUD_PASSWORD}
  client_id: site-1
```

State is published retained to `fuseiot/<client_id>/state/<device_id>`.
Commands arrive on `fuseiot/<client_id>/commands` as
`{"device_id", "command", "params"}`, and results go to `.../results`.

For AWS IoT Core, use `cloud_provider: aws` with `endpoint`, `client_id`,
`cert_path`, `key_path` and `ca_path`. Azure IoT Hub needs `connection_string`
and the `fuseiot[cloud-azure]` extra. Google Cloud IoT Core was retired in
2023; use the MQTT bridge instead.

## 6. Backups and upgrades

- Back up the `/data` volume, which holds `fuseiot.db` (last state) and
  `history.db`.
- Upgrade the Compose deployment (which builds the image locally) with
  `git pull && docker compose up -d --build`. If you switch `image:` to the
  published `ghcr.io/<owner>/fuseiot:<version>`, use
  `docker compose pull && docker compose up -d` instead. For systemd, run
  `pip install -U fuseiot`, then `systemctl restart fuseiot`. Check
  [CHANGELOG.md](../CHANGELOG.md) first.
