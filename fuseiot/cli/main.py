"""
``fuseiot`` command-line interface.

Devices come from the config file (``--config``, or ``FUSEIOT_CONFIG``)::

    fuseiot -c fuseiot.yaml devices
    fuseiot -c fuseiot.yaml on living_room_light
    fuseiot -c fuseiot.yaml cmd desk_lamp set_brightness brightness=40
    fuseiot -c fuseiot.yaml serve --host 0.0.0.0 --port 8080
"""
import json
import signal
import sys
import threading
from typing import Any, Dict, Optional, Tuple

import click

from fuseiot.__version__ import __version__
from fuseiot.config import Config, auto_config
from fuseiot.exceptions import ConfigurationError, FuseIoTError, StateError
from fuseiot.logging_config import configure_logging

EXIT_FAILED = 1
EXIT_CONFIG = 2


class State:
    def __init__(self, config_path: Optional[str], verbose: bool, as_json: bool):
        self.config_path = config_path
        self.verbose = verbose
        self.as_json = as_json
        self._config: Optional[Config] = None
        self._hub: Any = None

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = auto_config(self.config_path)
            except ConfigurationError as e:
                _fail(str(e), EXIT_CONFIG)
        return self._config  # type: ignore[return-value]

    def hub(self) -> Any:
        if self._hub is None:
            from fuseiot.hub import Hub, HubConfig

            self._hub = Hub(HubConfig.from_config(self.config))
        return self._hub

    def device(self, device_id: str) -> Any:
        """Load only the requested device from the config."""
        from fuseiot.registry import add_device_from_spec

        hub = self.hub()
        if device_id in hub:
            return hub[device_id]
        for entry in self.config.devices:
            if entry.get("id", entry.get("device_id")) == device_id:
                try:
                    return add_device_from_spec(hub, entry, connect=False)
                except ConfigurationError as e:
                    _fail(str(e), EXIT_CONFIG)
        known = ", ".join(e.get("id", "?") for e in self.config.devices) or "none"
        _fail(f"Device not found in config: {device_id} (configured: {known})", EXIT_CONFIG)

    def close(self) -> None:
        if self._hub is not None:
            self._hub.close()


def _fail(message: str, code: int = EXIT_FAILED) -> None:
    click.secho(f"Error: {message}", fg="red", err=True)
    sys.exit(code)


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def _parse_params(items: Tuple[str, ...]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise click.BadParameter(f"expected key=value, got {item!r}")
        key, raw = item.split("=", 1)
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


def _report(st: State, result: Any) -> None:
    if st.as_json:
        _echo_json(result.to_dict())
    elif result.success:
        mark = "OK" if result.confirmed else "SENT"
        detail = "confirmed" if result.confirmed else "acknowledged, not verified"
        click.secho(f"{mark} {result.device_id} {result.command}: {detail} "
                    f"({result.latency_ms:.0f} ms)", fg="green")
    else:
        click.secho(f"FAILED {result.device_id} {result.command}: [{result.status.name}] {result.error}",
                    fg="red", err=True)
    if not result.success:
        sys.exit(EXIT_FAILED)


def _run(st: State, device_id: str, command: str, params: Dict[str, Any]) -> None:
    device = st.device(device_id)
    if command not in device.commands:
        _fail(f"{device_id} ({device.category}) has no command {command!r}. "
              f"Available: {', '.join(device.commands)}", EXIT_CONFIG)
    try:
        result = getattr(device, command)(**params)
    except TypeError as e:
        _fail(f"Invalid parameters for {command}: {e}", EXIT_CONFIG)
    except (ValueError, FuseIoTError) as e:
        _fail(str(e))
    _report(st, result)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--config", "-c", "config_path", envvar="FUSEIOT_CONFIG", type=click.Path(dir_okay=False),
              help="Config file (YAML or JSON). Env: FUSEIOT_CONFIG")
@click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.version_option(__version__, prog_name="fuseiot")
@click.pass_context
def cli(ctx: click.Context, config_path: Optional[str], verbose: bool, as_json: bool) -> None:
    """FuseIoT - deterministic device control."""
    configure_logging(level="DEBUG" if verbose else "WARNING")
    st = State(config_path, verbose, as_json)
    ctx.obj = st
    ctx.call_on_close(st.close)


