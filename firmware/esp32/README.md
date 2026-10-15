# FuseIoT firmware for ESP32

Arduino firmware that makes a DIY ESP32 board a FuseIoT device. It implements
the [FuseIoT device contract](../../docs/device-contract.md) over HTTP, so
FuseIoT can send commands and confirm them by reading the device back.

- One firmware, six device types, chosen at compile time: `switchable`,
  `dimmable`, `rgb_light`, `sensor`, `lock`, `motor`.
- Boards: ESP32 (`esp32dev`), ESP32-C3 (`esp32-c3-devkitm-1`), ESP32-S3
  (`esp32-s3-devkitc-1`). Other ESP32 boards work if you pick valid pins.
- Optional bearer-token auth, mDNS discovery (`fuseiot discover --yaml`),
  ArduinoOTA, NTP timestamps, last state restored after a power cut.
- The device never reports a state it isn't in: physical button presses, fades,
  servo sweeps and sensor failures all show up in the state FuseIoT reads.
- The only third-party library is ArduinoJson 7.

## 1. Wiring

Default pins are below. Change any of them in `config.h`.

| Capability | Signal | ESP32 (esp32dev) | ESP32-C3 | ESP32-S3 | Notes |
|---|---|---|---|---|---|
| switchable | Relay IN | GPIO26 | GPIO4 | GPIO4 | `FUSEIOT_RELAY_ACTIVE_LOW 1` for "low-level trigger" relay boards |
| | Push button | GPIO0 (BOOT) | GPIO9 (BOOT) | GPIO0 (BOOT) | Button to GND, internal pull-up. `-1` disables it |
| | Status LED | GPIO2 (on-board) | none | none | Follows the relay |
| dimmable | PWM out | GPIO25 | GPIO5 | GPIO5 | Gate of a logic-level N-MOSFET, or the PWM input of an LED driver |
| rgb_light | R / G / B | GPIO25 / 26 / 27 | GPIO5 / 6 / 7 | GPIO5 / 6 / 7 | One MOSFET per channel. `FUSEIOT_RGB_COMMON_ANODE 1` for a common-anode LED |
| sensor (ADC) | Analog in | GPIO34 | GPIO1 | GPIO1 | Must be an ADC1 pin. ADC2 doesn't work while WiFi is on. Max 3.1 V |
| sensor (DHT22) | Data | GPIO4 | GPIO10 | GPIO10 | 4.7 k to 10 k pull-up to 3.3 V |
| lock | Lock relay/MOSFET | GPIO26 | GPIO4 | GPIO4 | Use a flyback diode across a solenoid |
| | Reed switch (door) | GPIO27 | GPIO5 | GPIO5 | Reed to GND, internal pull-up. `-1` disables it |
| | Bolt sense (optional) | off | off | off | Enables `"jammed"` detection |
| | Battery ADC (optional) | off | off | off | Through a divider (`FUSEIOT_BATTERY_DIVIDER`) |
| motor | Servo signal | GPIO18 | GPIO6 | GPIO6 | Power the servo from 5 V, not the 3.3 V pin. Connect the grounds |

Avoid these pins on the classic ESP32: GPIO6-11 (flash), GPIO34-39 as outputs
(they are input-only), and holding GPIO0, 2, 12 or 15 at the wrong level at
boot (strapping pins).

## 2. Configure

```sh
cd firmware/esp32
cp include/config.example.h include/config.h
```

Edit `include/config.h`. At minimum, set these:

```c
#define FUSEIOT_CAPABILITY FUSEIOT_CAP_DIMMABLE   // what this board is
#define FUSEIOT_WIFI_SSID "your-wifi"
#define FUSEIOT_WIFI_PASSWORD "your-wifi-password"
#define FUSEIOT_DEVICE_ID "desk-lamp"            // "" = fuseiot-<cap>-<mac>
#define FUSEIOT_TOKEN "a-long-random-secret"     // "" = no auth (LAN only!)
#define FUSEIOT_OTA_PASSWORD "another-secret"
```

Every other option is listed, with its default, in `config.example.h`: pins,
relay polarity, PWM frequency and gamma, ADC scaling, DHT22, lock PIN code,
auto-relock, servo pulse range, and so on. `config.h` is git-ignored.

To make a token, run `python -c "import secrets; print(secrets.token_urlsafe(24))"`.

## 3. Build and flash

### PlatformIO (recommended)

```sh
pip install platformio
cd firmware/esp32
pio run -e esp32dev -t upload          # or -e esp32-c3-devkitm-1 / -e esp32-s3-devkitc-1
pio device monitor -e esp32dev         # 115200 baud
```

PlatformIO downloads the toolchain and ArduinoJson. Running plain `pio run`
(with no `-e`) builds the `ci-*` environments instead. They compile every
capability with `include/config.ci.h`, so you can check the code without a
`config.h`.

