from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

import httpx
import pytest
from test_paid_boundaries import paid_boundary as paid_boundary
from test_session_policy import ALLOW, SynthesisStopped
from test_session_policy import session_policy as session_policy

from app.costs.models import BudgetExceeded
from app.voice import session as voice_session
from app.voice.logging import log_voice_event
from app.voice.protocol import ProbeOptions, ProtocolError, sanitize_vendor_message
from app.voice.tools import (
    ClientToolTimeoutError,
    ToolExecutionError,
    execute_measured,
    reset_tool_span_observer,
    set_tool_span_observer,
)
from app.voice.vad import VadInitializationError

PRIVATE = "private-transcript-credential-classifier-rationale"


@pytest.fixture(autouse=True)
def capture_voice_logging(monkeypatch):
    monkeypatch.setattr(logging.getLogger("talkstream"), "propagate", True)


@pytest.mark.parametrize("kind", ["data", "audio", "error", PRIVATE, [PRIVATE]])
def test_vendor_probe_does_not_echo_payload_keys_values_or_unknown_types(kind):
    message = {"type": kind, PRIVATE: PRIVATE, "data": {"audio": "short", "text": PRIVATE}}
    sanitized = sanitize_vendor_message(message)
    assert sanitized == {
        "type": kind if kind in ("data", "audio", "error") else "unknown",
        "field_count": 3,
    }
    assert PRIVATE not in json.dumps(sanitized)
    with pytest.raises(ProtocolError):
        sanitize_vendor_message([PRIVATE])


def test_voice_logging_drops_error_details_but_keeps_failure_metadata(caplog):
    with caplog.at_level(logging.WARNING, logger="talkstream.voice"):
        log_voice_event(
            logging.WARNING,
            "session_failed",
            session_id="session",
            turn_id="turn",
            error=PRIVATE,
            error_type="RuntimeError",
            outcome="unknown",
            recovered=True,
        )
    payload = json.loads(caplog.records[-1].message)
    assert PRIVATE not in caplog.text
    assert payload["error_type"] == "RuntimeError" and payload["outcome"] == "unknown"
    assert payload["recovered"] is True


@pytest.mark.parametrize(
    "mode,outcome",
    [
        ("reported", "error"),
        ("known", "error"),
        ("timeout", "unknown"),
        ("http", "unknown"),
        ("unexpected", "unknown"),
    ],
)
async def test_measured_errors_hide_details_and_preserve_outcomes(mode, outcome):
    spans = []

    async def operation():
        if mode == "reported":
            return {"ok": False, "error": PRIVATE}
        raise {
            "known": ToolExecutionError,
            "timeout": ClientToolTimeoutError,
            "http": httpx.HTTPError,
            "unexpected": RuntimeError,
        }[mode](PRIVATE)

    async def observe(span):
        spans.append(span)

    token = set_tool_span_observer(observe)
    try:
        result = json.loads(
            await execute_measured(name="todo_add", call_id="call", operation=operation)
        )
    finally:
        reset_tool_span_observer(token)
    assert PRIVATE not in json.dumps([result, spans])
    assert result["ok"] is False and isinstance(result["error"], str)
    assert spans[0]["outcome"] == outcome and spans[0]["duration_ms"] >= 0
    if outcome == "unknown":
        assert result["state"] == "unknown"


@pytest.mark.parametrize("error", [BudgetExceeded(PRIVATE), asyncio.CancelledError(PRIVATE)])
async def test_measured_control_exceptions_still_propagate_without_private_spans(error):
    spans = []

    async def operation():
        raise error

    async def observe(span):
        spans.append(span)

    token = set_tool_span_observer(observe)
    try:
        with pytest.raises(type(error)):
            await execute_measured(name="todo_add", call_id="call", operation=operation)
    finally:
        reset_tool_span_observer(token)
    assert PRIVATE not in json.dumps(spans)


@pytest.mark.parametrize(
    "duration,confidence,expected",
    [
        (1.25, 0.8, {"audio_duration": 1.25}),
        (0, 0, {"audio_duration": 0}),
        (True, True, None),
        (-1, -0.1, None),
        (float("nan"), float("nan"), None),
        (float("inf"), float("inf"), None),
        ("1", "0.8", None),
        (121, 2, None),
        (10**400, 10**400, None),
    ],
)
async def test_stt_owned_metrics_keep_only_safe_numeric_metadata(
    session_policy, monkeypatch, caplog, duration, confidence, expected
):
    r = session_policy([ALLOW])
    r.voice._probe = ProbeOptions(enabled=True)

    async def messages():
        yield {
            "type": "data",
            "data": {
                "transcript": PRIVATE,
                "language_code": PRIVATE,
                "language_probability": confidence,
                "request_id": PRIVATE,
                "metrics": {"audio_duration": duration, PRIVATE: PRIVATE, "latency": PRIVATE},
            },
        }

    monkeypatch.setattr(r.voice._stt.client, "messages", messages)
    with caplog.at_level(logging.DEBUG, logger="talkstream.voice"):
        timer = await r.voice._receive_stt_final()
    assert timer.raw_provider_metrics == expected
    assert timer.dimensions["language_code"] is None
    assert timer.dimensions["language_confidence"] == (
        confidence if type(confidence) in (int, float) and 0 <= confidence <= 1 else None
    )
    assert timer.dimensions["stt_request_id"] == hashlib.sha256(PRIVATE.encode()).hexdigest()
    assert PRIVATE not in json.dumps([timer.raw_provider_metrics, timer.dimensions, r.frames])
    assert PRIVATE not in caplog.text
    assert (await r.voice._response_queue.get())[1] == PRIVATE


async def test_vad_fallback_logs_fixed_reason(session_policy, monkeypatch, caplog):
    r = session_policy([ALLOW])
    r.voice._settings.vad_provider = "silero"

    def unavailable(path):
        raise VadInitializationError(PRIVATE)

    monkeypatch.setattr("app.voice.gate.SileroOnnxVad", unavailable)
    with caplog.at_level(logging.WARNING, logger="talkstream.voice"):
        gate = r.voice._build_gate()
    assert gate.vad_name == "energy"
    assert PRIVATE not in caplog.text


async def test_tts_observation_hides_provider_text_and_hashes_request_id(
    session_policy, monkeypatch, caplog
):
    r = session_policy([ALLOW, "Hello there.", ALLOW])
    r.voice._probe = ProbeOptions(enabled=True)

    class Synthesis:
        connection_id = "local-connection"

        def __init__(self, *args, **kwargs):
            pass

        async def synthesize(self, chunks, *, observer, **kwargs):
            async for _ in chunks:
                pass
            await observer("t_tts_text_submitted", time.monotonic(), {})
            await observer(
                "t_tts_first_chunk",
                time.monotonic(),
                {
                    "request_id": PRIVATE,
                    "content_type": PRIVATE,
                    "connection_id": self.connection_id,
                    PRIVATE: PRIVATE,
                },
            )
            if False:
                yield b""
            raise SynthesisStopped()

    monkeypatch.setitem(voice_session.SYNTHESIS, "sarvam", Synthesis)
    with caplog.at_level(logging.DEBUG, logger="talkstream.voice"), pytest.raises(SynthesisStopped):
        await r.voice._process_turn(timer=r.timer, transcript="Hello")
    assert r.timer.dimensions["tts_request_id"] == hashlib.sha256(PRIVATE.encode()).hexdigest()
    assert PRIVATE not in caplog.text
    assert PRIVATE not in json.dumps(r.frames)
