from __future__ import annotations

import pytest

from app.voice.metrics import TurnTimer, percentile


def complete_timer() -> TurnTimer:
    timer = TurnTimer(turn_id="turn-1", turn_index=1, endpointing_strategy="local_vad")
    timer.set_speech_anchor(capture_seq=10, capture_time_ms=1000.0)
    server = {
        "t_audio_frame_server_receive": 1.0,
        "t_speech_start_server": 1.1,
        "t_last_speech_frame_server": 2.0,
        "t_endpoint_decision": 2.5,
        "t_stt_flush_sent": 2.51,
        "t_stt_final": 2.81,
        "t_llm_request_start": 2.82,
        "t_llm_first_visible_token": 3.02,
        "t_llm_complete": 3.42,
        "t_llm_first_speakable_chunk": 3.12,
        "t_tts_connection_acquire_start": 3.0,
        "t_tts_connection_acquire_end": 3.05,
        "t_tts_text_submitted": 3.13,
        "t_tts_first_chunk": 3.38,
        "t_tts_complete": 3.8,
        "t_audio_sent_server": 3.39,
    }
    for name, value in server.items():
        timer.mark_server(name, value)
    client = {
        "t_client_audio_received_ms": 1550.0,
        "t_client_decode_complete_ms": 1560.0,
        "t_client_audio_scheduled_ms": 1570.0,
        "t_client_playback_start_ms": 1580.0,
        "t_client_playback_end_ms": 2000.0,
    }
    for name, value in client.items():
        timer.mark_client(name, value)
    return timer


def test_derives_latency_stages_without_crossing_clock_domains() -> None:
    timer = complete_timer()

    metrics = timer.derived_metrics()

    assert metrics["endpoint_decision_ms"] == pytest.approx(500)
    assert metrics["stt_flush_to_final_ms"] == pytest.approx(300)
    assert metrics["llm_visible_ttft_ms"] == pytest.approx(200)
    assert metrics["llm_first_speakable_ms"] == pytest.approx(300)
    assert metrics["tts_ttfb_ms"] == pytest.approx(250)
    assert metrics["client_decode_ms"] == pytest.approx(10)
    assert metrics["e2e_voice_to_voice_ms"] == pytest.approx(580)
    assert metrics["upstream_audio_transport_ms"] is None
    assert not timer.censored


def test_e2e_uses_only_browser_clock_anchor() -> None:
    timer = complete_timer()
    original = timer.derived_metrics()["e2e_voice_to_voice_ms"]
    timer.server_timestamps = {
        key: value + 10_000 for key, value in timer.server_timestamps.items()
    }

    assert timer.derived_metrics()["e2e_voice_to_voice_ms"] == original


def test_feedback_and_answer_latency_remain_separate_and_same_clock() -> None:
    timer = complete_timer()
    timer.mark_server("t_response_cue_sent_server", 3.0)
    timer.mark_client("t_client_response_cue_start_ms", 1400.0)

    metrics = timer.derived_metrics()

    assert metrics["feedback_voice_to_voice_ms"] == pytest.approx(400)
    assert metrics["answer_voice_to_voice_ms"] == pytest.approx(580)
    assert metrics["answer_after_feedback_ms"] == pytest.approx(180)
    assert metrics["response_cue_dispatch_ms"] == pytest.approx(190)
    assert not timer.censored


def test_turn_without_response_cue_is_not_censored_by_optional_timestamps() -> None:
    timer = complete_timer()

    assert "t_response_cue_sent_server" not in timer.missing_timestamps()
    assert "t_client_response_cue_start_ms" not in timer.missing_timestamps()
    assert not timer.censored


def test_missing_timestamps_are_censored_and_never_zero_filled() -> None:
    timer = TurnTimer(turn_id="turn-1", turn_index=1, endpointing_strategy="local_vad")

    metrics = timer.derived_metrics()

    assert timer.censored
    assert "t_stt_final" in timer.missing_timestamps()
    assert all(value is None for value in metrics.values())


def test_stage_order_is_enforced_within_each_clock() -> None:
    timer = TurnTimer(turn_id="turn-1", turn_index=1, endpointing_strategy="local_vad")
    timer.mark_server("t_stt_flush_sent", 2.0)
    with pytest.raises(ValueError, match="timestamp order"):
        timer.mark_server("t_stt_final", 1.0)

    timer.mark_client("t_client_audio_received_ms", 200.0)
    with pytest.raises(ValueError, match="timestamp order"):
        timer.mark_client("t_client_decode_complete_ms", 190.0)


def test_tool_spans_validate_and_preserve_outcomes() -> None:
    timer = TurnTimer(turn_id="turn-1", turn_index=1, endpointing_strategy="local_vad")
    span = {
        "name": "web_search",
        "call_id": "call-1",
        "start_server": 1.0,
        "end_server": 1.25,
        "duration_ms": 250.0,
        "outcome": "success",
    }

    timer.record_tool_span(span)

    assert timer.tool_spans == [span]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0, 1.0), (50, 2.5), (95, 3.85), (100, 4.0)],
)
def test_percentile_linear_interpolation(requested: float, expected: float) -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], requested) == pytest.approx(expected)


def test_percentile_empty_and_invalid_inputs() -> None:
    assert percentile([], 99) is None
    with pytest.raises(ValueError):
        percentile([1.0], 101)


async def test_saved_tts_wait_matches_live_waterfall(db_session) -> None:
    from app.analytics.service import AnalyticsService
    from app.models import TurnMetric, UsageSession, User

    user = User(email="metrics@example.com", is_verified=True)
    db_session.add(user)
    await db_session.flush()
    usage = UsageSession(user_id=user.id)
    db_session.add(usage)
    await db_session.flush()
    db_session.add(
        TurnMetric(
            usage_session_id=usage.id,
            turn_index=0,
            tts_connection_acquire_ms=300.0,
            dimensions={"tts_connection_wait_ms": 10.0},
        )
    )
    await db_session.commit()

    detail = await AnalyticsService(db_session).session_detail(user.id, usage.id)
    stages = detail.turns[0].stages
    assert stages["tts_connection_wait_ms"] == 10.0
    assert stages["tts_socket_acquire_ms"] == 300.0
    overview = await AnalyticsService(db_session).overview(user.id, "day")
    rolled = {stage.key: stage for stage in overview.latency.stages}
    assert rolled["tts_connection_wait_ms"].p50 == 10.0
    assert rolled["tts_socket_acquire_ms"].p50 == 300.0
