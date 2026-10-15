"""Notification channels (ntfy, Telegram, webhook, email, log) and the NotificationManager."""
import json
import smtplib
import time
from types import SimpleNamespace

import pytest
import responses

import fuseiot.notify as notify_module
from fuseiot.exceptions import ConfigurationError
from fuseiot.notify import (
    EmailChannel,
    LogChannel,
    NotificationManager,
    NtfyChannel,
    TelegramChannel,
    WebhookChannel,
    channel_from_dict,
)


@pytest.fixture
def mocked():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls = []
        self.messages = []
        self.closed = False
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.messages.append(msg)


class FakeSMTPSSL(FakeSMTP):
    instances = []


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTPSSL.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


class TestNtfy:
    def test_post_with_headers(self, mocked):
        mocked.add(responses.POST, "https://ntfy.example/home-topic", status=200)
        ch = NtfyChannel("phone", topic="home-topic", url="https://ntfy.example/", token="tk")
        ch.send("Door open", title="Alert", priority="high", tags=["warning", "door"])
        req = mocked.calls[0].request
        assert req.body == b"Door open"
        assert req.headers["Title"] == "Alert" and req.headers["Priority"] == "high"
        assert req.headers["Tags"] == "warning,door" and req.headers["Authorization"] == "Bearer tk"

    def test_minimal_headers_and_basic_auth(self, mocked):
        mocked.add(responses.POST, "https://ntfy.sh/t", status=200)
        NtfyChannel("p", topic="t", username="u", password="pw").send("hi")
        req = mocked.calls[0].request
        assert "Title" not in req.headers and "Tags" not in req.headers
        assert req.headers["Priority"] == "default"
        assert req.headers["Authorization"].startswith("Basic ")

    def test_unicode_message_and_title_do_not_crash(self, mocked):
        mocked.add(responses.POST, "https://ntfy.sh/t", status=200)
        NtfyChannel("p", topic="t").send("Température 21°C", title="Maison ☀")
        assert mocked.calls[0].request.body == "Température 21°C".encode()

    def test_http_error_raises(self, mocked):
        mocked.add(responses.POST, "https://ntfy.sh/t", status=403, body="forbidden")
        with pytest.raises(RuntimeError, match="ntfy returned HTTP 403: forbidden"):
            NtfyChannel("p", topic="t").send("x")

    @pytest.mark.parametrize("topic", ["", "a/b"])
    def test_invalid_topic(self, topic):
        with pytest.raises(ConfigurationError, match="invalid topic"):
            NtfyChannel("p", topic=topic)


class TestTelegram:
    def test_send_message(self, mocked):
        mocked.add(responses.POST, "https://tg.example/bot123:ABC/sendMessage", status=200, json={"ok": True})
        TelegramChannel("tg", bot_token="123:ABC", chat_id=42, api_url="https://tg.example/").send(
            "body", title="Head", priority="low")
        body = json.loads(mocked.calls[0].request.body)
        assert body == {"chat_id": "42", "text": "Head\nbody", "disable_notification": True}

    def test_default_api_and_normal_priority(self, mocked):
        mocked.add(responses.POST, "https://api.telegram.org/botT/sendMessage", status=200)
        TelegramChannel("tg", bot_token="T", chat_id="9").send("only")
        body = json.loads(mocked.calls[0].request.body)
        assert body["text"] == "only" and body["disable_notification"] is False

    @pytest.mark.parametrize("kwargs", [{"bot_token": "", "chat_id": 1}, {"bot_token": "t", "chat_id": ""},
                                        {"bot_token": "t", "chat_id": None}])
    def test_requires_token_and_chat(self, kwargs):
        with pytest.raises(ConfigurationError, match="needs bot_token and chat_id"):
            TelegramChannel("tg", **kwargs)


class TestWebhook:
    def test_json_body_and_headers(self, mocked):
        mocked.add(responses.POST, "https://hooks.example/x", status=204)
        WebhookChannel("h", url="https://hooks.example/x", headers={"X-Key": "k"}).send(
            "m", title="t", priority="urgent", tags=["a"])
        req = mocked.calls[0].request
        body = json.loads(req.body)
        assert body["title"] == "t" and body["message"] == "m" and body["priority"] == "urgent"
        assert body["tags"] == ["a"] and isinstance(body["timestamp"], float)
        assert req.headers["X-Key"] == "k"

    def test_server_error(self, mocked):
        mocked.add(responses.POST, "http://hooks.example/x", status=500)
        with pytest.raises(RuntimeError, match="HTTP 500"):
            WebhookChannel("h", url="http://hooks.example/x").send("m")

    def test_url_must_be_http(self):
        with pytest.raises(ConfigurationError, match="must be http"):
            WebhookChannel("h", url="ftp://x")


