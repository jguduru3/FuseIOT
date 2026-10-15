"""Shared automation actions, message templates and notify actions in rules."""
import re

import pytest

from fuseiot import Sensor, Switchable
from fuseiot.automation.actions import Action, NotifyAction, as_list, parse_action, render_template
from fuseiot.edge.rules import RuleEngine, rule_from_dict
from fuseiot.exceptions import ConfigurationError
from fuseiot.notify import LogChannel, NotificationManager


@pytest.fixture
def home(add_virtual):
    temp = add_virtual("sensor", Sensor, "hall_temp", initial_state={"value": 21.456})
    lamp = add_virtual("switchable", Switchable, "lamp")
    temp.read_state(fresh=True)
    lamp.read_state(fresh=True)
    return temp, lamp


@pytest.fixture
def log_channel(hub):
    channel = LogChannel("log")
    hub.notifier = NotificationManager({"log": channel})
    return channel


class TestRenderTemplate:
    def test_device_values_and_float_formatting(self, hub, home):
        assert render_template("Hall {hall_temp.value} C", hub) == "Hall 21.5 C"
        assert render_template("lamp={lamp.power}", hub) == "lamp=False"

    def test_context_and_builtin_placeholders(self, hub):
        out = render_template("{rule} at {time} on {date}", hub, {"rule": "frost"})
        assert re.fullmatch(r"frost at \d\d:\d\d on \d{4}-\d\d-\d\d", out)

    def test_context_overrides_time(self, hub):
        assert render_template("{time}", hub, {"time": "noon"}) == "noon"

    def test_unknown_placeholders_kept(self, hub, home):
        text = "{nobody.value} {hall_temp.missing} {plain} {{braces}}"
        assert render_template(text, hub) == "{nobody.value} {hall_temp.missing} {plain} {{braces}}"

    def test_device_without_cached_state_is_kept(self, hub, add_virtual):
        add_virtual("sensor", Sensor, "cold")
        assert render_template("{cold.value}", hub) == "{cold.value}"

    def test_non_string_context_values(self, hub):
        assert render_template("{n}-{schedule}", hub, {"n": 3, "schedule": None}) == "3-None"


class TestParseAction:
    def test_device_action(self):
        a = parse_action({"device": "lamp", "command": "on", "params": {"x": 1}, "delay": "0.5"})
        assert isinstance(a, Action)
        assert (a.device_id, a.method, a.params, a.delay) == ("lamp", "on", {"x": 1}, 0.5)
        assert a.devices == ["lamp"]

    def test_notify_action(self):
        a = parse_action({"notify": "phone", "message": "Hi", "title": "T", "priority": "high"})
        assert isinstance(a, NotifyAction)
        assert (a.channel, a.message, a.title, a.priority, a.delay) == ("phone", "Hi", "T", "high", 0.0)
        assert a.devices == []

    def test_notify_defaults(self):
        a = parse_action({"notify": 5, "message": 7})
        assert a.channel == "5" and a.message == "7" and a.title is None and a.priority == "default"

    @pytest.mark.parametrize("spec,fragment", [
        (["lamp"], "must be a mapping"),
        ({"device": "lamp"}, "needs 'device' and 'command'"),
        ({"command": "on"}, "needs 'device' and 'command'"),
        ({"notify": "phone"}, "needs a 'message'"),
    ])
    def test_errors(self, spec, fragment):
        with pytest.raises(ConfigurationError, match=fragment):
            parse_action(spec)

    def test_as_list(self):
        assert as_list({"a": 1}, "o", "k") == [{"a": 1}]
        assert as_list([{"a": 1}, {"b": 2}], "o", "k") == [{"a": 1}, {"b": 2}]
        with pytest.raises(ConfigurationError, match="o: 'k' must be a mapping"):
            as_list([{"a": 1}, "x"], "o", "k")


