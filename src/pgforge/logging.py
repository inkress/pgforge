"""Structured logging for pgforge.

Uses ``rich`` for human-readable output and a simple JSON formatter when
``PGFORGE_LOG_JSON=1`` is set. Verbosity is controlled by ``--verbose`` /
``--quiet`` CLI flags via :func:`configure`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

# Two consoles: stderr for log/progress, stdout for command results (so a user
# can ``pgforge ls --json | jq`` and not have logs pollute the pipe).
err_console = Console(stderr=True)
out_console = Console()

_DEFAULT_FORMAT = "%(message)s"


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                payload[key[4:]] = value
        return json.dumps(payload, default=str)


def configure(verbose: int = 0, quiet: bool = False) -> None:
    """Wire up the root logger. Idempotent — safe to call from CLI entry point."""
    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    if os.environ.get("PGFORGE_LOG_JSON") == "1":
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JSONFormatter())
    else:
        handler = RichHandler(
            console=err_console,
            show_time=verbose >= 1,
            show_path=verbose >= 2,
            rich_tracebacks=True,
            markup=True,
        )
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger. Use ``pgforge.<module>`` names by convention."""
    return logging.getLogger(name)
