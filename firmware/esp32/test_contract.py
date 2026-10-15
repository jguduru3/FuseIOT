#!/usr/bin/env python3
"""
FuseIoT device-contract check for HTTP devices (e.g. this ESP32 firmware).

Exercises a device exactly the way FuseIoT does - GET the base URL, read the
state endpoint, send each command and confirm it by polling the state - and
prints PASS/FAIL per check.

    python test_contract.py http://fuseiot-switchable-a1b2c3.local --token SECRET
    python test_contract.py http://192.168.1.50 --capability lock --allow-unlock --user-code 1234
    python test_contract.py --self-test        # check this script against FuseIoT's simulator

Needs the ``fuseiot`` package (``pip install fuseiot`` or run from the repo
root with ``PYTHONPATH=.``).

Safety: commands drive real hardware. Relays switch, servos move. Unlocking a
lock and changing a sensor calibration only happen with --allow-unlock /
--allow-calibrate.

Checks marked [fw] cover behaviour of the FuseIoT ESP32 firmware beyond the
minimal contract (404/400/405 errors, /info, id/fw in GET /). Skip them with
--contract-only when testing other devices.
"""
import argparse
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import requests

    from fuseiot import (
        Dimmable,
        Hub,
        Lock,
        Motor,
        RGBLight,
        Sensor,
        Switchable,
    )
    from fuseiot.capabilities.base import CapabilityConfig
    from fuseiot.protocols.http import HTTP
    from fuseiot.types import Color
except ImportError as exc:  # pragma: no cover - environment problem
    sys.exit(f"test_contract.py needs fuseiot and requests: {exc}")

CAPABILITIES: Dict[str, Tuple[Any, str]] = {
    "switchable": (Switchable, "/state"),
    "dimmable": (Dimmable, "/state"),
    "rgb_light": (RGBLight, "/state"),
    "sensor": (Sensor, "/reading"),
    "lock": (Lock, "/status"),
    "motor": (Motor, "/status"),
}

# A command path per capability that is safe to send a malformed body to.
BAD_JSON_PATH = {
    "switchable": "/on", "dimmable": "/brightness", "rgb_light": "/brightness",
    "sensor": "/calibrate", "lock": "/lock", "motor": "/speed",
}


class Report:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        print(f"\n== {title}")

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        return ok

    def skip(self, name: str, reason: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {name}  ({reason})")

    def result(self, name: str, result: Any) -> bool:
        detail = f"{result.status.name}, {result.latency_ms:.0f} ms"
        if result.error:
            detail += f", {result.error}"
        return self.check(name, bool(result.confirmed), detail)

    def summary(self) -> str:
        return f"{self.title}: {self.passed} passed, {self.failed} failed, {self.skipped} skipped"


class Raw:
    """Direct HTTP access for checks FuseIoT itself does not make."""

    def __init__(self, base_url: str, token: Optional[str], timeout: float) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, *, token: Any = "default", json: Any = None,
                data: Optional[str] = None) -> Tuple[int, Any]:
        headers = {"Accept": "application/json"}
        tok = self.token if token == "default" else token
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        resp = requests.request(method, self.base + path, headers=headers, json=json, data=data,
                                timeout=self.timeout)
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


STATE_SHAPES: Dict[str, Dict[str, Callable[[Any], bool]]] = {
    "switchable": {"power": lambda v: isinstance(v, bool)},
    "dimmable": {"power": lambda v: isinstance(v, bool),
                 "brightness": lambda v: _is_num(v) and 0 <= v <= 100},
    "rgb_light": {"power": lambda v: isinstance(v, bool),
                  "brightness": lambda v: _is_num(v) and 0 <= v <= 100,
                  "r": lambda v: _is_num(v) and 0 <= v <= 255,
                  "g": lambda v: _is_num(v) and 0 <= v <= 255,
                  "b": lambda v: _is_num(v) and 0 <= v <= 255,
                  "color_mode": lambda v: isinstance(v, str),
                  "effect": lambda v: isinstance(v, str)},
    "sensor": {"value": _is_num, "unit": lambda v: isinstance(v, str)},
    "lock": {"lock_state": lambda v: v in ("locked", "unlocked", "jammed"),
             "battery_level": lambda v: v is None or (_is_num(v) and 0 <= v <= 100),
             "door_open": lambda v: isinstance(v, bool)},
    "motor": {"position": _is_num, "speed": _is_num,
              "moving": lambda v: isinstance(v, bool), "homed": lambda v: isinstance(v, bool)},
}


