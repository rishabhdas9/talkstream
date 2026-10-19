from __future__ import annotations

import json
import logging
from typing import Any

auth_logger = logging.getLogger("talkstream.auth")


def log_auth_rejection(
    *, surface: str, reason: str, path: str | None = None, **fields: Any
) -> None:
    payload = {"event": "auth_rejected", "surface": surface}
    if path is not None:
        payload["path"] = path
    payload.update({"reason": reason, **fields})
    auth_logger.warning(json.dumps(payload, separators=(",", ":"), default=str))
