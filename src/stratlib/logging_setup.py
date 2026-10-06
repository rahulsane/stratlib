"""Console and file logging, with secrets scrubbed from every record."""

from __future__ import annotations

import logging
import logging.handlers
from collections.abc import Iterable
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RedactSecrets(logging.Filter):
    """Replace secret values in log output with a placeholder.

    The FMP key travels in a request header and is never logged on purpose;
    this is a second line of defence against a stray message or traceback.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self.secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            for secret in self.secrets:
                record.exc_text = record.exc_text.replace(secret, "[REDACTED]")
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def configure_logging(log_dir: Path, *, verbose: bool = False, secrets: Iterable[str] = ()) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    redact = RedactSecrets(secrets)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    console.addFilter(redact)
    root.addHandler(console)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "stratlib.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    file_handler.addFilter(redact)
    root.addHandler(file_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
