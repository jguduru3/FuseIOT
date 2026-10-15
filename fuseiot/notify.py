"""
Notifications to phones and other services.

Channels::

    notifications:
      channels:
        phone:    {type: ntfy, topic: my-home-8f3k2, url: https://ntfy.sh, token: ${NTFY_TOKEN}}
        family:   {type: telegram, bot_token: ${TG_TOKEN}, chat_id: "123456789"}
        hooks:    {type: webhook, url: https://example.com/hook, headers: {X-Key: ${HOOK_KEY}}}
        mail:     {type: email, host: smtp.gmail.com, port: 587, username: me@x.com,
                   password: ${SMTP_PASSWORD}, to: [me@x.com]}
      alerts:                       # automatic alerts -> channels
        device_offline: [phone]
        device_online: [phone]
        command_failed: [phone]
      cooldown: 300                 # seconds before repeating the same alert

``ntfy`` (https://ntfy.sh, free, self-hostable) is the easiest way to get push
notifications on Android and iOS: install the ntfy app and subscribe to your
topic. Pick a long, unguessable topic name, or self-host with access control.
"""
import smtplib
import ssl
import threading
import time
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from .exceptions import ConfigurationError
from .logging_config import get_logger

logger = get_logger("notify")

ALERT_EVENTS = ("device_offline", "device_online", "command_failed", "schedule_failed", "server_started")
PRIORITIES = ("min", "low", "default", "high", "urgent")


