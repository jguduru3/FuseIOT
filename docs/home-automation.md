# Home automation with ESP32 devices

This guide takes you from a bare ESP32 to a phone app that controls your home,
runs schedules and sends you alerts.

```
ESP32 boards ──Wi-Fi/HTTP──▶ FuseIoT server (Raspberry Pi / PC / Docker) ◀── phone app (PWA)
  relays, dimmers, sensors       schedules · rules · alerts · users            ntfy / Telegram push
```

## 1. Flash your ESP32s

The firmware in [`firmware/esp32`](../firmware/esp32/README.md) turns an ESP32
into a FuseIoT device. You pick one device type per board in `config.h`:

| Type | What it controls |
|---|---|
| `switchable` | Relay (lights, sockets, pumps), with an optional wall button |
| `dimmable` | PWM LED strip or driver |
| `rgb_light` | RGB LED strip, with rainbow and pulse effects |
| `sensor` | Temperature, humidity, analog probe |
| `lock` | Door lock or solenoid, with optional reed switch |
| `motor` | Servo, for blinds or a valve |

Copy `include/config.example.h` to `include/config.h` and set your Wi-Fi, a
device ID such as `porch-light`, the pins, and a token. Then flash it:
`pio run -t upload`. The board announces itself on your network as
`porch-light.local`.

> **Mains safety.** Relays switching 230 V/120 V must be in a proper enclosure
> with fusing. Use certified relay modules and keep low-voltage and mains
> wiring apart.

## 2. Find the devices and write the config

```bash
fuseiot discover --yaml > devices.yaml     # lists every FuseIoT ESP32 on the network
```

Paste the result into your `fuseiot.yaml`, then add the sections below. Start
from [`examples/demo.yaml`](../examples/demo.yaml), which already contains
every section, using simulated devices.

```yaml
location:                       # needed for sunrise/sunset
  latitude: 12.97
  longitude: 77.59
  timezone: Asia/Kolkata        # IANA name, or "local"

devices:
  - id: porch-light
    capability: switchable
    protocol: http
    endpoint: http://porch-light.local
    options: {bearer_token: "${PORCH_LIGHT_TOKEN}"}
```

Run `fuseiot -c fuseiot.yaml validate` after every change. It checks devices,
rules, schedules and notification channels without touching anything.

## 3. Schedules

```yaml
schedules:
  - name: porch_on
    at: sunset-15m                 # HH:MM, sunrise, sunset, with optional +/-1h30m offsets
    days: daily                    # daily | weekdays | weekends | [mon, wed, fri]
    then: {device: porch-light, command: "on"}

  - name: porch_off
    at: "23:30"
    then: {device: porch-light, command: "off"}

  - name: weekday_heating
    cron: "30 6 * * 1-5"           # minute hour day month weekday
    then:
      - {device: heating, command: set_temperature, params: {temperature: 21.5}}
      - {notify: phone, message: "Heating on, it's {hallway-temp.value} C inside"}
```

Run `fuseiot -c fuseiot.yaml schedules` to see the next run of each schedule
and today's sunrise and sunset.

Sunrise and sunset are calculated locally, with no internet needed, and are
accurate to about 2 minutes. If the server was off when a schedule was due,
that run is skipped rather than replayed late, so the lights never switch on at
3 am after a reboot.

## 4. Rules

Rules react to device states. They're checked every `rules_interval` seconds
(default 5), and by default act only when the outcome changes:

```yaml
rules:
  - name: frost_protection
    when: {device: outdoor-temp, property: value, op: "<", value: 2}
    then:
      - {device: pipe-heater, command: "on"}
      - {notify: phone, message: "Frost! Outside is {outdoor-temp.value} C", priority: high}
    else: {device: pipe-heater, command: "off"}
    cooldown: 600

  - name: door_left_unlocked
    when: {device: front-door, property: lock_state, op: "==", value: unlocked}
    then: {notify: phone, title: "Front door", message: "Unlocked at {time}"}
```

Operators are `==`, `!=`, `<`, `>`, `<=`, `>=`, `contains` and `changed`.
Message templates can use `{time}`, `{date}`, `{rule}`, `{schedule}` and any
device value as `{<device-id>.<key>}`.

## 5. Phone notifications

The easiest option is [ntfy](https://ntfy.sh): it's free, and you can host it
yourself. Install the ntfy app on Android or iOS, subscribe to a long random
topic name (anyone who knows the name can read it), and add:

```yaml
notifications:
  channels:
    phone:  {type: ntfy, topic: home-7f3k9q2xv1}
    family: {type: telegram, bot_token: "${TG_TOKEN}", chat_id: "123456789"}
    mail:   {type: email, host: smtp.gmail.com, port: 587, username: me@gmail.com,
             password: "${SMTP_APP_PASSWORD}", to: [me@gmail.com]}
    hooks:  {type: webhook, url: "https://example.com/hook"}
  alerts:                          # automatic alerts
    device_offline: [phone]        # an ESP32 stopped answering
    device_online: [phone]
    command_failed: [phone]        # a command could not be confirmed
  cooldown: 300                    # don't repeat the same alert within 5 minutes
```

Check a channel with `fuseiot -c fuseiot.yaml notify test phone`, or from the
app under Alerts → Send test.

The server polls every device every `monitor_interval` seconds (default 30).
That's how it notices offline devices and changes made at a wall switch.

## 6. User accounts

```bash
fuseiot -c fuseiot.yaml user add admin --role admin      # asks for a password
fuseiot -c fuseiot.yaml user add priya --role operator
fuseiot -c fuseiot.yaml user add guest --role viewer
```

This needs `users_db: ./data/users.db` in the config.

| Role | Can |
|---|---|
| viewer | See devices, states, schedules, rules |
| operator | Also switch devices and run schedules |
| admin | Also enable or disable rules and schedules, manage users, test notifications |

Admins can also manage users in the app (Account tab). Every user can create
personal API tokens there for scripts or other apps. Changing a password signs
out that user's other sessions, and you can't remove or demote the last admin.

## 7. Install the app on your phone

Run the server with `fuseiot -c fuseiot.yaml serve --host 0.0.0.0`, then open
it on your phone and sign in.

- **Android (Chrome):** menu → *Install app*.
- **iPhone (Safari):** Share → *Add to Home Screen*.

The app then opens full screen, like a native app.

Phones only install web apps served over **HTTPS**. These are the simplest
ways to get it:

1. **Tailscale** (recommended). It gives you HTTPS *and* secure access from
   outside your home, with no port forwarding. Install Tailscale on the server
   and your phone, then run `tailscale serve --bg 8080`. Open the
   `https://<server>.<tailnet>.ts.net` address it prints.
2. **Caddy with your own domain**, using a DNS challenge. See
   [deployment.md](deployment.md#3-security-checklist).

Over plain `http://` the app still works fully in the browser; it just won't
install.

## 8. Run it permanently

Use Docker (`docker compose up -d`) or the systemd unit. Both are covered in
[deployment.md](deployment.md). A Raspberry Pi 3 or newer is plenty for dozens
of devices.