On boot, the serial log shows the device id, IP address and mDNS name:

```
[fuseiot] firmware 1.0.0, capability dimmable
[net] connected, ip 192.168.1.50, rssi -58 dBm
[mdns] http://desk-lamp.local:80  (_fuseiot._tcp)
[http] listening on port 80 (auth on)
```

### Arduino IDE (2.x)

1. Install the ESP32 boards. In **Boards Manager**, install "esp32 by Espressif
   Systems". Core 2.0.x and 3.x both work.
2. In **Library Manager**, install **ArduinoJson** by Benoit Blanchon, version 7.x.
   No other libraries are needed.
3. Create a sketch folder called `fuseiot_esp32`. Put an empty
   `fuseiot_esp32.ino` in it, then copy in every file from `src/` plus your
   `include/config.h`. They all go in the same folder.
4. Select your board and port, then click Upload.

## 4. Over-the-air updates

ArduinoOTA starts once WiFi is up. It uses port 3232 and the password
`FUSEIOT_OTA_PASSWORD`.

```sh
# PlatformIO: the esp32dev-ota env uses espota; set --auth in platformio.ini to your OTA password
pio run -e esp32dev-ota -t upload --upload-port desk-lamp.local
```

In the Arduino IDE, the board appears as a network port named after the
device id.

## 5. Add it to FuseIoT

### Discover automatically

Each device advertises `_fuseiot._tcp` with TXT records `id`, `cap`, `fw` and
`auth`. To print ready-made config:

```sh
fuseiot discover --yaml
```

### Or write the entry yourself

```yaml
devices:
  - id: desk_lamp
    name: Desk lamp
    capability: dimmable
    protocol: http
    endpoint: http://desk-lamp.local        # http://<FUSEIOT_DEVICE_ID>.local, or the IP
    options:
      bearer_token: ${DESK_LAMP_TOKEN}      # the FUSEIOT_TOKEN value; omit if auth is off
      timeout: 5

  - id: blinds
    capability: motor
    protocol: http
    endpoint: http://fuseiot-motor-a1b2c3.local
    options: {bearer_token: ${BLINDS_TOKEN}}
    capability_options: {position_range: [0, 180]}   # match FUSEIOT_MOTOR_POS_MIN/MAX
```

The same device from Python:

```python
from fuseiot import Hub, HTTP, Dimmable

hub = Hub()
lamp = hub.add_device(HTTP("http://desk-lamp.local", bearer_token="..."), Dimmable, device_id="desk_lamp")
print(lamp.fade_to(30, duration_ms=1500))   # CONFIRMED once the fade has finished
```

mDNS `.local` names need Bonjour or Avahi on the machine running FuseIoT.
Windows 10 and later resolve them natively. If they don't resolve, use the IP
address shown in the serial log, ideally with a DHCP reservation.

## 6. HTTP API

`<state>` is `/state`, `/reading` or `/status`, depending on the capability.
Every response is JSON.

| Endpoint | Auth | Response |
|---|---|---|
| `GET /` | never | `{"device_type", "id", "fw", "status": "ok"}`. This is FuseIoT's reachability probe |
| `GET /info` | yes | `{"id", "cap", "fw", "uptime_s", "rssi", "free_heap", "ip", "time_synced", ...}` |
| capability endpoints | yes | see below |

| Capability | State read | Commands |
|---|---|---|
| switchable | `GET /state` → `{"power"}` | `POST /on`, `POST /off` |
| dimmable | `GET /state` → `{"power","brightness"}` | `POST /on {"brightness"?}`, `/off`, `/brightness {"brightness"}`, `/fade {"brightness","duration_ms"}` |
| rgb_light | `GET /state` → `{"power","brightness","r","g","b","color_mode","effect","speed"}` | `POST /on {"r","g","b","brightness"?}`, `/off`, `/color {"r","g","b","brightness"?}`, `/brightness`, `/effect {"effect","speed"?}` (`none`, `rainbow`, `pulse`) |
| sensor | `GET /reading` → `{"value","unit","timestamp"?,"calibrated"}` | `POST /calibrate {"reference"}` (`{"reset": true}` clears it) |
| lock | `GET /status` → `{"lock_state","battery_level","door_open"}` | `POST /lock {"action": "lock"\|"unlock", "user_code"?}` |
| motor | `GET /status` → `{"position","speed","moving","homed"}` | `POST /move {"position","speed"?}`, `/speed {"speed"}`, `/stop`, `/home` |

Commands answer `200 {"status": "accepted"}`. FuseIoT then confirms by
reading the state. Errors always have an `"error"` key:

