# Contributing

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
ruff check fuseiot examples
pytest                         # full suite; needs no hardware or network
pytest -m "not slow"           # skips the example scripts
```

- Keep Python 3.9 compatibility: no `X | Y` unions and no `match` statements.
- Device commands go through `Capability._send_and_confirm`. Don't bypass
  confirmation, and add any new public command to the class's `commands` tuple
  so the API and CLI can reach it.
- New protocols subclass `Protocol`. `connect()` returns a bool and never
  raises for network errors; `send()` raises `ProtocolError`.
- Test against `VirtualDevice` or `SimulatedHTTPDevice` rather than mocks
  where you can.
- Add an entry to `CHANGELOG.md` under *Unreleased*.
