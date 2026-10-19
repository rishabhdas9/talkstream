import asyncio
import time
from types import SimpleNamespace

import pytest

from app.runtime import RuntimePolicy
from app.voice.lifecycle import TurnDeadline, TurnDeadlineExceeded


def deadline(**overrides):
    return TurnDeadline(RuntimePolicy(**overrides), SimpleNamespace(monotonic=time.monotonic))


@pytest.mark.parametrize(
    ("stage", "field"),
    [
        ("initialization", "initialization_seconds"),
        ("stt_final", "stt_final_seconds"),
        ("first_answer", "first_answer_seconds"),
        ("tts_progress", "tts_progress_seconds"),
        ("turn_work", "turn_work_seconds"),
    ],
)
async def test_deadline_cancels_nonprogressing_work(stage, field):
    stopped = asyncio.Event()

    async def stalled():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(TurnDeadlineExceeded) as error:
        await deadline(**{field: 0.02}).run(stage, stalled())
    assert error.value.stage == stage and stopped.is_set()


async def test_new_earlier_stage_wakes_a_running_monitor():
    d = deadline(tts_progress_seconds=0.02)
    task = asyncio.create_task(d.monitor(asyncio.Event().wait()))
    await asyncio.sleep(0)
    d.arm("tts_progress")
    with pytest.raises(TurnDeadlineExceeded, match="tts_progress"):
        await asyncio.wait_for(task, 0.2)


async def test_commit_time_is_not_reset_when_queued_work_starts():
    d = deadline(first_answer_seconds=0.02)
    d.arm("first_answer", started_at=time.monotonic() - 1)
    called = False

    async def request():
        nonlocal called
        called = True

    with pytest.raises(TurnDeadlineExceeded, match="first_answer"):
        await d.run("turn_work", request())
    assert not called


async def test_audio_progress_and_first_answer_do_not_extend_total_work():
    d = deadline(first_answer_seconds=0.02, tts_progress_seconds=0.02, turn_work_seconds=0.07)

    async def stream():
        d.complete("first_answer")
        d.arm("tts_progress")
        while True:
            await asyncio.sleep(0.005)
            d.progress("tts_progress")

    d.arm("first_answer")
    with pytest.raises(TurnDeadlineExceeded, match="turn_work"):
        await d.run("turn_work", stream())


async def test_completed_answer_does_not_time_out_during_browser_playback():
    d = deadline(turn_work_seconds=0.02)
    assert await d.run("turn_work", asyncio.sleep(0, result="approved audio")) == "approved audio"
    await asyncio.sleep(0.03)


async def test_cancellation_resistant_work_fails_closed_with_a_finite_wait():
    from app.voice.lifecycle import SessionCleanupError

    release = asyncio.Event()
    stopped = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        finally:
            stopped.set()

    try:
        with pytest.raises(SessionCleanupError, match="did not stop"):
            await asyncio.wait_for(
                deadline(first_answer_seconds=0.01).run("first_answer", stubborn()), 1.3
            )
        assert not stopped.is_set()
    finally:
        release.set()
        await asyncio.wait_for(stopped.wait(), 0.1)
