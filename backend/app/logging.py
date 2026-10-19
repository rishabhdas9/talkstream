from __future__ import annotations

import logging
import re
from typing import Any

TOKEN_QUERY_PATTERN = re.compile(r"([?&](?:token|ticket)=)[^&\s]+", re.IGNORECASE)


class RedactTokenQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_value(value) for key, value in record.args.items()}
        return True


def install_token_log_redaction() -> None:
    for logger_name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, RedactTokenQueryFilter) for item in logger.filters):
            logger.addFilter(RedactTokenQueryFilter())


def configure_application_logging(level: str) -> None:
    application_logger = logging.getLogger("talkstream")
    application_logger.setLevel(level.upper())
    if not application_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        application_logger.addHandler(handler)
    application_logger.propagate = False


def _redact_value(value: Any) -> Any:
    return TOKEN_QUERY_PATTERN.sub(r"\1<redacted>", value) if isinstance(value, str) else value
