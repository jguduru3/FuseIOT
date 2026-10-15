"""Logging configuration."""
import builtins
import io
import sys

import pytest

from fuseiot import logging_config
from fuseiot.logging_config import configure_logging


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def no_colorama(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "colorama":
            raise ImportError("no colorama")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_windows_tty_without_colorama_does_not_crash(monkeypatch, no_colorama):
    # On Windows, structlog refuses colours without colorama; NUL and consoles report isatty() == True.
    monkeypatch.setattr(sys, "stderr", _TTY())
    monkeypatch.setattr(sys, "platform", "win32")
    assert logging_config._use_colors() is False
    configure_logging(level="WARNING", console=False)


def test_colors_only_on_tty(monkeypatch):
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert logging_config._use_colors() is False


def test_invalid_level_rejected():
    with pytest.raises(ValueError):
        configure_logging(level="LOUD")