@cli.command()
@click.option("--timeout", "-t", default=5.0, show_default=True, help="Scan duration in seconds.")
@click.option("--service", "-s", default=None, help="mDNS service type, e.g. _http._tcp.local.")
@click.option("--yaml", "as_yaml", is_flag=True,
              help="Print a ready 'devices:' config snippet for FuseIoT ESP32 firmware found.")
@click.pass_obj
def discover(st: State, timeout: float, service: Optional[str], as_yaml: bool) -> None:
    """Discover devices on the local network (mDNS)."""
    from fuseiot.discovery.mdns import MDNSDiscovery, config_entries

    if not st.as_json and not as_yaml:
        click.echo(f"Scanning for {timeout:.0f}s...")
    try:
        found = MDNSDiscovery().scan(service_type=service or ("_fuseiot._tcp.local." if as_yaml else None),
                                     timeout=timeout)
    except FuseIoTError as e:
        _fail(str(e))
    if as_yaml:
        import yaml

        entries = config_entries(found)
        if not entries:
            click.echo("# No FuseIoT devices found (they advertise _fuseiot._tcp).", err=True)
            return
        click.echo(yaml.safe_dump({"devices": entries}, sort_keys=False).rstrip())
        return
    if st.as_json:
        _echo_json([d.__dict__ for d in found])
        return
    if not found:
        click.echo("No devices found.")
        return
    click.echo(f"Found {len(found)} device(s):")
    for d in found:
        click.echo(f"  {d.name}\n    {d.protocol}://{d.address}:{d.port}  ({d.device_type})")


@cli.command("devices")
@click.pass_obj
def list_devices(st: State) -> None:
    """List devices defined in the config file."""
    rows = [{"id": e.get("id"), "capability": e.get("capability"), "protocol": e.get("protocol"),
             "endpoint": e.get("endpoint")} for e in st.config.devices]
    if st.as_json:
        _echo_json(rows)
        return
    if not rows:
        click.echo("No devices configured. Add a 'devices:' list to your config file.")
        return
    click.echo(f"{'ID':<28} {'CAPABILITY':<15} {'PROTOCOL':<10} ENDPOINT")
    for r in rows:
        click.echo(f"{r['id']!s:<28} {r['capability']!s:<15} {r['protocol']!s:<10} {r['endpoint'] or ''}")


@cli.command()
@click.argument("device_id")
@click.option("--cached", is_flag=True, help="Use cached state instead of polling the device.")
@click.pass_obj
def status(st: State, device_id: str, cached: bool) -> None:
    """Read a device's current state."""
    device = st.device(device_id)
    try:
        state = device.read_state(fresh=not cached)
    except StateError as e:
        _fail(e.message)
    info = {**device.describe(), "state": state}
    if st.as_json:
        _echo_json(info)
        return
    click.echo(f"{device_id}  ({info['category']} via {info['protocol']} {info['endpoint']})")
    for key, value in state.items():
        click.echo(f"  {key:<20} {value}")
    click.echo(f"  commands: {', '.join(device.commands)}")


def _power_command(name: str) -> Any:
    @click.argument("device_id")
    @click.option("--confirm/--no-confirm", default=None, help="Verify state afterwards (default: config).")
    @click.option("--timeout", "-t", type=float, default=None, help="Seconds allowed incl. confirmation.")
    @click.pass_obj
    def command(st: State, device_id: str, confirm: Optional[bool], timeout: Optional[float]) -> None:
        params: Dict[str, Any] = {}
        if confirm is not None:
            params["confirm"] = confirm
        if timeout is not None:
            params["timeout"] = timeout
        _run(st, device_id, name, params)

    command.__doc__ = f"Turn a device {name}." if name != "toggle" else "Toggle a device."
    return command


cli.command("on")(_power_command("on"))
cli.command("off")(_power_command("off"))
cli.command("toggle")(_power_command("toggle"))


@cli.command("cmd")
@click.argument("device_id")
@click.argument("command")
@click.argument("params", nargs=-1)
@click.pass_obj
def run_command(st: State, device_id: str, command: str, params: Tuple[str, ...]) -> None:
    """Run any device command: cmd DEVICE COMMAND [key=value ...].

    Values are parsed as JSON when possible (40 -> number, true -> bool).
    """
    _run(st, device_id, command, _parse_params(params))