| Status | When |
|---|---|
| 400 | Malformed JSON, a missing or non-numeric field, unknown effect, servo position out of range |
| 401 | Token missing or wrong (constant-time comparison) |
| 403 | Lock: wrong `user_code` |
| 404 | Unknown path |
| 405 | Wrong method, for example `POST /state` |
| 409 | Lock: refused to lock while the door is open (only with `FUSEIOT_LOCK_REFUSE_WHEN_OPEN 1`) |
| 429 | Lock: too many wrong codes. It is locked out for `FUSEIOT_LOCK_LOCKOUT_S` |
| 503 | Sensor: no valid reading (wiring, timeout, checksum, out of range). Includes a `"detail"` |

How state is reported:

- **dimmable**: `brightness` is the level the output is at right now. During a
  `/fade` it moves towards the target, so FuseIoT confirms the fade once it
  completes. `fade_to()` already allows `duration_ms` on top of its timeout.
- **rgb_light**: `r`, `g` and `b` are the base colour. While an effect runs,
  `color_mode` is `"effect"`. Setting a colour stops the effect.
- **motor**: `position` is the angle being commanded right now, and `moving`
  stays true until the target is reached. A hobby servo has no feedback, so a
  stalled servo can't be detected. At speed 100 it travels
  `FUSEIOT_MOTOR_MAX_DEG_PER_S` (default 360 °/s). Long, slow moves need a
  larger FuseIoT `timeout`.
- **sensor**: `timestamp` is the time the sample was taken. It is left out until
  NTP has synced. Calibration is stored in flash as an offset:
  `value = raw + (reference - raw at calibration)`.
- **lock**: the lock always boots locked. Without a bolt sense switch,
  `lock_state` is the relay state. With one, it is what the switch reports, and
  `"jammed"` when the bolt hasn't reached the commanded position within
  `FUSEIOT_LOCK_JAM_TIMEOUT_MS`. Auto-relock waits for the door to close.
- **switchable / dimmable / rgb_light**: the last state is saved to NVS 2 s
  after it stops changing, to limit flash wear. It is restored at boot when
  `FUSEIOT_RESTORE_STATE` is 1.

## 7. Check a device against the contract

`test_contract.py` drives a device the way FuseIoT does: it probes `GET /`,
reads the state, sends each command and confirms it. It prints PASS or FAIL
for each check.

```sh
pip install fuseiot            # or: PYTHONPATH=<repo root>
python test_contract.py http://desk-lamp.local --token "$TOKEN"
python test_contract.py http://front-door.local --token "$TOKEN" --allow-unlock --user-code 1234
python test_contract.py http://hall-temp.local --allow-calibrate
python test_contract.py --self-test     # validates the script itself against fuseiot's simulator
```

The script moves real hardware: it switches relays and drives servos.
Unlocking a lock and calibrating a sensor only run when you pass
`--allow-unlock` or `--allow-calibrate`. Checks marked `[fw]` cover this
firmware's extras: `/info` and the 400, 404 and 405 errors. Pass
`--contract-only` to skip them when testing a different device.

## 8. Safety

- **Mains voltage can kill.** Relay modules that switch 110/230 V belong in an
  enclosure with proper strain relief, clearance and a fuse. If you aren't
  qualified to do mains wiring, get an electrician or use a certified smart
  plug. Never touch the board while it is connected to mains, and that includes
  plugging in USB. Use a USB isolator, or power the mains side off, while
  flashing.
- Relay contacts have current ratings. Motors, compressors and LED drivers
  draw large inrush currents, so derate the relay or use a contactor.
- **Locks.** Put a flyback diode across solenoids. Most cheap solenoids are not
  rated for continuous duty, so set `FUSEIOT_LOCK_AUTO_RELOCK_S` for strikes.
  Set `FUSEIOT_TOKEN` and `FUSEIOT_LOCK_PIN_CODE`. The HTTP API is plain HTTP,
  so keep the device on a trusted network or VLAN, and keep a mechanical
  override. The firmware boots locked. On power loss, a fail-secure strike stays
  locked and a fail-safe one opens. Choose with `FUSEIOT_LOCK_ENERGIZE_TO_UNLOCK`.
- **Servos** can pinch fingers and draw stall current. Power them from a
  separate 5 V supply and share ground with the ESP32.
- The bearer token travels in clear text over HTTP. It keeps out casual LAN
  clients, not an attacker who can sniff your WiFi.

## 9. Limitations

- Plain HTTP only. The ESP32 can do TLS, but certificate management on DIY
  boards is out of scope here.
- The HTTP server handles one request at a time, which is fine for FuseIoT's
  request-then-poll pattern.
- The DHT22 reader runs about 5 ms with interrupts disabled. That is normal for
  DHT sensors and doesn't disturb WiFi.
- The ADC on the classic ESP32 is noticeably non-linear near 0 V and above about
  2.5 V. Calibrate against a reference thermometer, or use `/calibrate`.
