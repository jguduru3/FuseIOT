"""
Structured logging (structlog) for FuseIoT.

As a library FuseIoT is quiet by default: only warnings and errors are written
to stderr until the application calls :func:`configure_logging`.
"""
import logging
import logging.config
import sys
from typing import Any, List, Optional

import structlog

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_configured_by_user = False


def _install_default() -> None:
    if structlog.is_configured():
        return
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=False,
    )


def _use_colors() -> bool:
    """Colour only on a real terminal; structlog needs colorama for colours on Windows."""
    try:
        if not sys.stderr.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if sys.platform == "win32":
        try:
            import colorama  # noqa: F401
        except ImportError:
            return False
    return True


def configure_logging(
    level: str = "INFO",
    json_format: bool = False,
    log_file: Optional[str] = None,
    console: bool = True,
) -> None:
    """
    Configure structured logging.

    Args:
        level: DEBUG, INFO, WARNING, ERROR or CRITICAL.
        json_format: Emit one JSON object per line (recommended in production).
        log_file: Also write logs to this file.
        console: Write logs to stderr.
    """
    global _configured_by_user
    level = str(level).upper()
    if level not in _LEVELS:
        raise ValueError(f"Invalid log level: {level}")

    pre_chain: List[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_format
        else structlog.dev.ConsoleRenderer(colors=_use_colors())
    )

    handlers = {}
    if console:
        handlers["console"] = {"class": "logging.StreamHandler", "formatter": "main",
                               "stream": "ext://sys.stderr"}
    if log_file:
        handlers["file"] = {"class": "logging.FileHandler", "formatter": "main",
                            "filename": log_file, "encoding": "utf-8"}
    if not handlers:
        handlers["null"] = {"class": "logging.NullHandler"}

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "main": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processors": [
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    renderer,
                ],
                "foreign_pre_chain": pre_chain,
            }
        },
        "handlers": handlers,
        "loggers": {"fuseiot": {"handlers": list(handlers), "level": level, "propagate": False}},
    })

    structlog.configure(
        processors=[*pre_chain, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _configured_by_user = True


def get_logger(name: Optional[str] = None) -> Any:
    """Get a structured logger named ``fuseiot.<name>``."""
    return structlog.get_logger(f"fuseiot.{name}" if name else "fuseiot")


_install_default()