# --------------------------------------------------------------------------- common


def check_common(rep: Report, raw: Raw, cap: str, state_path: str, opts: argparse.Namespace) -> None:
    # FuseIoT's connect() probes the base URL with its credentials attached.
    status, body = raw.request("GET", "/")
    rep.check("GET / reachable (<500, not 401)", status < 500 and status != 401, f"HTTP {status}")
    if isinstance(body, dict) and "device_type" in body:
        rep.check("GET / reports device_type", body["device_type"] == cap,
                  f"device_type={body['device_type']!r}")
    if not opts.contract_only:
        status, body = raw.request("GET", "/", token=None)
        rep.check("[fw] GET / open without credentials", status == 200, f"HTTP {status}")
        rep.check("[fw] GET / has device_type, id, fw, status", isinstance(body, dict)
                  and all(k in body for k in ("device_type", "id", "fw", "status")), str(body))

    status, body = raw.request("GET", state_path)
    ok = rep.check(f"GET {state_path} returns 200 JSON object", status == 200 and isinstance(body, dict),
                   f"HTTP {status}")
    if ok:
        for key, valid in STATE_SHAPES[cap].items():
            rep.check(f"state key {key!r} present and well-typed", key in body and valid(body[key]),
                      repr(body.get(key, "<missing>")))
        if cap == "sensor" and "timestamp" in body:
            ts = body["timestamp"]
            rep.check("sensor timestamp is unix seconds", _is_num(ts) and abs(ts - time.time()) < 3600,
                      repr(ts))

    if raw.token:
        status, _ = raw.request("GET", state_path, token=None)
        rep.check("request without token is rejected with 401", status == 401, f"HTTP {status}")
        status, _ = raw.request("GET", state_path, token=raw.token + "x")
        rep.check("request with a wrong token is rejected with 401", status == 401, f"HTTP {status}")
    else:
        rep.skip("token checks", "no --token given")

    if opts.contract_only:
        return
    status, body = raw.request("GET", "/info")
    rep.check("[fw] GET /info diagnostics", status == 200 and isinstance(body, dict) and all(
        k in body for k in ("id", "cap", "fw", "uptime_s", "rssi", "free_heap", "ip")), f"HTTP {status}")
    status, body = raw.request("GET", "/no-such-endpoint")
    rep.check("[fw] unknown path -> 404 JSON", status == 404 and isinstance(body, dict), f"HTTP {status}")
    status, body = raw.request("POST", BAD_JSON_PATH[cap], data="{not json")
    rep.check("[fw] malformed JSON -> 400 JSON", status == 400 and isinstance(body, dict)
              and "error" in body, f"HTTP {status}")
    status, _ = raw.request("POST", state_path, json={})
    rep.check(f"[fw] POST {state_path} -> 405", status == 405, f"HTTP {status}")


# ------------------------------------------------------------------- capabilities


def check_switchable(rep: Report, dev: Any, raw: Raw, opts: argparse.Namespace) -> None:
    rep.result("on() confirmed", dev.on())
    rep.check("is_on is True", dev.read_state(fresh=True).get("power") is True)
    rep.result("off() confirmed", dev.off())
    rep.result("toggle() confirmed (off -> on)", dev.toggle())
    rep.result("toggle() confirmed (on -> off)", dev.toggle())


def check_dimmable(rep: Report, dev: Any, raw: Raw, opts: argparse.Namespace) -> None:
    rep.result("on(brightness=40) confirmed", dev.on(brightness=40))
    rep.result("set_brightness(75) confirmed", dev.set_brightness(75))
    rep.result("fade_to(20, 800 ms) confirmed", dev.fade_to(20, duration_ms=800))
    rep.result("off() confirmed", dev.off())
    rep.result("on() confirmed", dev.on())
    if not opts.contract_only:
        status, _ = raw.request("POST", "/brightness", json={})
        rep.check("[fw] /brightness without brightness -> 400", status == 400, f"HTTP {status}")
        status, _ = raw.request("POST", "/brightness", json={"brightness": "high"})
        rep.check("[fw] non-numeric brightness -> 400", status == 400, f"HTTP {status}")
    rep.result("off() confirmed", dev.off())