class Channel(ABC):
    """A destination for notifications. ``send`` raises on failure."""

    def __init__(self, name: str, timeout: float = 10.0):
        self.name = name
        self.timeout = timeout

    @property
    @abstractmethod
    def kind(self) -> str:
        """Channel type, e.g. ``ntfy``."""

    @abstractmethod
    def send(self, message: str, title: Optional[str] = None, priority: str = "default",
             tags: Optional[List[str]] = None) -> None:
        """Deliver one notification."""

    def _post(self, url: str, **kwargs: Any) -> Any:
        import requests

        response = requests.post(url, timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{self.kind} returned HTTP {response.status_code}: {response.text[:200]}")
        return response


class NtfyChannel(Channel):
    kind = "ntfy"  # type: ignore[assignment]

    def __init__(self, name: str, topic: str, url: str = "https://ntfy.sh", token: Optional[str] = None,
                 username: Optional[str] = None, password: Optional[str] = None, **kw: Any):
        super().__init__(name, **kw)
        if not topic or "/" in topic:
            raise ConfigurationError(f"ntfy channel {name!r}: invalid topic {topic!r}")
        self.url = url.rstrip("/")
        self.topic = topic
        self.token = token
        self.auth = (username, password) if username else None

    def send(self, message: str, title: Optional[str] = None, priority: str = "default",
             tags: Optional[List[str]] = None) -> None:
        headers = {"Priority": priority}
        if title:
            headers["Title"] = title.encode("utf-8").decode("latin-1", errors="replace")
        if tags:
            headers["Tags"] = ",".join(tags)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._post(f"{self.url}/{self.topic}", data=message.encode("utf-8"), headers=headers, auth=self.auth)


class TelegramChannel(Channel):
    kind = "telegram"  # type: ignore[assignment]

    def __init__(self, name: str, bot_token: str, chat_id: Any, api_url: str = "https://api.telegram.org",
                 **kw: Any):
        super().__init__(name, **kw)
        if not bot_token or chat_id in (None, ""):
            raise ConfigurationError(f"telegram channel {name!r} needs bot_token and chat_id")
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.api_url = api_url.rstrip("/")

    def send(self, message: str, title: Optional[str] = None, priority: str = "default",
             tags: Optional[List[str]] = None) -> None:
        text = f"{title}\n{message}" if title else message
        self._post(f"{self.api_url}/bot{self.bot_token}/sendMessage",
                   json={"chat_id": self.chat_id, "text": text,
                         "disable_notification": priority in ("min", "low")})


class WebhookChannel(Channel):
    kind = "webhook"  # type: ignore[assignment]

    def __init__(self, name: str, url: str, headers: Optional[Dict[str, str]] = None, **kw: Any):
        super().__init__(name, **kw)
        if not url.startswith(("http://", "https://")):
            raise ConfigurationError(f"webhook channel {name!r}: url must be http(s)")
        self.url = url
        self.headers = dict(headers or {})

    def send(self, message: str, title: Optional[str] = None, priority: str = "default",
             tags: Optional[List[str]] = None) -> None:
        self._post(self.url, json={"title": title, "message": message, "priority": priority,
                                   "tags": tags or [], "timestamp": time.time()}, headers=self.headers)


class EmailChannel(Channel):
    kind = "email"  # type: ignore[assignment]

    def __init__(self, name: str, host: str, to: Any, port: int = 587, username: Optional[str] = None,
                 password: Optional[str] = None, sender: Optional[str] = None, security: str = "starttls",
                 **kw: Any):
        super().__init__(name, **kw)
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.to = [to] if isinstance(to, str) else list(to or [])
        self.sender = sender or username
        if not self.to or not self.sender:
            raise ConfigurationError(f"email channel {name!r} needs 'to' and 'sender' (or username)")
        if security not in ("starttls", "ssl", "none"):
            raise ConfigurationError(f"email channel {name!r}: security must be starttls, ssl or none")
        self.security = security

    def send(self, message: str, title: Optional[str] = None, priority: str = "default",
             tags: Optional[List[str]] = None) -> None:
        msg = EmailMessage()
        msg["Subject"] = title or "FuseIoT notification"
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        if priority in ("high", "urgent"):
            msg["X-Priority"] = "1"
        msg.set_content(message)
        context = ssl.create_default_context()
        if self.security == "ssl":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with smtp:
            if self.security == "starttls":
                smtp.starttls(context=context)
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(msg)


class LogChannel(Channel):
    """Writes notifications to the log; handy for testing a setup."""
    kind = "log"  # type: ignore[assignment]

    def __init__(self, name: str, **kw: Any):
        super().__init__(name, **kw)
        self.sent: List[Dict[str, Any]] = []

    def send(self, message: str, title: Optional[str] = None, priority: str = "default",
             tags: Optional[List[str]] = None) -> None:
        self.sent.append({"title": title, "message": message, "priority": priority, "tags": tags})
        logger.warning("notification", channel=self.name, title=title, message=message, priority=priority)


CHANNEL_TYPES = {
    "ntfy": NtfyChannel,
    "telegram": TelegramChannel,
    "webhook": WebhookChannel,
    "email": EmailChannel,
    "log": LogChannel,
}


def channel_from_dict(name: str, spec: Dict[str, Any]) -> Channel:
    if not isinstance(spec, dict) or "type" not in spec:
        raise ConfigurationError(f"Notification channel {name!r} needs a 'type' ({', '.join(CHANNEL_TYPES)})")
    options = {k: v for k, v in spec.items() if k != "type"}
    try:
        cls = CHANNEL_TYPES[str(spec["type"]).lower()]
    except KeyError:
        raise ConfigurationError(
            f"Unknown notification type {spec['type']!r} for {name!r}; use {', '.join(CHANNEL_TYPES)}"
        ) from None
    try:
        return cls(name, **options)
    except TypeError as e:
        raise ConfigurationError(f"Invalid options for notification channel {name!r}: {e}") from None


class NotificationManager:
    """Named channels, automatic alert routing and per-alert cooldown."""

    def __init__(self, channels: Optional[Dict[str, Channel]] = None,
                 alerts: Optional[Dict[str, List[str]]] = None, cooldown: float = 300.0):
        self.channels: Dict[str, Channel] = dict(channels or {})
        self.alerts: Dict[str, List[str]] = {}
        self.cooldown = cooldown
        self._last_sent: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.history: List[Dict[str, Any]] = []
        for event, targets in (alerts or {}).items():
            if event not in ALERT_EVENTS:
                raise ConfigurationError(f"Unknown alert event {event!r}; use {', '.join(ALERT_EVENTS)}")
            targets = [targets] if isinstance(targets, str) else list(targets)
            for target in targets:
                if target not in self.channels:
                    raise ConfigurationError(f"Alert {event!r} uses unknown channel {target!r}")
            self.alerts[event] = targets

    @classmethod
    def from_config(cls, spec: Optional[Dict[str, Any]]) -> "NotificationManager":
        spec = spec or {}
        unknown = set(spec) - {"channels", "alerts", "cooldown"}
        if unknown:
            raise ConfigurationError(f"Unknown notifications keys: {sorted(unknown)}")
        channels = {name: channel_from_dict(name, ch) for name, ch in (spec.get("channels") or {}).items()}
        return cls(channels, spec.get("alerts") or {}, float(spec.get("cooldown", 300)))

    def has_channel(self, name: str) -> bool:
        return name in self.channels

    def send(self, channel: str, message: str, title: Optional[str] = None, priority: str = "default",
             tags: Optional[List[str]] = None) -> bool:
        """Send through one channel. Returns False (and logs) on failure; never raises."""
        target = self.channels.get(channel)
        if target is None:
            logger.error("notify_unknown_channel", channel=channel)
            return False
        if priority not in PRIORITIES:
            priority = "default"
        try:
            target.send(message, title=title, priority=priority, tags=tags)
        except Exception as e:
            logger.error("notify_failed", channel=channel, type=target.kind, error=str(e))
            self._record(channel, title, message, ok=False, error=str(e))
            return False
        logger.info("notify_sent", channel=channel, type=target.kind, title=title)
        self._record(channel, title, message, ok=True)
        return True

    def _record(self, channel: str, title: Optional[str], message: str, ok: bool,
                error: Optional[str] = None) -> None:
        with self._lock:
            self.history.append({"time": time.time(), "channel": channel, "title": title,
                                 "message": message, "ok": ok, "error": error})
            del self.history[:-100]

    def alert(self, event: str, title: str, message: str, key: Optional[str] = None,
              context: Optional[Dict[str, Any]] = None, priority: str = "high") -> int:
        """
        Route an automatic alert to its configured channels.

        Repeats of the same ``event``/``key`` within ``cooldown`` seconds are
        suppressed. Returns the number of channels that accepted it.
        """
        targets = self.alerts.get(event)
        if not targets:
            return 0
        dedupe = f"{event}:{key or title}"
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(dedupe)
            if last is not None and now - last < self.cooldown:
                return 0
            self._last_sent[dedupe] = now
        tag = {"device_offline": "warning", "device_online": "white_check_mark",
               "command_failed": "x"}.get(event)
        return sum(self.send(t, message, title=title, priority=priority, tags=[tag] if tag else None)
                   for t in targets)

    def describe(self) -> Dict[str, Any]:
        return {
            "channels": {name: ch.kind for name, ch in self.channels.items()},
            "alerts": self.alerts,
            "cooldown": self.cooldown,
            "recent": list(self.history[-20:]),
        }
