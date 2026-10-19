import io
import json
import wave
from pathlib import Path

import pytest
from test_session_lifecycle import recovery_rig as recovery_rig
from test_session_lifecycle import voice_rig as voice_rig

from app.voice.protocol import (
    CaptureFrame,
    ProtocolError,
    decode_capture_frame,
    encode_capture_frame,
    encode_output_audio,
    normalize_audio_payload,
)

GOLDEN = json.loads((Path(__file__).parent / "fixtures/output-audio-v2.json").read_text())


def test_output_matches_shared_cross_language_golden():
    assert (
        encode_output_audio(GOLDEN["header"], bytes.fromhex(GOLDEN["payload_hex"])).hex()
        == GOLDEN["frame_hex"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("turn_id", ""),
        ("turn_id", "x" * 129),
        ("generation_id", None),
        ("sequence", True),
        ("sequence", -1),
        ("sequence", 2**32),
        ("sequence", 1.5),
        ("codec", "mp3"),
        ("codec", "wav"),
        ("sample_rate_hz", True),
        ("sample_rate_hz", 23999),
        ("kind", "other"),
        ("kind", []),
        ("turn_id", "\ud800"),
    ],
)
def test_output_rejects_invalid_header(field, value):
    with pytest.raises(ProtocolError):
        encode_output_audio({**GOLDEN["header"], field: value}, b"\0\0")


@pytest.mark.parametrize("payload", [b"", b"x", b"\0" * (256 * 1024 + 2)])
def test_output_rejects_empty_partial_or_oversized_pcm(payload):
    with pytest.raises(ProtocolError):
        encode_output_audio(GOLDEN["header"], payload)


def test_header_requires_exact_fields_and_utf8_identity():
    with pytest.raises(ProtocolError):
        encode_output_audio({**GOLDEN["header"], "extra": "x"}, b"\0\0")
    header = {**GOLDEN["header"], "turn_id": "कॉल:1", "kind": "cue", "sequence": 2**32 - 1}
    frame = encode_output_audio(header, b"\0\0")
    size = int.from_bytes(frame[5:9], "big")
    assert json.loads(frame[9 : 9 + size]) == header


def wav_audio(*, channels=1, width=2, rate=24000):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        writer.writeframes(b"\0" * (channels * width * 4))
    return output.getvalue()


def test_complete_wav_is_decoded_before_the_pcm_boundary():
    wav = wav_audio()
    assert normalize_audio_payload("wav", wav, 24000) == b"\0" * 8
    assert normalize_audio_payload("linear16", b"\x01\0", 24000) == b"\x01\0"
    assert normalize_audio_payload("pcm", b"\x01\0", 24000) == b"\x01\0"
    with pytest.raises(ProtocolError):
        normalize_audio_payload("linear16", wav, 24000)


@pytest.mark.parametrize(
    "audio",
    [
        wav_audio(channels=2),
        wav_audio(width=1),
        wav_audio(rate=16000),
        wav_audio()[:-2],
        b"RIFF\0\0\0\0WAVE",
        b"not a wav",
    ],
)
def test_invalid_or_mismatched_wav_is_never_played_as_pcm(audio):
    with pytest.raises(ProtocolError):
        normalize_audio_payload("wav", audio, 24000)


@pytest.mark.parametrize("codec,payload", [("mp3", b"encoded"), ("pcm", b"x"), ("pcm", b"")])
def test_invalid_vendor_profile_fails_closed(codec, payload):
    with pytest.raises(ProtocolError):
        normalize_audio_payload(codec, payload, 24000)


def test_capture_identity_changes_at_both_endpoints_without_changing_layout():
    frame = CaptureFrame(7, 12.5, b"\0\0")
    encoded = encode_capture_frame(frame)
    assert encoded[:5] == b"TLKC\x01" and len(encoded) == 23
    assert decode_capture_frame(encoded) == frame
    with pytest.raises(ProtocolError):
        decode_capture_frame(b"ADSH" + encoded[4:])


def output_parts(frame):
    assert frame[:5] == b"TLKS\x02"
    size = int.from_bytes(frame[5:9], "big")
    return json.loads(frame[9 : 9 + size]), frame[9 + size :]


async def test_sender_frames_pcm_with_sequence_and_rechecks_after_lock(voice_rig, monkeypatch):
    import asyncio

    r = voice_rig
    timer = r.voice._new_timer("local_vad")
    timer.dimensions["generation_id"] = "g-1"
    frames = []

    async def send(audio):
        frames.append(audio)

    monkeypatch.setattr(r.voice._websocket, "send_bytes", send, raising=False)
    pcm = b"\0\0" * (128 * 1024 + 1)
    assert await r.voice._send_turn_audio(timer.turn_id, pcm, generation_id="g-1")
    parts = [output_parts(frame) for frame in frames]
    assert [header["sequence"] for header, _ in parts] == [0, 1]
    assert all(
        header["generation_id"] == "g-1" and header["codec"] == "pcm_s16le" for header, _ in parts
    )
    assert b"".join(payload for _, payload in parts) == pcm
    assert max(len(payload) for _, payload in parts) <= 256 * 1024
    await r.voice._send_lock.acquire()
    pending = asyncio.create_task(
        r.voice._send_turn_audio(timer.turn_id, b"\0\0", generation_id="g-1")
    )
    await asyncio.sleep(0)
    r.voice._cancelled_response_turns.add(timer.turn_id)
    r.voice._send_lock.release()
    assert await pending is False
    assert len(frames) == 2


