"""Logging with secret redaction.

Every log line passes through a redaction filter. Passwords fetched from the
credential store are registered as secrets and replaced with *** before
anything reaches the log file or the console, so an accidental log statement
can never leak a credential.
"""

from __future__ import annotations

import logging
import logging.handlers
import pathlib


class RedactingFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()

    def register(self, secret: str) -> None:
        if secret:
            self._secrets.add(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for secret in self._secrets:
            if secret in msg:
                record.msg = record.msg.replace(secret, "***")
                record.args = None
                return True
        return True


def setup_logging(data_dir: pathlib.Path) -> logging.Logger:
    """Create the mailbrief logger with file + console handlers."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mailbrief")
    logger.setLevel(logging.INFO)
    logger.addFilter(RedactingFilter())
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "mailbrief.log",
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    logger.addHandler(console)
    return logger


def register_secret(logger: logging.Logger, secret: str) -> None:
    """Register a secret value so no future log line can print it."""
    for flt in logger.filters:
        if isinstance(flt, RedactingFilter):
            flt.register(secret)
            return