class TestExecute:
    async def test_action_runs_command(self, hub, home):
        _, lamp = home
        assert await Action("lamp", "on").execute(hub) is True
        assert lamp.is_on

    async def test_failed_action_sends_command_failed_alert(self, hub, add_virtual):
        add_virtual("switchable", Switchable, "broken", failure_rate=1.0)
        channel = LogChannel("log")
        hub.notifier = NotificationManager({"log": channel}, alerts={"command_failed": ["log"]})
        assert await Action("broken", "on").execute(hub, {"rule": "r"}) is False
        assert len(channel.sent) == 1
        assert channel.sent[0]["title"] == "broken: on failed" and channel.sent[0]["tags"] == ["x"]

    async def test_unknown_device_returns_false(self, hub):
        assert await Action("ghost", "on").execute(hub) is False

    async def test_notify_renders_message_and_title(self, hub, home, log_channel):
        action = NotifyAction("log", "Hall is {hall_temp.value} C ({rule})", title="{rule}!", priority="high")
        assert await action.execute(hub, {"rule": "frost"}) is True
        assert log_channel.sent == [{"title": "frost!", "message": "Hall is 21.5 C (frost)", "priority": "high",
                                     "tags": None}]

    async def test_notify_invalid_priority_falls_back(self, hub, log_channel):
        assert await NotifyAction("log", "m", priority="extreme").execute(hub) is True
        assert log_channel.sent[0]["priority"] == "default" and log_channel.sent[0]["title"] is None

    async def test_notify_without_notifier(self, hub):
        assert hub.notifier is None
        assert await NotifyAction("log", "m").execute(hub) is False

    async def test_notify_unknown_channel(self, hub, log_channel):
        assert await NotifyAction("nope", "m").execute(hub) is False
        assert log_channel.sent == []

    async def test_notify_with_delay(self, hub, log_channel):
        assert await NotifyAction("log", "m", delay=0.01).execute(hub) is True
        assert len(log_channel.sent) == 1


class TestRulesWithNotify:
    def test_rule_from_dict_parses_notify_in_then_and_else(self):
        rule = rule_from_dict({"name": "frost", "when": {"device": "t", "property": "value", "op": "<", "value": 2},
                               "then": [{"device": "h", "command": "on"},
                                        {"notify": "phone", "message": "cold"}],
                               "else": {"notify": "phone", "message": "warm"}})
        assert isinstance(rule.actions[1], NotifyAction)
        assert isinstance(rule.else_actions[0], NotifyAction) and rule.else_actions[0].message == "warm"

    def test_from_config_ignores_notify_for_device_check(self, hub, home):
        engine = RuleEngine.from_config(hub, [{"name": "n", "when": {"device": "hall_temp", "property": "value",
                                                                     "op": ">", "value": 0},
                                               "then": {"notify": "anything", "message": "x"}}])
        listed = engine.list_rules()[0]
        assert listed["actions"] == [{"notify": "anything", "message": "x"}]

    def test_from_config_still_checks_device_actions(self, hub, home):
        with pytest.raises(ConfigurationError, match="unknown device 'ghost'"):
            RuleEngine.from_config(hub, [{"name": "n", "when": {"device": "hall_temp", "property": "value"},
                                          "else": {"device": "ghost", "command": "on"},
                                          "then": {"notify": "x", "message": "m"}}])

    async def test_notify_actions_get_rule_context(self, hub, home, log_channel):
        temp, _ = home
        engine = RuleEngine.from_config(hub, [{
            "name": "hot", "when": {"device": "hall_temp", "property": "value", "op": ">", "value": 25},
            "then": {"notify": "log", "message": "{rule}: {hall_temp.value}"},
            "else": {"notify": "log", "message": "{rule} cleared", "title": "OK"},
        }])
        await engine.evaluate()  # False on first evaluation -> else
        assert log_channel.sent[-1]["message"] == "hot cleared" and log_channel.sent[-1]["title"] == "OK"
        temp.protocol.set_state({"value": 30.04})
        temp.read_state(fresh=True)
        await engine.evaluate()
        assert log_channel.sent[-1]["message"] == "hot: 30.0"
        await engine.evaluate()  # edge mode: no repeat
        assert len(log_channel.sent) == 2
