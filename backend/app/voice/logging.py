from __future__ import annotations

import json
import logging
from typing import Any

voice_logger = logging.getLogger("talkstream.voice")


def log_voice_event(
    level: int,
    event: str,
    *,
    session_id: str,
    turn_id: str | None,
    **fields: Any,
) -> None:
    payload = {
        "event": event,
        "session_id": session_id,
        "turn_id": turn_id,
        **{key: value for key, value in fields.items() if key != "error"},
    }
    voice_logger.log(level, json.dumps(payload, separators=(",", ":"), default=str))
