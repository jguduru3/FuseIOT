"""
Background device monitor.

Polls every device on an interval so that:

* changes made by hand (a wall switch, a button on the ESP32) reach the cache,
  the dashboard (SSE) and the rules within one interval;
* a device that stops answering is marked offline after ``offline_after``
  consecutive failures, and an alert goes out (``device_offline``); when it
  answers again, ``device_online`` is sent.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..logging_config import get_logger

logger = get_logger("monitoring.devices")


@dataclass
class DeviceStatus:
    online: Optional[bool] = None   # None until the first poll
    failures: int = 0
    last_seen: Optional[float] = None
    last_error: Optional[str] = None
    changed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"online": self.online, "failures": self.failures, "last_seen": self.last_seen,
                "last_error": self.last_error, "changed_at": self.changed_at}


class DeviceMonitor:
    def __init__(self, hub: Any, interval: float = 30.0, offline_after: int = 2,
                 notifier: Any = None, max_workers: int = 8):
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self.hub = hub
        self.interval = interval
        self.offline_after = max(1, int(offline_after))
        self.notifier = notifier
        self.status: Dict[str, DeviceStatus] = {}
        #: Callbacks ``(device_id, status_dict)`` invoked when a device goes online/offline.
        self.listeners: List[Callable[[str, Dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fuseiot-poll")

    def _poll_one(self, device: Any) -> None:
        try:
            device.read_state(fresh=True)
            error = None
        except Exception as e:
            error = getattr(e, "message", None) or str(e)
        now = time.time()
        with self._lock:
            st = self.status.setdefault(device.id, DeviceStatus())
            was_online = st.online
            if error is None:
                st.failures = 0
                st.last_seen = now
                st.last_error = None
                st.online = True
            else:
                st.failures += 1
                st.last_error = error
                if st.failures >= self.offline_after:
                    st.online = False
            changed = st.online is not None and st.online != was_online
            if changed:
                st.changed_at = now
            online, last_error = st.online, st.last_error
        if not changed:
            return
        snapshot = self.get(device.id)
        for listener in list(self.listeners):
            try:
                listener(device.id, snapshot.to_dict() if snapshot else {})
            except Exception as e:
                logger.warning("monitor_listener_error", error=str(e))
        name = getattr(device.config, "name", None) or device.id
        if online:
            logger.info("device_online", device=device.id)
            if was_online is False and self.notifier is not None:
                self.notifier.alert("device_online", title=f"{name} is back online",
                                    message=f"{name} ({device.id}) is responding again.", key=device.id)
        else:
            logger.warning("device_offline", device=device.id, error=last_error)
            if self.notifier is not None:
                self.notifier.alert("device_offline", title=f"{name} is offline",
                                    message=f"{name} ({device.id}) stopped responding: {last_error}",
                                    key=device.id)

    def poll_all(self) -> None:
        """Poll every device once (in parallel) and wait for the results."""
        devices = self.hub.devices()
        list(self._pool.map(self._poll_one, devices))
        known = {d.id for d in devices}
        with self._lock:
            for device_id in list(self.status):
                if device_id not in known:
                    del self.status[device_id]

    def get(self, device_id: str) -> Optional[DeviceStatus]:
        with self._lock:
            return self.status.get(device_id)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: v.to_dict() for k, v in self.status.items()}

    def offline_devices(self) -> List[str]:
        with self._lock:
            return [k for k, v in self.status.items() if v.online is False]

    def _loop(self) -> None:
        logger.info("device_monitor_started", interval=self.interval)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_all()
            except Exception as e:  # keep monitoring alive
                logger.error("device_monitor_error", error=str(e))
            self._stop.wait(max(0.0, self.interval - (time.monotonic() - started)))

    def start_background(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="fuseiot-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        self._pool.shutdown(wait=False)