def check_rgb_light(rep: Report, dev: Any, raw: Raw, opts: argparse.Namespace) -> None:
    rep.result("on(red, brightness=80) confirmed", dev.on(Color(255, 0, 0), brightness=80))
    rep.result("set_color(green) confirmed", dev.set_color(Color(0, 255, 0)))
    rep.result("set_hex('#3366ff') confirmed", dev.set_hex("#3366ff"))
    rep.result("set_brightness(30) confirmed", dev.set_brightness(30))
    rep.result("set_effect('rainbow', 60) confirmed", dev.set_effect("rainbow", 60))
    rep.result("set_effect('pulse') confirmed", dev.set_effect("pulse"))
    rep.result("set_effect('none') confirmed", dev.set_effect("none"))
    if not opts.contract_only:
        status, body = raw.request("POST", "/effect", json={"effect": "disco", "speed": 50})
        rep.check("[fw] unsupported effect -> 400", status == 400, f"HTTP {status} {body}")
        _, state = raw.request("GET", "/state")
        rep.check("[fw] color_mode is 'rgb' with effect none",
                  isinstance(state, dict) and state.get("color_mode") == "rgb", str(state))
    rep.result("off() confirmed", dev.off())


def check_sensor(rep: Report, dev: Any, raw: Raw, opts: argparse.Namespace) -> None:
    state = dev.read(fresh=True)
    rep.check("read() returns a numeric value", _is_num(state.get("value")), repr(state.get("value")))
    rep.check("unit reported", isinstance(state.get("unit"), str) and state["unit"] != "unknown",
              repr(state.get("unit")))
    if not opts.allow_calibrate:
        rep.skip("calibrate()", "changes the stored calibration; pass --allow-calibrate")
        return
    reference = round(float(state["value"]) + 1.5, 2)
    rep.result(f"calibrate({reference}) confirmed (calibrated: true)", dev.calibrate(reference))
    if not opts.contract_only:
        after = dev.read(fresh=True)["value"]
        rep.check("[fw] value follows the reference after calibration", abs(after - reference) < 1.0,
                  f"value={after}, reference={reference}")
        status, _ = raw.request("POST", "/calibrate", json={"reset": True})
        rep.check("[fw] calibration reset", status == 200, f"HTTP {status}")


def check_lock(rep: Report, dev: Any, raw: Raw, opts: argparse.Namespace) -> None:
    rep.result("lock() confirmed", dev.lock())
    if not opts.allow_unlock:
        rep.skip("unlock()", "would unlock a real door; pass --allow-unlock")
        return
    if opts.user_code and not opts.contract_only:
        status, body = raw.request("POST", "/lock", json={"action": "unlock", "user_code": opts.user_code + "9"})
        rep.check("[fw] wrong user_code -> 403", status == 403, f"HTTP {status} {body}")
        _, state = raw.request("GET", "/status")
        rep.check("[fw] still locked after a wrong code",
                  isinstance(state, dict) and state.get("lock_state") == "locked", str(state))
    rep.result("unlock() confirmed", dev.unlock(user_code=opts.user_code))
    rep.result("lock() confirmed", dev.lock())


def check_motor(rep: Report, dev: Any, raw: Raw, opts: argparse.Namespace) -> None:
    long = max(opts.timeout, 10.0)
    rep.result("home() confirmed (position 0, homed)", dev.home(timeout=long))
    rep.result("move_to(90, speed=100) confirmed", dev.move_to(90, speed=100, timeout=long))
    rep.result("set_speed(40) confirmed", dev.set_speed(40))
    rep.result("move_to(45) confirmed", dev.move_to(45, timeout=long))
    if not opts.contract_only:
        status, _ = raw.request("POST", "/move", json={"position": 45 + 135, "speed": 5})
        time.sleep(0.2)
        _, mid = raw.request("GET", "/status")
        rep.check("[fw] slow move reports moving=true", isinstance(mid, dict) and mid.get("moving") is True,
                  str(mid))
    rep.result("stop() confirmed (moving false)", dev.stop())
    if not opts.contract_only:
        status, _ = raw.request("POST", "/move", json={"position": 10000})
        rep.check("[fw] out-of-range position -> 400", status == 400, f"HTTP {status}")
    rep.result("home() confirmed", dev.home(timeout=long))