class TestEmail:
    def test_starttls_login_and_message(self, fake_smtp):
        ch = EmailChannel("mail", host="smtp.example", to="me@example.com", username="bot@example.com",
                          password="pw")
        ch.send("Body text", title="Subject!", priority="urgent")
        smtp = FakeSMTP.instances[0]
        assert (smtp.host, smtp.port) == ("smtp.example", 587)
        assert smtp.calls == ["starttls", ("login", "bot@example.com", "pw")] and smtp.closed
        msg = smtp.messages[0]
        assert msg["Subject"] == "Subject!" and msg["From"] == "bot@example.com"
        assert msg["To"] == "me@example.com" and msg["X-Priority"] == "1"
        assert msg.get_content().strip() == "Body text"

    def test_ssl_without_login(self, fake_smtp):
        ch = EmailChannel("mail", host="h", port="465", to=["a@x", "b@x"], sender="s@x", security="ssl")
        ch.send("b")
        assert FakeSMTP.instances == []
        smtp = FakeSMTPSSL.instances[0]
        assert smtp.port == 465 and smtp.context is not None and smtp.calls == []
        msg = smtp.messages[0]
        assert msg["To"] == "a@x, b@x" and msg["Subject"] == "FuseIoT notification" and msg["X-Priority"] is None

    def test_plain_security_none(self, fake_smtp):
        EmailChannel("mail", host="h", to="a@x", sender="s@x", security="none").send("b")
        assert FakeSMTP.instances[-1].calls == []

    @pytest.mark.parametrize("kwargs,fragment", [
        ({"to": []}, "needs 'to' and 'sender'"),
        ({"to": "a@x", "sender": None}, "needs 'to' and 'sender'"),
        ({"to": "a@x", "sender": "s@x", "security": "tls"}, "security must be"),
    ])
    def test_validation(self, kwargs, fragment):
        with pytest.raises(ConfigurationError, match=fragment):
            EmailChannel("mail", host="h", **kwargs)


class TestChannelFromDict:
    def test_builds_each_type(self):
        assert isinstance(channel_from_dict("a", {"type": "ntfy", "topic": "t"}), NtfyChannel)
        assert isinstance(channel_from_dict("b", {"type": "Telegram", "bot_token": "x", "chat_id": 1}),
                          TelegramChannel)
        assert isinstance(channel_from_dict("c", {"type": "webhook", "url": "https://x"}), WebhookChannel)
        assert isinstance(channel_from_dict("d", {"type": "email", "host": "h", "to": "a@x", "sender": "s@x"}),
                          EmailChannel)
        ch = channel_from_dict("e", {"type": "log", "timeout": 3})
        assert isinstance(ch, LogChannel) and ch.timeout == 3 and ch.name == "e" and ch.kind == "log"

    @pytest.mark.parametrize("spec,fragment", [
        ({}, "needs a 'type'"),
        ("ntfy", "needs a 'type'"),
        ({"type": "pager"}, "Unknown notification type 'pager'"),
        ({"type": "ntfy", "topic": "t", "colour": "red"}, "Invalid options"),
        ({"type": "telegram", "bot_token": "x"}, "Invalid options"),
    ])
    def test_errors(self, spec, fragment):
        with pytest.raises(ConfigurationError, match=fragment):
            channel_from_dict("n", spec)


class Exploding(LogChannel):
    def send(self, *a, **kw):
        raise RuntimeError("boom")


