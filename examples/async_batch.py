"""
Concurrent control: per-device ``*_async`` methods and ``Hub.batch``.

    python examples/async_batch.py
"""
import asyncio

from fuseiot import Hub, Switchable, VirtualDevice


async def main() -> None:
    async with Hub() as hub:
        # Swap VirtualDevice(...) for HTTP("http://192.168.1.4x/api") with real lights.
        lights = [
            hub.add_device(VirtualDevice("switchable", latency_ms=40, settle_ms=80),
                           Switchable, device_id=f"light_{i:02d}")
            for i in range(1, 9)
        ]

        # Every command method has an awaitable twin: on() -> on_async()
        results = await asyncio.gather(*(light.on_async() for light in lights))
        confirmed = sum(r.confirmed for r in results)
        print(f"on_async: {confirmed}/{len(lights)} confirmed")

        # Batch with bounded concurrency and an aggregated result
        batch = await hub.batch([(light, "off", {"confirm": True}) for light in lights],
                                max_concurrent=4)
        print(f"batch off: {batch}  (success rate {batch.success_rate:.0f}%, "
              f"{batch.total_latency_ms:.0f} ms total)")
        for failure in batch.get_failures():
            print(f"  failed: {failure.device_id}: {failure.error}")


if __name__ == "__main__":
    asyncio.run(main())
