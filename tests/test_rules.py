"""Edge automation: conditions, actions, rules and the rule engine."""
import asyncio
import time

import pytest

from fuseiot import Sensor, Switchable, Thermostat
from fuseiot.edge.rules import Action, Condition, Operator, Rule, RuleEngine, rule_from_dict
from fuseiot.exceptions import ConfigurationError


@pytest.fixture
def home(add_virtual):
    """Hub with a temperature sensor, a heater switch and a thermostat."""
    temp = add_virtual("sensor", Sensor, "temp", initial_state={"value": 15.0})
    heater = add_virtual("switchable", Switchable, "heater")
    thermo = add_virtual("thermostat", Thermostat, "thermo")
    return temp, heater, thermo


def set_temp(temp, value):
    temp.protocol.set_state({"value": value})
    temp.read_state(fresh=True)


class TestCondition:
    @pytest.mark.parametrize("op,value,expected", [
        ("==", 15.0, True), ("eq", 15.0, True), ("=", 15.0, True), ("!=", 15.0, False),
        ("<", 16, True), ("lt", 15, False), (">", 14, True), ("gt", 15, False),
        ("<=", 15, True), ("le", 14, False), (">=", 15, True), ("ge", 16, False),
    ])
    def test_operators(self, hub, home, op, value, expected):
        assert Condition("temp", "value", op, value).evaluate(hub) is expected

    def test_contains(self, hub, home):
        assert Condition("temp", "unit", "contains", "cel").evaluate(hub) is True
        assert Condition("temp", "unit", Operator.CONTAINS, "fahr").evaluate(hub) is False

    def test_invalid_operator(self):
        with pytest.raises(ValueError):
            Condition("temp", "value", "~=", 1)

    def test_property_attribute_fallback(self, hub, home):
        assert Condition("heater", "is_on", "==", False).evaluate(hub) is True

    def test_unavailable_values_evaluate_false(self, hub, home):
        assert Condition("temp", "no_such_property", "==", None).evaluate(hub) is False
        assert Condition("temp", "calibrate", "==", None).evaluate(hub) is False  # method
        assert Condition("ghost", "value", "==", 1).evaluate(hub) is False
        assert Condition("temp", "value", "<", "a string").evaluate(hub) is False  # TypeError

    def test_changed(self, hub, home):
        temp = home[0]
        c = Condition("temp", "value", "changed")
        assert c.evaluate(hub) is False  # first observation
        assert c.evaluate(hub) is False
        set_temp(temp, 16.0)
        assert c.evaluate(hub) is True
        assert c.evaluate(hub) is False


class TestAction:
    async def test_execute(self, hub, home):
        heater = home[1]
        assert await Action("heater", "on").execute(hub) is True
        assert heater.protocol.state["power"] is True

    async def test_params_and_delay(self, hub, home):
        start = time.monotonic()
        assert await Action("thermo", "set_mode", {"mode": "cool"}, delay=0.05).execute(hub) is True
        assert time.monotonic() - start >= 0.05
        assert home[2].protocol.state["mode"] == "cool"

    async def test_disallowed_or_failing_actions(self, hub, home, add_virtual):
        assert await Action("heater", "read_state").execute(hub) is False
        assert await Action("ghost", "on").execute(hub) is False
        assert await Action("thermo", "set_mode", {"mode": "warp"}).execute(hub) is False
        add_virtual("switchable", Switchable, "dead", failure_rate=1.0)
        assert await Action("dead", "on").execute(hub) is False


class TestRuleParsing:
    def test_full_rule(self):
        rule = rule_from_dict({
            "name": "frost", "mode": "level", "cooldown": "60", "enabled": False,
            "when": [{"device": "temp", "property": "value", "op": "<", "value": 2}],
            "then": [{"device": "heater", "command": "on", "delay": 1}],
            "else": [{"device": "heater", "command": "off", "params": {"confirm": False}}],
        })
        assert rule.mode == "level" and rule.cooldown == 60.0 and rule.enabled is False
        assert rule.conditions[0].operator is Operator.LT
        assert rule.actions[0].delay == 1.0
        assert rule.else_actions[0].params == {"confirm": False}

    def test_single_condition_mapping(self):
        rule = rule_from_dict({"name": "r", "when": {"device": "a", "property": "power"},
                               "then": [{"device": "a", "command": "off"}]})
        assert len(rule.conditions) == 1 and rule.conditions[0].operator is Operator.EQ
        assert rule.else_actions is None and rule.mode == "edge"

    @pytest.mark.parametrize("spec,fragment", [
        ({"when": [], "then": []}, "name"),
        ({"name": "r", "then": []}, "when"),
        ({"name": "r", "when": []}, "then"),
        ({"name": "r", "when": [{"device": "a"}], "then": []}, "Invalid condition"),
        ({"name": "r", "when": [{"device": "a", "property": "p", "op": "~"}], "then": []}, "Invalid condition"),
        ({"name": "r", "when": [], "then": [{"device": "a"}]}, "needs 'device' and 'command'"),
        ({"name": "r", "when": [], "then": [], "mode": "sometimes"}, "mode"),
    ])
    def test_invalid(self, spec, fragment):
        with pytest.raises(ConfigurationError, match=fragment):
            rule_from_dict(spec)

    def test_single_action_mapping(self):
        try:
            rule = rule_from_dict({"name": "r", "when": {"device": "a", "property": "power"},
                                   "then": {"device": "a", "command": "off"}})
        except ConfigurationError:
            return
        assert len(rule.actions) == 1


