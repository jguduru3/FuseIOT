"""
Conveyor control with resilience: circuit breaker, rate limiting, emergency stop.

    python examples/industrial_control.py
"""
import time

from fuseiot import (
    CapabilityConfig,
    CircuitBreaker,
    Hub,
    Motor,
    RateLimiter,
    Sensor,
    Switchable,
    VirtualDevice,
)


class ConveyorSystem:
    def __init__(self) -> None:
        self.hub = Hub()
        strict = CapabilityConfig(
            timeout=3.0,
            retry_attempts=2,
            circuit_breaker=CircuitBreaker(failure_threshold=3, recovery_timeout=10),
            rate_limiter=RateLimiter(requests=20, window=1.0),
        )
        # Replace VirtualDevice(...) with Serial("/dev/ttyUSB0") / HTTP(...) on the line.
        self.motor = self.hub.add_device(VirtualDevice("motor", latency_ms=20, settle_ms=200), Motor,
                                         device_id="conveyor", config=strict,
                                         position_range=(0, 1000), speed_range=(0, 100))
        self.load = self.hub.add_device(VirtualDevice("sensor", initial_state={"value": 12.0}), Sensor,
                                        device_id="belt_load", unit="kg")
        self.e_stop = self.hub.add_device(VirtualDevice("switchable"), Switchable, device_id="e_stop_relay")

    def move(self, position: float, speed: float) -> bool:
        if self.load.read(fresh=True)["value"] > 50:
            print("Load too high, refusing to move")
            return False
        result = self.motor.move_to(position, speed=speed)
        print(f"move_to({position}) -> {result.status.name} in {result.latency_ms:.0f} ms")
        return result.confirmed

    def emergency_stop(self) -> None:
        stop = self.motor.stop(timeout=1.0)
        relay = self.e_stop.on()
        print(f"EMERGENCY STOP: motor {stop.status.name}, relay {relay.status.name}")

    def run(self) -> None:
        self.motor.home().raise_for_error()
        for target in (250, 500, 750):
            if not self.move(target, speed=60):
                self.emergency_stop()
                break
            time.sleep(0.1)

        print("Simulating a failing drive controller...")
        self.motor.protocol.sim.failure_rate = 1.0
        for _ in range(4):
            r = self.motor.move_to(100)
            print(f"  {r.status.name}: {r.error}")
        print(f"breaker: {self.motor.config.circuit_breaker.get_stats()}")
        self.hub.close()


if __name__ == "__main__":
    ConveyorSystem().run()