class TestNotificationManager:
    def test_from_config(self):
        mgr = NotificationManager.from_config({
            "channels": {"phone": {"type": "log"}, "hook": {"type": "webhook", "url": "https://x"}},
            "alerts": {"device_offline": ["phone", "hook"], "device_online": "phone"},
            "cooldown": 10,
        })
        assert mgr.has_channel("phone") and not mgr.has_channel("sms")
        assert mgr.alerts == {"device_offline": ["phone", "hook"], "device_online": ["phone"]}
        assert mgr.describe()["channels"] == {"phone": "log", "hook": "webhook"}
        assert mgr.cooldown == 10.0

    def test_from_empty_config(self):
        mgr = NotificationManager.from_config(None)
        assert mgr.channels == {} and mgr.describe()["recent"] == [] and mgr.cooldown == 300

    @pytest.mark.parametrize("spec,fragment", [
        ({"channel": {}}, "Unknown notifications keys"),
        ({"channels": {"p": {"type": "log"}}, "alerts": {"door_open": ["p"]}}, "Unknown alert event"),
        ({"channels": {"p": {"type": "log"}}, "alerts": {"device_offline": ["sms"]}}, "unknown channel 'sms'"),
        ({"channels": {"p": {"type": "bogus"}}}, "Unknown notification type"),
    ])
    def test_from_config_errors(self, spec, fragment):
        with pytest.raises(ConfigurationError, match=fragment):
            NotificationManager.from_config(spec)

    def test_send_records_history(self):
        log = LogChannel("log")
        mgr = NotificationManager({"log": log})
        assert mgr.send("log", "hello", title="T", priority="urgent", tags=["a"]) is True
        assert log.sent == [{"title": "T", "message": "hello", "priority": "urgent", "tags": ["a"]}]
        entry = mgr.history[-1]
        assert entry["ok"] is True and entry["channel"] == "log" and entry["error"] is None

    def test_send_never_raises(self):
        mgr = NotificationManager({"bad": Exploding("bad")})
        assert mgr.send("bad", "x") is False
        assert mgr.history[-1]["ok"] is False and mgr.history[-1]["error"] == "boom"
        assert mgr.send("missing", "x") is False
        assert len(mgr.history) == 1

    def test_http_failure_via_manager(self, mocked):
        mocked.add(responses.POST, "https://ntfy.sh/t", status=500)
        mgr = NotificationManager.from_config({"channels": {"p": {"type": "ntfy", "topic": "t"}}})
        assert mgr.send("p", "x") is False
        assert "HTTP 500" in mgr.history[-1]["error"]

    def test_history_is_bounded(self):
        mgr = NotificationManager({"log": LogChannel("log")})
        for i in range(130):
            mgr.send("log", str(i))
        assert len(mgr.history) == 100 and mgr.history[-1]["message"] == "129"
        assert len(mgr.describe()["recent"]) == 20

    def test_alert_routing_and_tags(self):
        a, b = LogChannel("a"), LogChannel("b")
        mgr = NotificationManager({"a": a, "b": b}, alerts={"device_offline": ["a", "b"],
                                                            "device_online": ["b"]})
        assert mgr.alert("device_offline", title="Lamp offline", message="m", key="lamp") == 2
        assert a.sent[0]["tags"] == ["warning"] and a.sent[0]["priority"] == "high"
        assert mgr.alert("device_online", title="Lamp online", message="m", key="lamp") == 1
        assert b.sent[-1]["tags"] == ["white_check_mark"]
        assert mgr.alert("command_failed", title="x", message="m") == 0  # not routed

    def test_alert_without_tag(self):
        a = LogChannel("a")
        mgr = NotificationManager({"a": a}, alerts={"server_started": "a"})
        assert mgr.alert("server_started", title="up", message="m", priority="low") == 1
        assert a.sent[0]["tags"] is None and a.sent[0]["priority"] == "low"

    def test_alert_cooldown_per_event_and_key(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(notify_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
        a = LogChannel("a")
        mgr = NotificationManager({"a": a}, alerts={"device_offline": ["a"], "device_online": ["a"]}, cooldown=60)
        assert mgr.alert("device_offline", "t", "m", key="lamp") == 1
        assert mgr.alert("device_offline", "t", "m", key="lamp") == 0          # suppressed
        assert mgr.alert("device_offline", "t", "m", key="fan") == 1           # other key
        assert mgr.alert("device_online", "t", "m", key="lamp") == 1           # other event
        clock[0] += 59
        assert mgr.alert("device_offline", "t", "m", key="lamp") == 0
        clock[0] += 2
        assert mgr.alert("device_offline", "t", "m", key="lamp") == 1
        assert len(a.sent) == 4

    def test_alert_dedupes_on_title_without_key(self):
        a = LogChannel("a")
        mgr = NotificationManager({"a": a}, alerts={"command_failed": ["a"]})
        assert mgr.alert("command_failed", "lamp: on failed", "m") == 1
        assert mgr.alert("command_failed", "lamp: on failed", "m2") == 0
        assert mgr.alert("command_failed", "fan: on failed", "m") == 1

    def test_alert_counts_only_successful_channels(self):
        good = LogChannel("good")
        mgr = NotificationManager({"good": good, "bad": Exploding("bad")},
                                  alerts={"device_offline": ["bad", "good"]})
        assert mgr.alert("device_offline", "t", "m") == 1
        assert [h["ok"] for h in mgr.history] == [False, True]

    def test_zero_cooldown_never_suppresses(self):
        a = LogChannel("a")
        mgr = NotificationManager({"a": a}, alerts={"device_offline": ["a"]}, cooldown=0)
        assert mgr.alert("device_offline", "t", "m") == 1
        assert mgr.alert("device_offline", "t", "m") == 1