class TestRuleEngine:
    async def test_edge_mode_fires_on_transitions(self, hub, home):
        temp, heater, _ = home
        engine = RuleEngine(hub)
        rule = engine.add_simple("heat", Condition("temp", "value", "<", 18),
                                 [Action("heater", "on")], else_actions=[Action("heater", "off")])
        await engine.evaluate()
        assert heater.protocol.state["power"] is True and rule.trigger_count == 1
        heater.protocol.set_state({"power": False})  # someone switches it off manually
        await engine.evaluate()
        assert heater.protocol.state["power"] is False, "edge rules do not re-fire while true"
        set_temp(temp, 21.0)
        await engine.evaluate()
        assert rule.trigger_count == 2 and rule.last_outcome is False
        heater_off_posts = [r for r in heater.protocol.requests if r.get("_path") == "/off"]
        assert len(heater_off_posts) == 1

    async def test_level_mode_and_cooldown(self, hub, home):
        heater = home[1]
        engine = RuleEngine(hub)
        level = engine.add_simple("lvl", Condition("temp", "value", "<", 18), [Action("heater", "on")],
                                  mode="level")
        await engine.evaluate()
        await engine.evaluate()
        assert level.trigger_count == 2
        level.cooldown = 60
        await engine.evaluate()
        assert level.trigger_count == 2, "cooldown suppresses re-triggering"
        assert heater.protocol.state["power"] is True

    async def test_no_else_actions_means_no_trigger(self, hub, home):
        engine = RuleEngine(hub)
        rule = engine.add_simple("hot", Condition("temp", "value", ">", 30), [Action("heater", "off")])
        await engine.evaluate()
        assert rule.trigger_count == 0 and rule.last_triggered is None and rule.last_outcome is False

    async def test_enable_disable(self, hub, home):
        engine = RuleEngine(hub)
        rule = engine.add_simple("heat", Condition("temp", "value", "<", 18), [Action("heater", "on")])
        assert engine.set_enabled("heat", False) is True
        await engine.evaluate()
        assert rule.trigger_count == 0
        engine.set_enabled("heat", True)
        await engine.evaluate()
        assert rule.trigger_count == 1
        assert engine.set_enabled("ghost", True) is False

    def test_add_remove_list(self, hub, home):
        engine = RuleEngine(hub)
        engine.add(Rule("a", [Condition("temp", "value", ">", 1)], [Action("heater", "on", {"confirm": True})]))
        with pytest.raises(ConfigurationError, match="Duplicate"):
            engine.add(Rule("a", [], []))
        listed = engine.list_rules()
        assert listed[0]["conditions"] == [{"device": "temp", "property": "value", "op": ">", "value": 1}]
        assert listed[0]["actions"] == [{"device": "heater", "command": "on", "params": {"confirm": True}}]
        assert engine.remove("a") is True and engine.remove("a") is False

    def test_from_config(self, hub, home):
        specs = [{"name": "r", "when": {"device": "temp", "property": "value", "op": "<", "value": 18},
                  "then": [{"device": "heater", "command": "on"}],
                  "else": [{"device": "thermo", "command": "fan_off"}]}]
        engine = RuleEngine.from_config(hub, specs)
        assert [r.name for r in engine.rules] == ["r"]
        bad = [{"name": "r", "when": {"device": "temp", "property": "value"},
                "then": [{"device": "ghost", "command": "on"}]}]
        with pytest.raises(ConfigurationError, match="unknown device 'ghost'"):
            RuleEngine.from_config(hub, bad)

    def test_background_thread(self, hub, home):
        heater = home[1]
        engine = RuleEngine(hub)
        engine.add_simple("heat", Condition("temp", "value", "<", 18), [Action("heater", "on")])
        engine.start_background(interval=0.02)
        try:
            deadline = time.monotonic() + 3
            while not heater.protocol.state["power"] and time.monotonic() < deadline:
                time.sleep(0.01)
            assert heater.protocol.state["power"] is True
            assert engine.running
        finally:
            engine.stop()
        assert not engine._thread.is_alive() and not engine.running

    async def test_start_in_running_loop_survives_errors(self, hub, home):
        engine = RuleEngine(hub)
        calls = []

        async def flaky_evaluate():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")

        engine.evaluate = flaky_evaluate  # type: ignore[assignment]
        engine.start(interval=0.01)
        for _ in range(100):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        engine.stop()
        assert len(calls) >= 3
        await asyncio.sleep(0.02)
        assert engine._task.done()

    def test_stop_is_prompt(self, hub, home):
        engine = RuleEngine(hub)
        engine.start_background(interval=1.5)
        time.sleep(0.1)
        start = time.monotonic()
        engine.stop()
        assert time.monotonic() - start < 0.5