async def test_interrupt_reaches_browser_before_provider_teardown(voice_rig):
    import asyncio
    from types import SimpleNamespace

    r = voice_rig
    entered = asyncio.Event()
    release = asyncio.Event()

    async def close(reason):
        entered.set()
        await release.wait()

    task = await r.start()
    timer = r.voice._new_timer("local_vad")
    timer.dimensions["generation_id"] = "g-1"
    r.voice._active_tts = SimpleNamespace(connection_id="fake", close_and_discard=close)
    r.voice._active_tts_turn_id = timer.turn_id
    pending = asyncio.create_task(r.voice._interrupt_active_response(timer))
    try:
        await asyncio.wait_for(entered.wait(), 0.3)
        assert any(
            e["type"] == "interrupt" and e.get("generation_id") == "g-1" for e in r.state.events
        )
        assert r.voice._turn_output_cancelled(timer.turn_id)
    finally:
        release.set()
        await pending
        await r.end()
        await asyncio.wait_for(task, 0.5)


async def test_streamed_wav_is_normalized_and_tagged_before_browser_send(voice_rig, monkeypatch):
    import asyncio

    from app.voice.tts import SarvamTtsClient

    r = voice_rig
    r.voice._settings.tts_output_audio_codec = "wav"
    frames = []

    async def reply(*args, **kwargs):
        yield "Hello."

    async def synthesize(self, chunks, **kwargs):
        async for _ in chunks:
            yield wav_audio(rate=r.voice._settings.tts_sample_rate_hz)

    async def send(audio):
        frames.append(audio)

    monkeypatch.setattr(r.voice._agent, "stream_reply", reply, raising=False)
    monkeypatch.setattr(SarvamTtsClient, "synthesize", synthesize)
    monkeypatch.setattr(r.voice._websocket, "send_bytes", send, raising=False)
    task = await r.start()
    timer = r.voice._new_timer("local_vad")
    r.voice._playback_started_events[timer.turn_id].set()
    await r.voice._response_queue.put((timer, "hello"))
    await asyncio.wait_for(r.voice._response_queue.join(), 0.4)
    header, payload = output_parts(frames[0])
    assert payload == b"\0" * 8
    assert (
        header["turn_id"] == timer.turn_id
        and header["sample_rate_hz"] == r.voice._settings.tts_sample_rate_hz
    )
    assert header["generation_id"] == timer.dimensions["generation_id"]
    await r.end()
    await asyncio.wait_for(task, 0.5)


async def test_unsupported_synthesis_profile_is_rejected_before_initialization(voice_rig):
    from app.voice.protocol import ProbeOptions
    from app.voice.session import VoiceSession, VoiceSessionError

    r = voice_rig
    settings = r.voice._settings.model_copy(update={"tts_output_audio_codec": "mp3"})
    with pytest.raises(VoiceSessionError, match="codec"):
        VoiceSession(
            websocket=r.voice._websocket,
            settings=settings,
            probe_options=ProbeOptions(),
            database=r.base.database,
            user_id="owner",
            wallet=r.base.wallet,
            starting_balance_paise=500,
            connected_at=100.0,
        )


@pytest.mark.parametrize("stage", ["playback_started", "playback_finished", "response_cue_started"])
async def test_stale_audio_acknowledgements_cannot_update_current_generation(voice_rig, stage):
    r = voice_rig
    timer = r.voice._new_timer("local_vad")
    timer.dimensions.update(
        {"generation_id": "g-2", "response_cue_id": "cue-2", "response_cue_status": "sent"}
    )
    control = {
        "turn_id": timer.turn_id,
        "generation_id": "g-1",
        "cue_id": "cue-2",
        "audio_received_perf_ms": 1,
        "decode_complete_perf_ms": 2,
        "scheduled_perf_ms": 3,
        "playback_start_perf_ms": 4,
        "playback_end_perf_ms": 5,
        "played_audio_ms": 1,
        "cue_start_perf_ms": 4,
        "feedback_voice_to_voice_ms": 3,
    }
    await getattr(r.voice, f"_handle_{stage}")(control)
    assert timer.client_timestamps_ms == {}
    assert not r.voice._playback_started_events[timer.turn_id].is_set()
    control["generation_id"] = "g-2"
    await getattr(r.voice, f"_handle_{stage}")(control)
    assert timer.client_timestamps_ms
