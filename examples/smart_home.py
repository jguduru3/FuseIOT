"""
Smart-home scenes, events and an automation rule (simulated devices).

    python examples/smart_home.py
"""
import asyncio

from fuseiot import (
    Action,
    Color,
    Condition,
    Dimmable,
    Hub,
    RGBLight,
    RuleEngine,
    Sensor,
    Switchable,
    Thermostat,
    ThermostatMode,
    VirtualDevice,
)


class SmartHome:
    def __init__(self) -> None:
        self.hub = Hub()
        add = self.hub.add_device
        # Replace VirtualDevice(...) with HTTP("http://<device-ip>/api") for real hardware.
        self.living = add(VirtualDevice("rgb_light"), RGBLight, device_id="living_room_light")
        self.bedroom = add(VirtualDevice("dimmable"), Dimmable, device_id="bedroom_light")
        self.porch = add(VirtualDevice("switchable"), Switchable, device_id="porch_light")
        self.thermostat = add(VirtualDevice("thermostat"), Thermostat, device_id="thermostat")
        self.indoor = add(VirtualDevice("sensor", initial_state={"value": 21.0}), Sensor,
                          device_id="indoor_temp", unit="celsius")

        self.hub.on_state_change(
            lambda e: print(f"  event {e.device_id}: {sorted(e.changed_keys)} -> {e.new_state}"),
            pattern="*_light",
        )

    async def evening(self) -> None:
        print("Evening scene")
        results = await asyncio.gather(
            self.living.on_async(color=Color(255, 147, 41), brightness=60),
            self.bedroom.on_async(brightness=40),
            self.porch.on_async(),
            self.thermostat.set_temperature_async(22.0),
        )
        await self.thermostat.set_mode_async(ThermostatMode.AUTO)
        print(f"  {sum(r.confirmed for r in results)}/{len(results)} commands confirmed")

    async def night(self) -> None:
        print("Night scene")
        batch = await self.hub.batch([
            (self.living, "off", {}),
            (self.bedroom, "off", {}),
            (self.porch, "off", {}),
            (self.thermostat, "set_temperature", {"temperature": 18.0}),
        ])
        print(f"  {batch}")

    async def run(self) -> None:
        rules = RuleEngine(self.hub)
        rules.add_simple(
            "cold_house",
            Condition("indoor_temp", "value", "<", 19.0),
            then_actions=[Action("thermostat", "set_mode", {"mode": "heat"})],
        )
        await self.evening()

        print("Temperature drops to 17.5 C ...")
        self.indoor.protocol.set_state({"value": 17.5})
        self.indoor.read(fresh=True)
        await rules.evaluate()
        print(f"  thermostat mode is now {self.thermostat.read_state(fresh=True)['mode']!r}")

        await self.night()
        self.hub.close()


if __name__ == "__main__":
    asyncio.run(SmartHome().run())