@cli.command()
@click.pass_obj
def stats(st: State) -> None:
    """Show hub statistics."""
    _echo_json(st.hub().stats())


@cli.command()
@click.pass_obj
def validate(st: State) -> None:
    """Validate the config (devices, rules, schedules, notifications) without contacting anything."""
    from fuseiot.runtime import validate_config

    cfg = st.config
    errors = validate_config(cfg)
    if errors:
        for err in errors:
            click.secho(f"  - {err}", fg="red", err=True)
        _fail(f"{len(errors)} problem(s) in configuration", EXIT_CONFIG)
    channels = len((cfg.notifications or {}).get("channels") or {})
    click.secho(f"Configuration OK: {len(cfg.devices)} device(s), {len(cfg.rules)} rule(s), "
                f"{len(cfg.schedules)} schedule(s), {channels} notification channel(s).", fg="green")


@cli.command()
@click.option("--host", default=None, help="Bind address (default: config server_host).")
@click.option("--port", "-p", type=int, default=None, help="Port (default: config server_port).")
@click.option("--token", envvar="FUSEIOT_API_TOKEN", default=None,
              help="Service API token (admin). Env: FUSEIOT_API_TOKEN")
@click.option("--insecure", is_flag=True, help="Allow serving on a public interface without authentication.")
@click.option("--no-rules", is_flag=True, help="Do not run automation rules.")
@click.pass_obj
def serve(st: State, host: Optional[str], port: Optional[int], token: Optional[str],
          insecure: bool, no_rules: bool) -> None:
    """Run the app, REST API, health checks, metrics, rules, schedules and device monitor."""
    from fuseiot.auth import UserStore
    from fuseiot.runtime import Runtime

    cfg = st.config
    bind_host = host or cfg.server_host
    api_token = token or cfg.api_token
    has_users = False
    if cfg.users_db:
        store = UserStore(cfg.users_db)
        has_users = store.has_users()
        store.close()
    if bind_host not in ("127.0.0.1", "localhost", "::1") and not (api_token or has_users) and not insecure:
        _fail(f"Refusing to serve on {bind_host} without authentication. Create a user "
              "('fuseiot user add NAME --role admin', needs users_db), set FUSEIOT_API_TOKEN, "
              "use --host 127.0.0.1, or pass --insecure on a trusted network.", EXIT_CONFIG)
    configure_logging(level="DEBUG" if st.verbose else cfg.log_level, json_format=cfg.log_json,
                      log_file=cfg.log_file)
    try:
        rt = Runtime.from_config(cfg, with_rules=not no_rules)
        st._hub = rt.hub
        server = rt.make_server(host=bind_host, port=port, api_token=api_token, allow_insecure=insecure)
    except (ConfigurationError, FuseIoTError, OSError) as e:
        _fail(str(e), EXIT_CONFIG)

    stop = threading.Event()

    def shutdown(signum: int, frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    rt.start()
    server.start()
    parts = [f"{len(rt.hub)} device(s)"]
    if rt.rules is not None:
        parts.append(f"{len(rt.rules.rules)} rule(s)")
    if rt.scheduler is not None:
        parts.append(f"{len(rt.scheduler.schedules)} schedule(s)")
    if rt.notifier is not None:
        parts.append(f"{len(rt.notifier.channels)} notification channel(s)")
    auth = "users" if has_users else ("token" if api_token else "off")
    click.echo(f"FuseIoT {__version__} serving {', '.join(parts)} at {server.url} (auth: {auth})")
    if rt.notifier is not None:
        rt.notifier.alert("server_started", title="FuseIoT started",
                          message=f"Serving {', '.join(parts)}.", priority="low")
    try:
        while not stop.wait(0.5):
            pass
    finally:
        click.echo("Shutting down...")
        rt.stop()
        st._hub = None


# ---------------------------------------------------------------- schedules


@cli.command("schedules")
@click.pass_obj
def list_schedules(st: State) -> None:
    """Show schedules with their next run time (and today's sunrise/sunset)."""
    from fuseiot.automation.schedule import Location, Scheduler
    from fuseiot.automation.sun import sunrise, sunset

    cfg = st.config
    try:
        location = Location.from_dict(cfg.location)
        hub = st.hub()
        from fuseiot.registry import load_devices

        load_devices(hub, cfg.devices, connect=False) if not len(hub) else None
        scheduler = Scheduler.from_config(hub, cfg.schedules, cfg.location)
    except ConfigurationError as e:
        _fail(str(e), EXIT_CONFIG)
    items = scheduler.list_schedules()
    if st.as_json:
        _echo_json(items)
        return
    tz = location.tz
    if location.has_coordinates:
        today = location.now().date()
        rise = sunrise(today, float(location.latitude), float(location.longitude), tz)
        fall = sunset(today, float(location.latitude), float(location.longitude), tz)
        click.echo(f"Today ({tz}): sunrise {rise.strftime('%H:%M') if rise else '-'}, "
                   f"sunset {fall.strftime('%H:%M') if fall else '-'}")
    if not items:
        click.echo("No schedules. Add a 'schedules:' list to your config file.")
        return
    for s in items:
        when = (s["cron"] and f"cron {s['cron']}") or f"at {s['at']}"
        days = "" if s["cron"] else ("daily" if len(s["days"]) == 7 else ",".join(s["days"]))
        nxt = s["next_run"][:16].replace("T", " ") if s["next_run"] else ("disabled" if not s["enabled"] else "-")
        click.echo(f"  {s['name']:<28} {when:<22} {days:<16} next: {nxt}")


# ---------------------------------------------------------------- notifications


@cli.group()
def notify() -> None:
    """Notification channels (ntfy, Telegram, webhook, email)."""


@notify.command("test")
@click.argument("channel")
@click.option("--message", "-m", default="This is a test notification from FuseIoT.")
@click.pass_obj
def notify_test(st: State, channel: str, message: str) -> None:
    """Send a test notification through CHANNEL."""
    from fuseiot.notify import NotificationManager

    try:
        manager = NotificationManager.from_config(st.config.notifications)
    except ConfigurationError as e:
        _fail(str(e), EXIT_CONFIG)
    if not manager.has_channel(channel):
        known = ", ".join(manager.channels) or "none configured"
        _fail(f"Unknown channel {channel!r} (configured: {known})", EXIT_CONFIG)
    if manager.send(channel, message, title="FuseIoT test"):
        click.secho(f"Sent via {channel}.", fg="green")
    else:
        _fail(f"Sending via {channel} failed; run with -v for details.")


# ---------------------------------------------------------------- users


def _user_store(st: State) -> Any:
    from fuseiot.auth import UserStore

    if not st.config.users_db:
        _fail("User accounts need 'users_db' in the config (e.g. users_db: ./data/users.db).", EXIT_CONFIG)
    return UserStore(st.config.users_db)


def _ask_password(password: Optional[str]) -> str:
    if password:
        return password
    return click.prompt("Password", hide_input=True, confirmation_prompt=True)


@cli.group()
def user() -> None:
    """Manage user accounts for the app and API."""


@user.command("add")
@click.argument("username")
@click.option("--role", type=click.Choice(["viewer", "operator", "admin"]), default="operator", show_default=True)
@click.option("--password", envvar="FUSEIOT_NEW_PASSWORD", default=None,
              help="Password (prompted if omitted). Env: FUSEIOT_NEW_PASSWORD")
@click.pass_obj
def user_add(st: State, username: str, role: str, password: Optional[str]) -> None:
    """Create a user."""
    store = _user_store(st)
    try:
        store.add_user(username, _ask_password(password), role)
    except ConfigurationError as e:
        _fail(e.message, EXIT_CONFIG)
    finally:
        store.close()
    click.secho(f"User {username!r} created with role {role}.", fg="green")


@user.command("list")
@click.pass_obj
def user_list(st: State) -> None:
    """List users."""
    store = _user_store(st)
    users = store.list_users()
    store.close()
    if st.as_json:
        _echo_json(users)
        return
    if not users:
        click.echo("No users yet. Create one with: fuseiot user add NAME --role admin")
    for u in users:
        click.echo(f"  {u['username']:<24} {u['role']:<9} {'disabled' if u['disabled'] else ''}")


@user.command("passwd")
@click.argument("username")
@click.option("--password", envvar="FUSEIOT_NEW_PASSWORD", default=None)
@click.pass_obj
def user_passwd(st: State, username: str, password: Optional[str]) -> None:
    """Set a user's password (signs them out of all sessions)."""
    store = _user_store(st)
    try:
        store.set_password(username, _ask_password(password))
    except ConfigurationError as e:
        _fail(e.message, EXIT_CONFIG)
    finally:
        store.close()
    click.secho("Password updated.", fg="green")


@user.command("role")
@click.argument("username")
@click.argument("role", type=click.Choice(["viewer", "operator", "admin"]))
@click.pass_obj
def user_role(st: State, username: str, role: str) -> None:
    """Change a user's role."""
    store = _user_store(st)
    try:
        store.set_role(username, role)
    except ConfigurationError as e:
        _fail(e.message, EXIT_CONFIG)
    finally:
        store.close()
    click.secho(f"{username} is now {role}.", fg="green")


@user.command("disable")
@click.argument("username")
@click.pass_obj
def user_disable(st: State, username: str) -> None:
    """Disable a user and revoke their tokens."""
    store = _user_store(st)
    try:
        store.set_disabled(username, True)
    except ConfigurationError as e:
        _fail(e.message, EXIT_CONFIG)
    finally:
        store.close()
    click.secho(f"{username} disabled.", fg="green")


@user.command("enable")
@click.argument("username")
@click.pass_obj
def user_enable(st: State, username: str) -> None:
    """Re-enable a disabled user."""
    store = _user_store(st)
    try:
        store.set_disabled(username, False)
    except ConfigurationError as e:
        _fail(e.message, EXIT_CONFIG)
    finally:
        store.close()
    click.secho(f"{username} enabled.", fg="green")


@user.command("remove")
@click.argument("username")
@click.confirmation_option(prompt="Delete this user and all their tokens?")
@click.pass_obj
def user_remove(st: State, username: str) -> None:
    """Delete a user."""
    store = _user_store(st)
    try:
        store.remove_user(username)
    except ConfigurationError as e:
        _fail(e.message, EXIT_CONFIG)
    finally:
        store.close()
    click.secho(f"{username} removed.", fg="green")


@user.command("token")
@click.argument("username")
@click.option("--name", default="cli", show_default=True, help="Label for the token.")
@click.option("--days", type=float, default=None, help="Expire after N days (default: never).")
@click.pass_obj
def user_token(st: State, username: str, name: str, days: Optional[float]) -> None:
    """Create an API token for USERNAME (printed once)."""
    store = _user_store(st)
    try:
        token, _ = store.issue_token(username, name=name, kind="api", ttl=days * 86400 if days else None)
    except ConfigurationError as e:
        _fail(e.message, EXIT_CONFIG)
    finally:
        store.close()
    click.echo(token)


@cli.command()
@click.argument("device_type")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", "-p", type=int, default=8001, show_default=True)
@click.option("--settle-ms", type=float, default=0.0, help="Delay before commanded state is visible.")
@click.option("--failure-rate", type=float, default=0.0, help="Probability (0-1) of simulated failures.")
@click.option("--token", default=None, help="Require this bearer token.")
def simulate(device_type: str, host: str, port: int, settle_ms: float, failure_rate: float,
             token: Optional[str]) -> None:
    """Run a simulated HTTP device (switchable, dimmable, sensor, motor, ...)."""
    from fuseiot.simulation.http_device import SimulatedHTTPDevice

    try:
        dev = SimulatedHTTPDevice(device_type, host=host, port=port, auth_token=token,
                                  settle_ms=settle_ms, failure_rate=failure_rate)
    except (ValueError, OSError) as e:
        _fail(str(e), EXIT_CONFIG)
    click.echo(f"Simulated {device_type} at {dev.url} (Ctrl+C to stop)")
    try:
        dev.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dev.stop()


def main() -> None:
    """Console-script entry point."""
    cli(prog_name="fuseiot")


if __name__ == "__main__":
    main()