CHECKS = {
    "switchable": check_switchable, "dimmable": check_dimmable, "rgb_light": check_rgb_light,
    "sensor": check_sensor, "lock": check_lock, "motor": check_motor,
}


# ------------------------------------------------------------------------- driver


def run(url: str, opts: argparse.Namespace, title: Optional[str] = None) -> Report:
    raw = Raw(url, opts.token, opts.timeout)
    cap = opts.capability
    if not cap:
        try:
            _, body = raw.request("GET", "/", token=None)
            cap = body.get("device_type") if isinstance(body, dict) else None
        except requests.RequestException as e:
            rep = Report(title or url)
            rep.check("GET / reachable", False, str(e))
            return rep
    rep = Report(title or f"{url} ({cap})")
    if cap not in CAPABILITIES:
        rep.check("capability known", False, f"{cap!r}; pass --capability {'|'.join(CAPABILITIES)}")
        return rep
    cls, state_path = CAPABILITIES[cap]

    try:
        check_common(rep, raw, cap, state_path, opts)
    except requests.RequestException as e:
        rep.check("HTTP checks", False, str(e))
        return rep

    hub = Hub()
    try:
        protocol = HTTP(url, bearer_token=opts.token, timeout=opts.timeout)
        dev = hub.add_device(protocol, cls, device_id="dut",
                             config=CapabilityConfig(timeout=opts.timeout, poll_interval=0.1))
        rep.check("FuseIoT connect()", True)
        CHECKS[cap](rep, dev, raw, opts)
    except Exception as e:  # report, don't crash: this is a test tool
        rep.check("FuseIoT checks", False, f"{type(e).__name__}: {e}")
    finally:
        hub.close()
    return rep


def self_test(opts: argparse.Namespace) -> List[Report]:
    """Run the contract checks against FuseIoT's reference simulator."""
    from fuseiot.simulation import SimulatedHTTPDevice

    reports = []
    opts.contract_only = True  # the simulator implements the contract, not the firmware extras
    opts.allow_unlock = opts.allow_calibrate = True
    for cap in CAPABILITIES:
        opts.capability = cap
        opts.token = "self-test-token"
        # settle_ms makes state changes visible late, exercising confirmation polling.
        with SimulatedHTTPDevice(cap, auth_token=opts.token, settle_ms=150) as sim:
            reports.append(run(sim.url, opts, title=f"simulator: {cap}"))
    return reports


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", nargs="?", help="device base URL, e.g. http://fuseiot-lock-a1b2c3.local")
    p.add_argument("--token", help="bearer token (FUSEIOT_TOKEN)")
    p.add_argument("--capability", choices=sorted(CAPABILITIES),
                   help="device capability (default: device_type from GET /)")
    p.add_argument("--timeout", type=float, default=5.0, help="seconds per command incl. confirmation")
    p.add_argument("--contract-only", action="store_true",
                   help="skip [fw] checks specific to the FuseIoT ESP32 firmware")
    p.add_argument("--allow-unlock", action="store_true", help="lock: also test unlock()")
    p.add_argument("--user-code", help="lock: PIN for unlock (FUSEIOT_LOCK_PIN_CODE)")
    p.add_argument("--allow-calibrate", action="store_true", help="sensor: also test calibrate()")
    p.add_argument("--self-test", action="store_true", help="run against FuseIoT's simulator")
    opts = p.parse_args(argv)

    if opts.self_test:
        reports = self_test(opts)
    elif opts.url:
        reports = [run(opts.url, opts)]
    else:
        p.error("give a device URL or --self-test")

    print()
    for rep in reports:
        print(("OK    " if rep.failed == 0 else "FAIL  ") + rep.summary())
    return 0 if all(r.failed == 0 for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
