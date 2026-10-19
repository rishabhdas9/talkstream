from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import socket
import time
import uuid
from collections import deque
from collections.abc import Sequence
from functools import partial
from typing import Any

import anyio
import httpx
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import SecretStr
from sqlalchemy import select, update

from app.config import Settings
from app.costs.models import BudgetExceeded, CostScope
from app.costs.rates import RateUnavailable
from app.db import Database
from app.models import TurnMetric, UsageSession
from app.policy.engine import PolicyEngine, policy_reply
from app.policy.models import PolicyContext, PolicyDocument
from app.voice.agent import AgentLimitError, AgentRunner, truncate_text_to_played_audio
from app.voice.chunker import PhraseChunker, spoken_form
from app.voice.extensions import VoiceAgentExtension
from app.voice.feedback import ResponseCuePolicy
from app.voice.gate import BargeInDetector, VadFrame, VadGate, VadGateDecision, build_vad_gate
from app.voice.lifecycle import (
    SessionCleanupError,
    SessionLifecycle,
    TurnDeadline,
    TurnDeadlineExceeded,
    bounded_cleanup,
    retain_cleanup_task,
)
from app.voice.logging import log_voice_event
from app.voice.metrics import TurnTimer
from app.voice.protocol import (
    AUDIO_SAMPLE_RATES,
    MAX_AUDIO_PAYLOAD_BYTES,
    ProbeOptions,
    ProtocolError,
    decode_capture_frame,
    decode_tool_result,
    encode_output_audio,
    normalize_audio_payload,
    sanitize_vendor_message,
    validate_client_message,
)
from app.voice.providers.contracts import ProviderContext, check_endpointing
from app.voice.providers.factory import RECOGNITION, SYNTHESIS, recognition_key, synthesis_key
from app.voice.providers.sarvam import SarvamSynthesizer
from app.voice.stt import SttConfig
from app.voice.tools import ClientToolProxy, ExaSearchClient, build_agent_tools
from app.voice.tts import SarvamTtsError, TtsConfig
from app.voice.vad import FRAME_SAMPLES
from app.wallet.billing import BillingMeter
from app.wallet.service import SessionBillingResult, WalletService


class VoiceSessionError(RuntimeError):
    pass


def _generation_id(timer: TurnTimer) -> str:
    return timer.dimensions.setdefault("generation_id", uuid.uuid4().hex)


def _is_transient_network_error(exc: BaseException) -> bool:
    """Recognize transport failures, including errors wrapped by SDKs."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (socket.gaierror, ConnectionError, TimeoutError, httpx.TransportError),
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


TTS_LANGUAGES = {
    "bn-IN",
    "en-IN",
    "gu-IN",
    "hi-IN",
    "kn-IN",
    "ml-IN",
    "mr-IN",
    "od-IN",
    "pa-IN",
    "ta-IN",
    "te-IN",
}


class VoiceSession:
    def __init__(
        self,
        *,
        websocket: WebSocket,
        settings: Settings,
        probe_options: ProbeOptions,
        database: Database,
        user_id: str,
        wallet: WalletService,
        starting_balance_paise: int,
        connected_at: float,
        agent_extensions: Sequence[VoiceAgentExtension] = (),
    ) -> None:
        api_key = settings.sarvam_api_key
        if api_key is None:
            raise VoiceSessionError("SARVAM_API_KEY is not configured")
        key = api_key.get_secret_value()
        lease = getattr(websocket.state, "admission_lease", None)
        costs = getattr(websocket.state, "cost_service", None)
        admission = getattr(websocket.state, "call_admission", None)
        if lease is None or costs is None or admission is None:
            raise VoiceSessionError("Voice admission is required")
        self._clock = costs.clock
        self._policy = costs.policy
        self._websocket = websocket
        self._settings = settings
        self._content_policy = PolicyDocument.load(settings.content_policy_file)
        self._probe = probe_options
        if (probe_options.tts_output_audio_codec or settings.tts_output_audio_codec) not in {
            "pcm",
            "linear16",
            "wav",
        } or settings.tts_sample_rate_hz not in AUDIO_SAMPLE_RATES:
            raise VoiceSessionError("Unsupported streaming audio codec or sample rate")
        self._database = database
        self._wallet = wallet
        self._admission = admission
        self._lease = lease
        self._billing_started = False
        self._closing = False
        self._terminal_reason = "user"
        self._consecutive_provider_failures = 0
        self._turn_deadlines: dict[str, TurnDeadline] = {}
        self._supervised_tasks: set[asyncio.Task[Any]] = set()
        self._cleanup = partial(bounded_cleanup, supervised_tasks=self._supervised_tasks)
        self._stt_deadline = TurnDeadline(self._policy, self._clock, self._supervised_tasks)
        self._stt_ready = asyncio.Event()
        self.lifecycle = SessionLifecycle(self._finalize_session)
        self._user_id = user_id
        self._starting_balance_paise = starting_balance_paise
        grant = getattr(websocket.state, "voice_grant", None)
        self._session_id = grant.session_id if grant is not None else str(uuid.uuid4())
        self._thread_id = str(uuid.uuid4())
        self._endpointing_strategy = (
            probe_options.endpointing_strategy or settings.endpointing_strategy
        )
        self._send_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._persist_lock = asyncio.Lock()
        self._turn_counter = 0
        self._latest_user_turn_index = 0
        self._last_capture_seq = 0
        self._last_capture_time_ms = 0.0
        self._last_frame_server_receive = 0.0
        self._last_stt_flush_at: float | None = None
        self._active_local_timer: TurnTimer | None = None
        self._active_sarvam_timer: TurnTimer | None = None
        self._realtime_timers: dict[int, TurnTimer] = {}
        self._realtime_final_starts: dict[int, float] = {}
        self._realtime_turn_offset = 0
        self._pending_local_timers: deque[TurnTimer] = deque()
        self._timers: dict[str, TurnTimer] = {}
        self._playback_started_events: dict[str, asyncio.Event] = {}
        self._persisted_turns: set[str] = set()
        self._first_stt_probe_sent = False
        self._sarvam_received_frames = 0
        self._sarvam_forwarded_frames = 0
        self._sarvam_shadow_last_speech: VadFrame | None = None
        self._response_queue: asyncio.Queue[tuple[TurnTimer, str]] = asyncio.Queue()
        self._active_response_task: asyncio.Task[None] | None = None
        self._active_response_timer: TurnTimer | None = None
        self._active_tts: SarvamSynthesizer | None = None
        self._active_tts_turn_id: str | None = None
        self._agent_playing_audio = False
        self._playback_turn_id: str | None = None
        self._interrupted_turns: set[str] = set()
        self._cancelled_response_turns: set[str] = set()
        self._response_cue_policy = ResponseCuePolicy(
            enabled=settings.response_cues_enabled,
            cooldown_turns=settings.response_cue_cooldown_turns,
        )
        self._barge_detector = BargeInDetector(sustained_speech_ms=settings.vad_barge_in_ms)
        self._barge_buffer: list[VadFrame] = []
        self._barge_pre_roll_frames = 0
        self._history_ready = asyncio.Event()
        self._history_ready.set()
        self._pending_history_timer: TurnTimer | None = None
        self._assistant_text_by_turn: dict[str, list[str]] = {}
        self._billing_stop = asyncio.Event()
        self._billing_disconnected_at: float | None = None
        self._billing_terminal_event_sent = False
        self._balance_exhausted = False
        self._low_balance_warned = False
        self._balance_cutoff_turn_ids: set[str] = set()
        client_type, recognizer_type = RECOGNITION[settings.stt_transport]
        if not recognizer_type.rotate_after_final and self._endpointing_strategy != "sarvam":
            raise VoiceSessionError("Continuous recognition requires provider endpointing")
        stt_key = recognition_key(settings, settings.stt_transport)
        if stt_key is None:
            raise VoiceSessionError("The selected recognition provider key is not configured")
        self._stt = recognizer_type(
            client_type(
                SttConfig(
                    api_key=stt_key.get_secret_value(),
                    model=settings.stt_model,
                    language_code=settings.stt_language_code,
                    sample_rate_hz=16_000,
                    input_audio_codec=(
                        probe_options.stt_input_audio_codec or settings.stt_input_audio_codec
                    ),
                    audio_encoding=probe_options.stt_audio_encoding or settings.stt_audio_encoding,
                ),
                costs=costs,
                scope=CostScope(user_id=user_id, session_id=self._session_id),
                expires_at=lease.expires_at,
            )
        )
        check_endpointing(self._endpointing_strategy, self._stt)
        exa_key = (
            settings.exa_api_key.get_secret_value() if settings.exa_api_key is not None else None
        )
        self._exa = ExaSearchClient(
            api_key=exa_key,
            timeout_seconds=settings.exa_search_timeout_seconds,
            costs=costs,
            scope=CostScope(user_id=user_id, session_id=self._session_id),
            expires_at=lease.expires_at,
        )
        self._todo_proxy = ClientToolProxy(
            sender=self._send_json,
            timeout_seconds=settings.client_tool_timeout_seconds,
            owner_id=user_id,
        )

        base_tools = build_agent_tools(
            exa_client=self._exa,
            todo_proxy=self._todo_proxy,
        )
        self._agent = AgentRunner(
            api_key=key,
            base_url=settings.sarvam_chat_base_url,
            model_name=settings.llm_model,
            thread_id=self._thread_id,
            costs=costs,
            scope=CostScope(user_id=user_id, session_id=self._session_id),
            expires_at=lease.expires_at,
            tools=[
                *base_tools,
                *(tool for extension in agent_extensions for tool in extension.tools),
            ],
            system_prompt_extensions=[extension.system_prompt for extension in agent_extensions],
            receipt_lookup=self._todo_proxy.reconcile,
        )
        tts_key = synthesis_key(settings)
        if tts_key is None:
            raise VoiceSessionError("The selected synthesis provider key is not configured")
        self._api_key = tts_key.get_secret_value()
        self._gate: VadGate | None = None
        self._billing_meter = BillingMeter(
            wallet=wallet,
            user_id=user_id,
            usage_session_id=self._session_id,
            connected_at=connected_at,
            price_per_minute_paise=settings.price_per_minute_paise,
        )

    async def run(self) -> None:
        end_reason = "user"
        supervised_tasks = self._supervised_tasks
        billing_task: asyncio.Task[None] | None = None
        try:
            try:
                await TurnDeadline(self._policy, self._clock, self._supervised_tasks).run(
                    "initialization", self._initialize()
                )
            except TurnDeadlineExceeded as exc:
                raise VoiceSessionError("Voice initialization timed out") from exc
            self._billing_meter.connected_at = self._clock.monotonic()
            self._billing_started = True
            self._stt_ready.set()
            billing_task = asyncio.create_task(self._billing_loop(), name="voice-billing-meter")
            supervised_tasks.add(billing_task)
            browser_task = asyncio.create_task(self._browser_loop(), name="voice-browser-receiver")
            stt_task = asyncio.create_task(self._stt_loop(), name="voice-stt-receiver")
            response_task = asyncio.create_task(self._response_loop(), name="voice-response-worker")
            supervised_tasks.update({browser_task, stt_task, response_task})
            done, _ = await asyncio.wait(
                {browser_task, stt_task, response_task, billing_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                try:
                    task.result()
                except Exception as exc:
                    log_voice_event(
                        logging.ERROR,
                        "session_worker_failed",
                        session_id=self._session_id,
                        turn_id=self._current_turn_id(),
                        worker=task.get_name(),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    raise
            end_reason = "balance_exhausted" if self._balance_exhausted else self._terminal_reason
        except asyncio.CancelledError:
            end_reason = (
                "session_expired"
                if self._clock.utc_now() >= self._lease.expires_at
                else "cancelled"
            )
            raise
        except Exception as exc:
            if isinstance(exc, SessionCleanupError):
                end_reason = "cleanup_incomplete"
            elif isinstance(exc, (BudgetExceeded, RateUnavailable)):
                end_reason = "provider_budget_exhausted"
            else:
                end_reason = (
                    self._terminal_reason
                    if self._terminal_reason == "provider_failures"
                    else "error"
                )
            log_voice_event(
                logging.ERROR,
                "session_error",
                session_id=self._session_id,
                turn_id=None,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        finally:
            self._closing = True
            if self._billing_disconnected_at is None:
                self._billing_disconnected_at = self._clock.monotonic()
            await self.lifecycle.finish(end_reason)

    async def _initialize(self) -> None:
        await self._start_usage_session()
        self._gate = await asyncio.to_thread(self._build_gate)
        await self._stt.connect(
            ProviderContext(
                scope=CostScope(user_id=self._user_id, session_id=self._session_id),
                deadline=self._lease.expires_at,
            )
        )
        log_voice_event(
            logging.INFO,
            "session_start",
            session_id=self._session_id,
            turn_id=None,
            endpointing_strategy=self._endpointing_strategy,
            vad_model=self._gate.vad_name if self._gate else None,
        )
        await self._send_json(
            {
                "type": "ready",
                "session_id": self._session_id,
                "balance_paise": self._starting_balance_paise,
                "price_per_minute_paise": self._settings.price_per_minute_paise,
                "tts_sample_rate_hz": self._settings.tts_sample_rate_hz,
                "endpointing_strategy": self._endpointing_strategy,
                "vad_model": self._gate.vad_name if self._gate else None,
                "capabilities": [
                    "audio_frames_v2",
                    *(["response_cues_v1"] if self._settings.response_cues_enabled else []),
                ],
            }
        )
        await self._send_initial_billing()
        await self._send_listening_state()
        await self._wallet.mark_ready(
            user_id=self._user_id,
            usage_session_id=self._session_id,
            ready_at=self._clock.utc_now(),
        )

    async def _finalize_session(self, reason: str) -> None:
        self._closing = True
        self._billing_stop.set()
        if self._billing_disconnected_at is None:
            self._billing_disconnected_at = self._clock.monotonic()
        ended_at = self._clock.utc_now()
        self._cancelled_response_turns.update(self._timers)
        self._todo_proxy.cancel_pending()
        errors = ["PriorCleanupFailure"] if reason == "cleanup_incomplete" else []

        async def attempt(work):
            try:
                return await self._cleanup(work)
            except Exception as exc:
                errors.append(type(exc).__name__)
                log_voice_event(
                    logging.ERROR,
                    "session_cleanup_step_failed",
                    session_id=self._session_id,
                    turn_id=None,
                    error_type=type(exc).__name__,
                )
                return None

        work = set(self._supervised_tasks)
        if self._active_response_task is not None:
            work.add(self._active_response_task)
        work.discard(asyncio.current_task())
        for task in work:
            if not task.done():
                task.cancel()
        if work:
            done, pending = await asyncio.wait(work, timeout=1)
            for task in done:
                if not task.cancelled():
                    task.exception()
            for task in pending:
                task.cancel()
                retain_cleanup_task(task)
            if pending:
                errors.append("SessionWorkStillRunning")
        await attempt(self._stt.close())
        if self._active_tts is not None:
            await attempt(self._active_tts.close_and_discard("session ended"))
        if self._billing_started and not self._billing_terminal_event_sent:
            result = await attempt(self._billing_meter.finalize(self._billing_disconnected_at))
            if result is not None:
                if result.exhausted:
                    self._balance_exhausted = True
                    reason = "balance_exhausted"
                    self._capture_balance_cutoff_turns()

                async def notify():
                    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                        await self._record_and_emit_billing(
                            result,
                            final=True,
                            terminated_reason="balance_exhausted" if result.exhausted else None,
                        )
                        terminal: dict[str, Any] = {"type": "call_ended", "reason": reason}
                        message = {
                            "error": "Voice session failed",
                            "cleanup_incomplete": "Call cleanup could not finish safely.",
                            "provider_budget_exhausted": (
                                "Call stopped: the AI spending allowance is unavailable."
                            ),
                            "session_expired": "Call time limit reached.",
                            "provider_failures": "Call stopped after repeated provider failures.",
                        }.get(reason)
                        if message is not None:
                            terminal["message"] = message
                        await self._send_json(terminal)

                with contextlib.suppress(Exception):
                    await self._cleanup(notify(), timeout_seconds=0.2)
        if self._balance_exhausted:
            await attempt(self._persist_balance_cutoff_turns())
        await attempt(self._exa.close())
        await attempt(self._end_usage_session(reason, ended_at=ended_at))
        if errors:
            raise SessionCleanupError("Call cleanup could not finish safely")
        await self._cleanup(self._admission.release(self._session_id))
        log_voice_event(
            logging.INFO,
            "session_end",
            session_id=self._session_id,
            turn_id=None,
            reason=reason,
            turns=self._turn_counter,
        )

    async def _send_initial_billing(self) -> None:
        low_balance = self._starting_balance_paise <= self._settings.low_balance_warn_paise
        if low_balance:
            self._low_balance_warned = True
            log_voice_event(
                logging.INFO,
                "low_balance_warning",
                session_id=self._session_id,
                turn_id=None,
                balance_paise=self._starting_balance_paise,
                threshold_paise=self._settings.low_balance_warn_paise,
            )
        await self._send_json(
            {
                "type": "billing",
                "session_id": self._session_id,
                "seconds": 0,
                "cost_paise": 0,
                "session_cost_paise": 0,
                "charged_paise": 0,
                "balance_paise": self._starting_balance_paise,
                "low_balance": low_balance,
                "warning": "low_balance" if low_balance else None,
                "final": False,
                "terminated_reason": None,
            }
        )

    async def _billing_loop(self) -> None:
        while not self._billing_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._billing_stop.wait(),
                    timeout=self._settings.billing_tick_seconds,
                )
                return
            except TimeoutError:
                pass
            result = await self._billing_meter.charge_full_elapsed(self._clock.monotonic())
            if result.exhausted:
                self._closing = True
                self._balance_exhausted = True
                self._capture_balance_cutoff_turns()
                self._billing_disconnected_at = self._clock.monotonic()
                self._billing_stop.set()
                return
            await self._cleanup(
                self._record_and_emit_billing(result, final=False, terminated_reason=None),
                timeout_seconds=0.2,
            )

    async def _record_and_emit_billing(
        self,
        result: SessionBillingResult,
        *,
        final: bool,
        terminated_reason: str | None,
    ) -> None:
        low_balance = result.balance_paise <= self._settings.low_balance_warn_paise
        if result.charged_paise:
            log_voice_event(
                logging.INFO,
                "billing_deduction",
                session_id=self._session_id,
                turn_id=None,
                charged_paise=result.charged_paise,
                session_cost_paise=result.cost_paise,
                billed_seconds=result.billable_seconds,
                balance_paise=result.balance_paise,
            )
        if low_balance and not self._low_balance_warned:
            self._low_balance_warned = True
            log_voice_event(
                logging.INFO,
                "low_balance_warning",
                session_id=self._session_id,
                turn_id=None,
                balance_paise=result.balance_paise,
                threshold_paise=self._settings.low_balance_warn_paise,
            )
        if terminated_reason is not None:
            self._billing_terminal_event_sent = True
            log_voice_event(
                logging.INFO,
                "billing_terminated",
                session_id=self._session_id,
                turn_id=None,
                reason=terminated_reason,
                billed_seconds=result.billable_seconds,
                session_cost_paise=result.cost_paise,
                balance_paise=result.balance_paise,
            )
        await self._send_json(
            {
                "type": "billing",
                "session_id": self._session_id,
                "seconds": result.billable_seconds,
                "cost_paise": result.cost_paise,
                "session_cost_paise": result.cost_paise,
                "charged_paise": result.charged_paise,
                "balance_paise": result.balance_paise,
                "low_balance": low_balance,
                "warning": "low_balance" if low_balance else None,
                "final": final,
                "terminated_reason": terminated_reason,
            }
        )

    def _capture_balance_cutoff_turns(self) -> None:
        candidates = (
            self._active_local_timer,
            self._active_sarvam_timer,
            self._active_response_timer,
            self._timers.get(self._playback_turn_id) if self._playback_turn_id else None,
        )
        self._balance_cutoff_turn_ids.update(
            timer.turn_id for timer in candidates if timer is not None
        )

    async def _persist_balance_cutoff_turns(self) -> None:
        for turn_id in self._balance_cutoff_turn_ids:
            timer = self._timers.get(turn_id)
            if timer is None:
                continue
            timer.dimensions["balance_cutoff"] = True
            await self._persist_timer(timer)

    def _build_gate(self) -> VadGate:
        return build_vad_gate(self._settings, session_id=self._session_id)

    async def _browser_loop(self) -> None:
        try:
            while True:
                message = await self._websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    self._terminal_reason = "disconnect"
                    self._billing_disconnected_at = self._clock.monotonic()
                    return
                binary = message.get("bytes")
                if binary is not None:
                    await self._handle_audio_frame(binary)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                control = json.loads(text)
                if not isinstance(control, dict):
                    raise ProtocolError("control message must be a JSON object")
                control = validate_client_message(control)
                message_type = control.get("type")
                if message_type == "end_call":
                    self._billing_disconnected_at = self._clock.monotonic()
                    self._billing_stop.set()
                    return
                if message_type == "probe_flush" and self._probe.enabled:
                    if self._endpointing_strategy != "sarvam":
                        raise ProtocolError("probe_flush is only valid with Sarvam endpointing")
                    self._last_stt_flush_at = time.monotonic()
                    sent = await self._stt.flush()
                    await self._send_probe("stt", "sent", sent)
                    continue
                if message_type == "playback_started":
                    await self._handle_playback_started(control)
                    continue
                if message_type == "playback_finished":
                    await self._handle_playback_finished(control)
                    continue
                if message_type == "interrupt_ack":
                    await self._handle_interrupt_ack(control)
                    continue
                if message_type == "response_cue_started":
                    await self._handle_response_cue_started(control)
                    continue
                if message_type == "tool_result":
                    await self._handle_tool_result(control)
        except WebSocketDisconnect:
            self._terminal_reason = "disconnect"
            self._billing_disconnected_at = self._clock.monotonic()
            self._billing_stop.set()
            return

    async def _handle_tool_result(self, control: dict[str, Any]) -> None:
        tool_result = decode_tool_result(control)
        accepted = self._todo_proxy.resolve(
            call_id=tool_result.call_id,
            result=tool_result.result,
        )
        log_voice_event(
            logging.INFO,
            "tool_result_received" if accepted else "tool_result_discarded",
            session_id=self._session_id,
            turn_id=(
                self._active_response_timer.turn_id
                if self._active_response_timer is not None
                else self._current_turn_id()
            ),
            call_id=tool_result.call_id,
            outcome="accepted" if accepted else "late_or_cancelled",
        )

    async def _response_loop(self) -> None:
        while True:
            timer, transcript = await self._response_queue.get()
            try:
                if timer.turn_index < getattr(self, "_latest_user_turn_index", 0):
                    timer.dimensions["superseded_before_response"] = True
                    self._turn_deadlines.pop(timer.turn_id, None)
                    await self._persist_timer(timer)
                    continue
                if not self._history_ready.is_set():
                    try:
                        await asyncio.wait_for(self._history_ready.wait(), timeout=2.0)
                    except TimeoutError:
                        pending = self._pending_history_timer
                        if pending is not None:
                            await self._reconcile_interrupted_history(
                                pending,
                                played_audio_ms=0.0,
                                acknowledgement="timeout",
                            )
                deadline = self._turn_deadlines.setdefault(
                    timer.turn_id, TurnDeadline(self._policy, self._clock, self._supervised_tasks)
                )
                deadline.arm("first_answer")
                response = asyncio.create_task(
                    deadline.run(
                        "turn_work", self._process_turn(timer=timer, transcript=transcript)
                    ),
                    name=f"voice-response-{timer.turn_index}",
                )
                self._active_response_task = response
                self._active_response_timer = timer
                try:
                    await response
                except (BudgetExceeded, RateUnavailable, SessionCleanupError):
                    raise
                except TurnDeadlineExceeded as exc:
                    await self._recover_deadline_turn(timer, exc)
                    self._record_provider_failure()
                except SarvamTtsError as exc:
                    await self._recover_tts_turn(timer, exc)
                    self._record_provider_failure()
                except AgentLimitError as exc:
                    # A per-turn limit (answer length, tool count) fails the turn, not the call.
                    await self._recover_turn(
                        timer,
                        exc,
                        error_dimension="agent_limit",
                        error_code="turn_limit_reached",
                        user_message="That answer was too long. Please ask for a shorter one.",
                    )
                except Exception as exc:
                    if not _is_transient_network_error(exc):
                        raise
                    await self._recover_network_turn(timer, exc)
                    self._record_provider_failure()
                except asyncio.CancelledError:
                    if asyncio.current_task() and asyncio.current_task().cancelling():
                        raise
                    if not response.cancelled():
                        raise
                else:
                    self._consecutive_provider_failures = 0
            finally:
                self._turn_deadlines.pop(timer.turn_id, None)
                if self._active_response_timer is timer:
                    self._active_response_timer = None
                if self._active_response_task is not None and self._active_response_task.done():
                    self._active_response_task = None
                self._response_queue.task_done()

    def _record_provider_failure(self) -> None:
        self._consecutive_provider_failures += 1
        if self._consecutive_provider_failures >= self._policy.max_provider_failures:
            self._terminal_reason = "provider_failures"
            raise VoiceSessionError("Voice stopped after consecutive provider failures")

    async def _recover_deadline_turn(self, timer: TurnTimer, exc: TurnDeadlineExceeded) -> None:
        await self._recover_turn(
            timer,
            exc,
            error_dimension=exc.stage,
            error_code="turn_deadline_exceeded",
            user_message="That response took too long. Please try again.",
        )

    async def _recover_tts_turn(self, timer: TurnTimer, exc: SarvamTtsError) -> None:
        """Fail one response without taking down the authenticated voice session."""
        await self._recover_turn(
            timer,
            exc,
            error_dimension="tts_rejected",
            error_code="tts_turn_failed",
            user_message="I could not speak that response. Please try again.",
        )

    async def _recover_network_turn(self, timer: TurnTimer, exc: Exception) -> None:
        """Recover after the upstream client's bounded retry is exhausted."""
        await self._recover_turn(
            timer,
            exc,
            error_dimension="upstream_network",
            error_code="upstream_temporarily_unavailable",
            user_message="The voice service was temporarily unavailable. Please try again.",
        )

    async def _recover_turn(
        self,
        timer: TurnTimer,
        exc: Exception,
        *,
        error_dimension: str,
        error_code: str,
        user_message: str,
    ) -> None:
        timer.dimensions["response_error"] = error_dimension
        self._cancelled_response_turns.add(timer.turn_id)
        self._todo_proxy.cancel_pending()
        if self._assistant_text_by_turn.get(timer.turn_id):
            await self._interrupt_active_response(timer, reason="provider_failure")
        if self._playback_turn_id == timer.turn_id:
            self._agent_playing_audio = False
            self._playback_turn_id = None
        log_voice_event(
            logging.WARNING,
            "turn_response_failed",
            session_id=self._session_id,
            turn_id=timer.turn_id,
            error_type=type(exc).__name__,
            error=str(exc),
            recovered=True,
        )
        await self._send_json(
            {
                "type": "error",
                "turn_id": timer.turn_id,
                "generation_id": timer.dimensions.get("generation_id"),
                "code": error_code,
                "message": user_message,
                "stage": exc.stage if isinstance(exc, TurnDeadlineExceeded) else None,
            }
        )
        await self._persist_timer(timer)
        await self._send_listening_state()

    async def _handle_audio_frame(self, binary: bytes) -> None:
        frame = decode_capture_frame(binary)
        received_at = time.monotonic()
        self._last_capture_seq = frame.capture_seq
        self._last_capture_time_ms = frame.capture_time_ms
        self._last_frame_server_receive = received_at
        log_voice_event(
            logging.DEBUG,
            "audio_frame_received",
            session_id=self._session_id,
            turn_id=self._current_turn_id(),
            capture_seq=frame.capture_seq,
            pcm_bytes=len(frame.pcm),
        )
        if self._endpointing_strategy == "sarvam":
            self._sarvam_received_frames += 1
            gate = self._gate
            if gate is not None:
                shadow_decision = gate.process(
                    VadFrame(
                        capture_seq=frame.capture_seq,
                        capture_time_ms=frame.capture_time_ms,
                        server_received_at=received_at,
                        pcm=frame.pcm,
                    )
                )
                if shadow_decision.forward_frames:
                    self._sarvam_shadow_last_speech = shadow_decision.forward_frames[-1]
                if shadow_decision.endpoint_fired:
                    self._sarvam_shadow_last_speech = shadow_decision.last_speech_frame
            await self._forward_stt_audio(frame.pcm, frame.capture_seq, self._current_turn_id())
            self._sarvam_forwarded_frames += 1
            return
        if len(frame.pcm) != FRAME_SAMPLES * 2:
            raise ProtocolError(f"local VAD requires {FRAME_SAMPLES}-sample PCM frames")
        gate = self._gate
        if gate is None:
            raise VoiceSessionError("local VAD gate is not initialized")
        decision = gate.process(
            VadFrame(
                capture_seq=frame.capture_seq,
                capture_time_ms=frame.capture_time_ms,
                server_received_at=received_at,
                pcm=frame.pcm,
            )
        )
        await self._apply_gate_decision(decision)

    async def _apply_gate_decision(self, decision: VadGateDecision) -> None:
        if (self._agent_playing_audio or self._barge_buffer) and await self._handle_barge_candidate(
            decision
        ):
            return
        thinking_cancellation: asyncio.Task[None] | None = None
        if decision.speech_started and self._is_thinking_response_active():
            active_timer = self._active_response_timer
            if active_timer is not None:
                thinking_cancellation = asyncio.create_task(
                    self._cancel_thinking_response(active_timer),
                    name=f"cancel-thinking-{active_timer.turn_index}",
                )
        if decision.speech_started:
            await self._start_local_turn(
                list(decision.forward_frames),
                speech_probability=decision.speech_probability,
            )
        elif decision.silence_started and self._active_local_timer is not None:
            await self._send_agent_state(
                "user_speaking",
                transmitting=False,
                detail="silence - not transmitting",
            )
        elif decision.speech_resumed and self._active_local_timer is not None:
            await self._send_agent_state("user_speaking", transmitting=True)

        turn_id = self._active_local_timer.turn_id if self._active_local_timer else None
        for forwarded in decision.forward_frames:
            await self._forward_stt_audio(forwarded.pcm, forwarded.capture_seq, turn_id)

        if thinking_cancellation is not None:
            await thinking_cancellation

        if not decision.endpoint_fired:
            return
        timer = self._active_local_timer
        last_speech = decision.last_speech_frame
        stats = decision.completed_stats
        if timer is None or last_speech is None or stats is None:
            raise VoiceSessionError("VadGate fired an endpoint without an active turn")
        if not getattr(self._stt, "heard_speech", True):
            # Its START_SPEECH lands ~0.4 s into speech, before this endpoint; without it the
            # flush returns nothing and the turn would fail its deadline. The audio stays
            # buffered and joins the next utterance's flush.
            self._active_local_timer = None
            log_voice_event(
                logging.INFO,
                "endpoint_without_provider_speech",
                session_id=self._session_id,
                turn_id=timer.turn_id,
            )
            return
        endpoint_at = time.monotonic()
        timer.mark_server("t_last_speech_frame_server", last_speech.server_received_at)
        timer.set_speech_anchor(
            capture_seq=last_speech.capture_seq,
            capture_time_ms=last_speech.capture_time_ms,
        )
        timer.mark_server("t_endpoint_decision", endpoint_at)
        timer.dimensions.update(
            {
                "received_frames": stats.received_frames,
                "forwarded_frames": stats.forwarded_frames,
                "gated_silent_frames": stats.gated_silent_frames,
                "pre_roll_frames": stats.pre_roll_frames,
                "silent_frames_forwarded": 0,
            }
        )
        timer.mark_server("t_stt_flush_sent", time.monotonic())
        self._last_stt_flush_at = timer.server_timestamps["t_stt_flush_sent"]
        self._pending_local_timers.append(timer)
        self._active_local_timer = None
        self._stt_deadline.arm("stt_final")
        await self._stt_ready.wait()
        if self._turn_output_cancelled(timer.turn_id):
            return
        sent = await self._stt.flush()
        if self._probe.enabled:
            await self._send_probe("stt", "sent", sent)
        await self._send_agent_state("thinking")
        log_voice_event(
            logging.INFO,
            "endpoint_fired",
            session_id=self._session_id,
            turn_id=timer.turn_id,
            endpointing_strategy="local_vad",
            endpoint_window_ms=timer.derived_metrics()["endpoint_decision_ms"],
            received_frames=stats.received_frames,
            forwarded_frames=stats.forwarded_frames,
            gated_silent_frames=stats.gated_silent_frames,
            silent_frames_forwarded=0,
        )

    async def _start_local_turn(
        self,
        frames: list[VadFrame],
        *,
        speech_probability: float,
        pre_roll_frames: int | None = None,
    ) -> TurnTimer:
        if not frames:
            raise VoiceSessionError("cannot start a local turn without speech frames")
        first_frame = frames[0]
        current_frame = frames[-1]
        timer = self._new_timer("local_vad")
        self._latest_user_turn_index = timer.turn_index
        timer.mark_server("t_audio_frame_server_receive", first_frame.server_received_at)
        timer.mark_server("t_speech_start_server", current_frame.server_received_at)
        timer.dimensions["vad_model"] = self._gate.vad_name if self._gate else None
        self._active_local_timer = timer
        await self._send_agent_state("user_speaking", transmitting=True)
        log_voice_event(
            logging.INFO,
            "speech_onset",
            session_id=self._session_id,
            turn_id=timer.turn_id,
            capture_seq=current_frame.capture_seq,
            speech_probability=speech_probability,
            pre_roll_frames=(
                max(0, len(frames) - 1) if pre_roll_frames is None else pre_roll_frames
            ),
        )
        return timer

    async def _handle_barge_candidate(self, decision: VadGateDecision) -> bool:
        playback_turn_id = self._playback_turn_id
        if not self._agent_playing_audio and self._barge_buffer:
            self._barge_buffer.extend(decision.forward_frames)
            timer = await self._start_local_turn(
                self._barge_buffer,
                speech_probability=decision.speech_probability,
                pre_roll_frames=self._barge_pre_roll_frames,
            )
            buffered = tuple(self._barge_buffer)
            self._barge_buffer.clear()
            self._barge_detector.reset()
            self._barge_pre_roll_frames = 0
            for frame in buffered:
                await self._forward_stt_audio(frame.pcm, frame.capture_seq, timer.turn_id)
            return True
        if not self._agent_playing_audio:
            return False

        speech_frame = decision.forward_frames[-1] if decision.forward_frames else None
        if speech_frame is not None:
            self._barge_buffer.extend(decision.forward_frames)
            barge = self._barge_detector.observe_speech(speech_frame)
            if barge.candidate_started:
                self._barge_pre_roll_frames = max(
                    0,
                    len(decision.forward_frames) - 1,
                )
                log_voice_event(
                    logging.INFO,
                    "barge_in_candidate",
                    session_id=self._session_id,
                    turn_id=playback_turn_id,
                    capture_seq=speech_frame.capture_seq,
                )
            if barge.detected:
                if playback_turn_id is None or playback_turn_id not in self._timers:
                    raise VoiceSessionError("barge-in detected without an active playback turn")
                interrupted_timer = self._timers[playback_turn_id]
                onset = barge.onset_server_time or speech_frame.server_received_at
                interrupted_timer.mark_server("t_barge_speech_onset_server", onset)
                interrupted_timer.mark_server(
                    "t_barge_speech_detected_server", speech_frame.server_received_at
                )
                log_voice_event(
                    logging.INFO,
                    "barge_in_detected",
                    session_id=self._session_id,
                    turn_id=playback_turn_id,
                    detection_ms=interrupted_timer.derived_metrics()["barge_detection_ms"],
                    buffered_frames=len(self._barge_buffer),
                )
                buffered = tuple(self._barge_buffer)
                next_timer = await self._start_local_turn(
                    list(buffered),
                    speech_probability=decision.speech_probability,
                    pre_roll_frames=self._barge_pre_roll_frames,
                )
                await self._interrupt_active_response(interrupted_timer)
                for frame in buffered:
                    await self._forward_stt_audio(
                        frame.pcm,
                        frame.capture_seq,
                        next_timer.turn_id,
                    )
                await self._send_agent_state("user_speaking", transmitting=True)
                self._barge_buffer.clear()
                self._barge_detector.reset()
                self._barge_pre_roll_frames = 0
                return True
            return True

        if decision.silence_started or decision.endpoint_fired:
            cancelled = self._barge_detector.observe_silence()
            if cancelled.candidate_cancelled:
                log_voice_event(
                    logging.INFO,
                    "barge_in_candidate_rejected",
                    session_id=self._session_id,
                    turn_id=playback_turn_id,
                    buffered_frames=len(self._barge_buffer),
                )
            self._barge_buffer.clear()
            self._barge_pre_roll_frames = 0
            if self._gate is not None:
                self._gate.reset()
        return True

    async def _interrupt_active_response(
        self, timer: TurnTimer, *, reason: str = "barge_in"
    ) -> None:
        if timer.turn_id in self._interrupted_turns:
            return
        self._interrupted_turns.add(timer.turn_id)
        self._agent_playing_audio = False
        self._playback_turn_id = None
        timer.dimensions["interrupted"] = True
        timer.dimensions["interrupt_reason"] = (
            "sustained_user_speech" if reason == "barge_in" else reason
        )
        timer.dimensions["interrupted_audio_generated_ms"] = (
            _optional_float(timer.dimensions.get("generated_audio_ms")) or 0.0
        )

        response_task = self._active_response_task
        tts = self._active_tts if self._active_tts_turn_id == timer.turn_id else None
        if response_task is not None and not response_task.done():
            response_task.cancel()
            cancellation_status = "cancel_requested"
        else:
            cancellation_status = "already_complete"
        log_voice_event(
            logging.INFO,
            "llm_stream_cancelled",
            session_id=self._session_id,
            turn_id=timer.turn_id,
            status=cancellation_status,
        )
        cancelled_tools = self._todo_proxy.cancel_pending()
        if cancelled_tools:
            log_voice_event(
                logging.INFO,
                "client_tools_cancelled",
                session_id=self._session_id,
                turn_id=timer.turn_id,
                count=cancelled_tools,
                reason="barge_in",
            )

        timer.dimensions["tts_socket_teardown_status"] = (
            "closing" if tts is not None else "already_closed"
        )
        timer.mark_server("t_interrupt_sent_server", time.monotonic())
        self._history_ready.clear()
        self._pending_history_timer = timer
        await self._cleanup(
            self._send_json(
                {
                    "type": "interrupt",
                    "turn_id": timer.turn_id,
                    "generation_id": _generation_id(timer),
                    "reason": reason,
                }
            ),
            timeout_seconds=0.2,
        )

        tts_connection_id = tts.connection_id if tts is not None else None
        tts_teardown_status = "closed_and_discarded" if tts is not None else "already_closed"
        if tts is not None:
            await self._cleanup(tts.close_and_discard("barge-in"))
        timer.dimensions["tts_socket_teardown_status"] = tts_teardown_status
        log_voice_event(
            logging.INFO,
            "tts_socket_closed",
            session_id=self._session_id,
            turn_id=timer.turn_id,
            connection_id=tts_connection_id,
            status=tts_teardown_status,
        )
        if response_task is not None and not response_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup(response_task)

        await self._send_agent_state(
            "interrupted",
            detail="Interrupted by user - playback cleared",
        )
        log_voice_event(
            logging.INFO,
            "interrupt_sent",
            session_id=self._session_id,
            turn_id=timer.turn_id,
            acknowledgement_proxy=True,
        )
        await self._persist_timer(timer)

    def _is_thinking_response_active(self) -> bool:
        response = self._active_response_task
        timer = self._active_response_timer
        return (
            response is not None
            and not response.done()
            and timer is not None
            and not self._agent_playing_audio
            and self._playback_turn_id != timer.turn_id
        )

    async def _cancel_thinking_response(self, timer: TurnTimer) -> None:
        response = self._active_response_task
        if (
            response is None
            or response.done()
            or self._active_response_timer is not timer
            or self._agent_playing_audio
        ):
            return

        self._cancelled_response_turns.add(timer.turn_id)
        timer.dimensions["cancelled_while_thinking"] = True
        timer.dimensions["response_cancelled"] = True
        timer.dimensions["cancel_reason"] = "new_user_speech"
        await self._cancel_response_cue(timer, reason="new_user_speech")

        self._history_ready.clear()
        try:
            response.cancel()
            cancelled_tools = self._todo_proxy.cancel_pending()
            if cancelled_tools:
                log_voice_event(
                    logging.INFO,
                    "client_tools_cancelled",
                    session_id=self._session_id,
                    turn_id=timer.turn_id,
                    count=cancelled_tools,
                    reason="new_user_speech_while_thinking",
                )

            tts = self._active_tts if self._active_tts_turn_id == timer.turn_id else None
            if tts is not None:
                await self._cleanup(tts.close_and_discard("new user speech while thinking"))
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup(response)

            history_result = await self._agent.reconcile_interrupted_history("")
            timer.dimensions.update(
                {
                    "history_reconciled": True,
                    "history_acknowledgement": "not_required_before_playback",
                    "retained_spoken_characters": 0,
                    **history_result,
                }
            )
        finally:
            self._history_ready.set()
        if self._active_response_task is response:
            self._active_response_task = None
        if self._active_response_timer is timer:
            self._active_response_timer = None
        await self._persist_timer(timer)
        log_voice_event(
            logging.INFO,
            "thinking_response_cancelled",
            session_id=self._session_id,
            turn_id=timer.turn_id,
            tts_closed=tts is not None,
            history_reconciled=True,
        )

    async def _dispatch_response_cue(
        self,
        timer: TurnTimer,
        language_code: str | None,
    ) -> None:
        await asyncio.sleep(self._settings.response_cue_delay_ms / 1000)
        if (
            self._active_response_timer is not timer
            or self._turn_output_cancelled(timer.turn_id)
            or self._agent_playing_audio
            or timer.last_speech_capture_time_ms is None
            or not self._response_cue_policy.reserve(turn_index=timer.turn_index)
        ):
            return

        cue_id = f"{timer.turn_id}:cue"
        cue_language = self._response_cue_policy.language_for(language_code)
        timer.dimensions.update(
            {
                "response_cue_id": cue_id,
                "response_cue_key": "neutral_ack",
                "response_cue_language_code": cue_language,
                "response_cue_status": "sent",
            }
        )
        timer.mark_server("t_response_cue_sent_server", time.monotonic())
        await self._send_json(
            {
                "type": "response_cue",
                "turn_id": timer.turn_id,
                "generation_id": _generation_id(timer),
                "cue_id": cue_id,
                "cue_key": "neutral_ack",
                "language_code": cue_language,
                "delay_ms": self._settings.response_cue_delay_ms,
                "last_speech_capture_time_ms": timer.last_speech_capture_time_ms,
            }
        )
        log_voice_event(
            logging.INFO,
            "response_cue_sent",
            session_id=self._session_id,
            turn_id=timer.turn_id,
            cue_id=cue_id,
            language_code=cue_language,
            dispatch_ms=timer.derived_metrics()["response_cue_dispatch_ms"],
        )

    async def _cancel_response_cue(self, timer: TurnTimer, *, reason: str) -> None:
        message = self._prepare_response_cue_cancel(timer, reason=reason)
        if message is not None:
            await self._send_json(message)

    @staticmethod
    def _prepare_response_cue_cancel(
        timer: TurnTimer,
        *,
        reason: str,
    ) -> dict[str, Any] | None:
        cue_id = timer.dimensions.get("response_cue_id")
        status = timer.dimensions.get("response_cue_status")
        if not isinstance(cue_id, str) or status not in {"sent", "started"}:
            return None
        timer.dimensions["response_cue_status"] = "cancelled"
        timer.dimensions["response_cue_cancel_reason"] = reason
        return {
            "type": "response_cue_cancel",
            "turn_id": timer.turn_id,
            "generation_id": _generation_id(timer),
            "cue_id": cue_id,
            "reason": reason,
        }

    async def _handle_response_cue_started(self, control: dict[str, Any]) -> None:
        turn_id = control.get("turn_id")
        cue_id = control.get("cue_id")
        if not isinstance(turn_id, str) or turn_id not in self._timers:
            raise ProtocolError("response_cue_started has an unknown turn_id")
        timer = self._timers[turn_id]
        if control.get("generation_id") != timer.dimensions.get("generation_id"):
            return
        if self._turn_output_cancelled(turn_id):
            return

        if cue_id != timer.dimensions.get("response_cue_id"):
            return
        if "t_client_response_cue_start_ms" in timer.client_timestamps_ms:
            return
        cue_was_cancelled = timer.dimensions.get("response_cue_status") == "cancelled"
        if cue_was_cancelled:
            timer.dimensions["response_cue_ack_after_cancel"] = True
        cue_start = float(control["cue_start_perf_ms"])
        timer.mark_client("t_client_response_cue_start_ms", cue_start)
        if not cue_was_cancelled:
            timer.dimensions["response_cue_status"] = "started"
        reported = float(control["feedback_voice_to_voice_ms"])
        computed = timer.derived_metrics()["feedback_voice_to_voice_ms"]
        timer.dimensions["response_cue_reported_feedback_ms"] = reported
        timer.dimensions["response_cue_feedback_delta_ms"] = (
            abs(reported - computed) if computed is not None else None
        )
        if turn_id in self._persisted_turns:
            await self._persist_timer(timer)

    async def _handle_interrupt_ack(self, control: dict[str, Any]) -> None:
        turn_id = control.get("turn_id")
        if not isinstance(turn_id, str) or turn_id not in self._interrupted_turns:
            raise ProtocolError("interrupt_ack has an unknown turn_id")
        if control.get("audio_queue_cleared") is not True:
            raise ProtocolError("interrupt_ack must confirm audio_queue_cleared")
        timer = self._timers[turn_id]
        if control.get("generation_id") != timer.dimensions.get("generation_id"):
            return

        received = control.get("interrupt_received_perf_ms")
        cleared = control.get("queue_cleared_perf_ms")
        if not isinstance(received, int | float) or not isinstance(cleared, int | float):
            raise ProtocolError("interrupt_ack timestamps must be numeric")
        timer.mark_client("t_interrupt_received_client_ms", float(received))
        timer.mark_client("t_playback_queue_cleared_client_ms", float(cleared))
        timer.mark_server("t_interrupt_ack_received_server", time.monotonic())
        played_audio_ms = _optional_float(control.get("played_audio_ms")) or 0.0
        timer.dimensions["interrupted_audio_played_ms"] = max(0.0, played_audio_ms)
        await self._reconcile_interrupted_history(
            timer,
            played_audio_ms=max(0.0, played_audio_ms),
            acknowledgement="received",
        )
        await self._persist_timer(timer)
        if self._settings.metrics_hud:
            await self._send_json(self._metrics_event(timer))
        derived = timer.derived_metrics()
        await self._send_json(
            {
                "type": "interrupt_resolved",
                "turn_id": turn_id,
                "barge_in_stop_ack_ms": derived["barge_in_stop_ack_ms"],
                "played_audio_ms": played_audio_ms,
                "tts_socket_teardown_status": timer.dimensions.get("tts_socket_teardown_status"),
            }
        )
        log_voice_event(
            logging.INFO,
            "interrupt_ack",
            session_id=self._session_id,
            turn_id=turn_id,
            barge_client_flush_ms=derived["barge_client_flush_ms"],
            barge_in_stop_ack_ms=derived["barge_in_stop_ack_ms"],
            acknowledgement_proxy=True,
            played_audio_ms=played_audio_ms,
        )

    async def _reconcile_interrupted_history(
        self,
        timer: TurnTimer,
        *,
        played_audio_ms: float,
        acknowledgement: str,
    ) -> None:
        if timer.dimensions.get("history_reconciled") is True:
            self._history_ready.set()
            return
        generated_audio_ms = (
            _optional_float(timer.dimensions.get("interrupted_audio_generated_ms")) or 0.0
        )
        full_text = " ".join(self._assistant_text_by_turn.get(timer.turn_id, []))
        retained_text = truncate_text_to_played_audio(
            full_text,
            played_audio_ms=played_audio_ms,
            generated_audio_ms=generated_audio_ms,
        )
        result = await self._agent.reconcile_interrupted_history(retained_text)
        timer.dimensions.update(
            {
                "history_reconciled": True,
                "history_acknowledgement": acknowledgement,
                "history_truncation_method": "played_audio_ratio_word_boundary",
                "retained_spoken_characters": len(retained_text),
                **result,
            }
        )
        self._pending_history_timer = None
        self._history_ready.set()
        log_voice_event(
            logging.INFO,
            "assistant_history_truncated",
            session_id=self._session_id,
            turn_id=timer.turn_id,
            acknowledgement=acknowledgement,
            generated_audio_ms=generated_audio_ms,
            played_audio_ms=played_audio_ms,
            **result,
        )

    async def _forward_stt_audio(self, pcm: bytes, capture_seq: int, turn_id: str | None) -> None:
        await self._stt_ready.wait()
        if self._closing or (turn_id is not None and self._turn_output_cancelled(turn_id)):
            return
        sent = await self._stt.send_audio(pcm)
        log_voice_event(
            logging.DEBUG,
            "stt_audio_sent",
            session_id=self._session_id,
            turn_id=turn_id,
            capture_seq=capture_seq,
            pcm_bytes=len(pcm),
            endpointing_strategy=self._endpointing_strategy,
        )
        if self._probe.enabled and not self._first_stt_probe_sent:
            self._first_stt_probe_sent = True
            await self._send_probe("stt", "sent", sent)

    async def _stt_loop(self) -> None:
        rotate = False
        while True:
            try:
                if rotate:
                    self._stt_ready.clear()
                    reconnect_started = time.monotonic()
                    await self._stt_deadline.monitor(self._stt.reconnect())
                    self._realtime_timers.clear()
                    self._realtime_final_starts.clear()
                    self._realtime_turn_offset = self._latest_user_turn_index
                    self._active_sarvam_timer = None
                    self._stt_ready.set()
                    rotate = False
                    log_voice_event(
                        logging.INFO,
                        "stt_socket_rotated",
                        session_id=self._session_id,
                        turn_id=self._current_turn_id(),
                        reconnect_ms=(time.monotonic() - reconnect_started) * 1000,
                    )
                await self._stt_deadline.monitor(self._receive_stt_final())
                self._stt_deadline.complete("stt_final")
                if self._realtime_final_starts:
                    self._stt_deadline.arm(
                        "stt_final", started_at=min(self._realtime_final_starts.values())
                    )
                rotate = getattr(self._stt, "rotate_after_final", True)
            except TurnDeadlineExceeded as exc:
                self._stt_deadline.complete("stt_final")
                timer = self._timer_for_final(time.monotonic())
                await self._recover_deadline_turn(timer, exc)
                self._record_provider_failure()
                rotate = True
            except (BudgetExceeded, RateUnavailable, SessionCleanupError):
                raise
            except Exception as exc:
                if not _is_transient_network_error(exc):
                    raise
                self._stt_deadline.complete("stt_final")
                timer = self._timer_for_final(time.monotonic())
                await self._recover_network_turn(timer, exc)
                self._record_provider_failure()
                rotate = True

    async def _receive_stt_final(self) -> TurnTimer:
        async for event in self._stt.events():
            received_at = event.received_at
            log_voice_event(
                logging.DEBUG,
                "stt_vendor_message",
                session_id=self._session_id,
                turn_id=self._current_turn_id(),
                direction="received",
                message=sanitize_vendor_message(event.raw),
            )
            if self._probe.enabled:
                await self._send_probe("stt", "received", event.raw)
            if event.kind in {"speech_start", "speech_end"}:
                await self._handle_stt_event(event.kind, received_at, event.utterance_id)
                continue
            if event.kind == "error":
                raise VoiceSessionError("Speech recognition is temporarily unavailable.")
            if event.kind != "final":
                continue
            transcript = event.text
            timer = self._timer_for_final(received_at, utterance_id=event.utterance_id)
            if getattr(self._stt, "rotate_after_final", True):
                self._stt_ready.clear()
            timer.mark_server("t_stt_final", received_at)
            duration = event.audio_duration_s
            timer.raw_provider_metrics = (
                {"audio_duration": duration}
                if duration is not None and duration <= self._policy.session_seconds
                else None
            )
            timer.dimensions.update(
                {
                    "language_code": event.language if event.language in TTS_LANGUAGES else None,
                    "language_confidence": event.confidence,
                    "stt_request_id": _provider_request_id_hash(event.provider_request_id),
                }
            )
            log_voice_event(
                logging.INFO,
                "stt_final",
                session_id=self._session_id,
                turn_id=timer.turn_id,
                transcript_chars=len(transcript.strip()),
                language_code=timer.dimensions["language_code"],
                stt_ms=timer.waterfall()["stt_ms"],
                provider_metrics=timer.raw_provider_metrics,
            )
            deadline = TurnDeadline(self._policy, self._clock, self._supervised_tasks)
            deadline.arm("first_answer")
            deadline.arm("turn_work")
            self._turn_deadlines[timer.turn_id] = deadline
            await self._response_queue.put((timer, transcript.strip()))
            self._last_stt_flush_at = None
            return timer
        raise VoiceSessionError("Saaras WebSocket closed before the voice session ended")

    async def _handle_stt_event(
        self, kind: str, received_at: float, utterance_id: int | None = None
    ) -> None:
        if self._endpointing_strategy != "sarvam":
            return
        if utterance_id is not None:
            if utterance_id not in self._realtime_timers:
                if len(self._realtime_timers) >= 16:
                    raise VoiceSessionError("Too many pending recognition utterances")
                timer = self._new_timer("sarvam")
                # Finalization can lag behind a later utterance's speech-start event.
                timer.turn_index = self._realtime_turn_offset + utterance_id + 1
                self._realtime_timers[utterance_id] = timer
            self._active_sarvam_timer = self._realtime_timers[utterance_id]
            if kind == "speech_end":
                self._realtime_final_starts.setdefault(utterance_id, self._clock.monotonic())
        if kind == "speech_start":
            if self._is_thinking_response_active() and self._active_response_timer is not None:
                await self._cancel_thinking_response(self._active_response_timer)
            timer = self._active_sarvam_timer or self._new_timer("sarvam")
            self._active_sarvam_timer = timer
            self._latest_user_turn_index = timer.turn_index
            timer.mark_server("t_audio_frame_server_receive", self._last_frame_server_receive)
            timer.mark_server("t_speech_start_server", received_at)
            timer.dimensions["speech_anchor_source"] = "sarvam_signal_approximation"
            await self._send_agent_state("user_speaking", transmitting=True)
            log_voice_event(
                logging.INFO,
                "speech_onset",
                session_id=self._session_id,
                turn_id=timer.turn_id,
                endpointing_strategy="sarvam",
            )
        elif kind == "speech_end":
            timer = self._active_sarvam_timer or self._new_timer("sarvam")
            self._active_sarvam_timer = timer
            timer.mark_server("t_audio_frame_server_receive", self._last_frame_server_receive)
            timer.mark_server("t_speech_start_server", received_at)
            timer.mark_server("t_endpoint_decision", received_at)
            self._stt_deadline.arm("stt_final")
            shadow_last_speech = self._sarvam_shadow_last_speech
            if shadow_last_speech is not None:
                timer.mark_server(
                    "t_last_speech_frame_server", shadow_last_speech.server_received_at
                )
                timer.set_speech_anchor(
                    capture_seq=shadow_last_speech.capture_seq,
                    capture_time_ms=shadow_last_speech.capture_time_ms,
                )
            else:
                timer.set_speech_anchor(
                    capture_seq=self._last_capture_seq,
                    capture_time_ms=self._last_capture_time_ms,
                )
            timer.dimensions.update(
                {
                    "speech_anchor_source": (
                        "local_vad_shadow_last_speech_frame"
                        if shadow_last_speech is not None
                        else "sarvam_end_speech_receive_approximation"
                    ),
                    "vad_model": self._gate.vad_name if self._gate else None,
                    "received_frames": self._sarvam_received_frames,
                    "forwarded_frames": self._sarvam_forwarded_frames,
                    "gated_silent_frames": 0,
                    "silent_frames_forwarded": "all_input_silence",
                }
            )
            await self._send_agent_state("thinking")
            log_voice_event(
                logging.INFO,
                "endpoint_fired",
                session_id=self._session_id,
                turn_id=timer.turn_id,
                endpointing_strategy="sarvam",
                received_frames=self._sarvam_received_frames,
                forwarded_frames=self._sarvam_forwarded_frames,
                gated_silent_frames=0,
                sends_silence_to_vendor=True,
            )
            self._sarvam_received_frames = 0
            self._sarvam_forwarded_frames = 0
            self._sarvam_shadow_last_speech = None
            if self._gate is not None:
                self._gate.reset()

    def _timer_for_final(self, received_at: float, *, utterance_id: int | None = None) -> TurnTimer:
        if utterance_id is not None:
            self._realtime_final_starts.pop(utterance_id, None)
            timer = self._realtime_timers.pop(utterance_id, None)
            if timer is not None:
                if timer is self._active_sarvam_timer:
                    self._active_sarvam_timer = None
                return timer
        if (
            utterance_id is None
            and self._endpointing_strategy == "local_vad"
            and self._pending_local_timers
        ):
            return self._pending_local_timers.popleft()
        if (
            utterance_id is None
            and self._endpointing_strategy == "sarvam"
            and self._active_sarvam_timer
        ):
            timer = self._active_sarvam_timer
            self._active_sarvam_timer = None
            return timer
        timer = self._new_timer(self._endpointing_strategy)
        if utterance_id is not None:
            timer.turn_index = self._realtime_turn_offset + utterance_id + 1
        self._latest_user_turn_index = max(self._latest_user_turn_index, timer.turn_index)
        timer.mark_server("t_audio_frame_server_receive", self._last_frame_server_receive)
        timer.mark_server("t_speech_start_server", received_at)
        timer.mark_server("t_last_speech_frame_server", received_at)
        timer.mark_server("t_endpoint_decision", received_at)
        timer.set_speech_anchor(
            capture_seq=self._last_capture_seq,
            capture_time_ms=self._last_capture_time_ms,
        )
        timer.dimensions["speech_anchor_source"] = "final_receive_fallback"
        return timer

    async def _approved_answer(self, timer, transcript, *, observer, tool_observer) -> str:
        language = _optional_string(timer.dimensions.get("language_code"))
        context = PolicyContext(
            user_id=self._user_id,
            session_id=self._session_id,
            turn_id=timer.turn_id,
            policy_version=self._content_policy.version,
        )

        async def record(event):
            timer.dimensions.setdefault("policy_events", []).append(
                event.model_dump(mode="json", exclude={"context"})
            )

        async def classify(stage, text, policy_context):
            if stage == "output":
                text = json.dumps(
                    {
                        "user_request": transcript,
                        "candidate_answer": text,
                        "receipts": self._agent.policy_evidence(timer.turn_id),
                    },
                    ensure_ascii=False,
                )
            return await self._agent.classify_policy(
                stage, text, policy_context, policy=self._content_policy
            )

        engine = PolicyEngine(
            policy=self._content_policy,
            classifier=classify,
            event_sink=record,
            clock=self._clock,
            secrets=tuple(
                value.get_secret_value()
                for name in type(self._settings).model_fields
                if isinstance(value := getattr(self._settings, name), SecretStr)
            ),
            hidden_instructions=(self._agent._system_message.content,),
        )
        decision = await engine.inspect("input", transcript, context)
        if self._turn_output_cancelled(timer.turn_id):
            raise asyncio.CancelledError
        if not decision.allowed:
            return policy_reply(decision, language)
        await self._send_json(
            {
                "type": "final_transcript",
                "turn_id": timer.turn_id,
                "generation_id": _generation_id(timer),
                "text": transcript,
                "language_code": language,
                "language_probability": _optional_float(
                    timer.dimensions.get("language_confidence")
                ),
            }
        )
        hold_start = self._clock.monotonic()
        approved = False
        safe_history_reply = ""
        try:
            answer = "".join(
                [
                    token
                    async for token in self._agent.stream_reply(
                        transcript,
                        observer=observer,
                        tool_observer=tool_observer,
                        turn_id=timer.turn_id,
                        generation_id=_generation_id(timer),
                        language_code=language,
                        policy=engine,
                        policy_context=context,
                        is_current=lambda: not self._turn_output_cancelled(timer.turn_id),
                    )
                ]
            )
            decision = await engine.inspect("output", answer, context)
            if self._turn_output_cancelled(timer.turn_id):
                raise asyncio.CancelledError
            approved = decision.allowed
            if approved:
                return answer
            safe_history_reply = policy_reply(decision, language)
            return safe_history_reply
        finally:
            timer.dimensions["policy_answer_hold_ms"] = max(
                0.0, (self._clock.monotonic() - hold_start) * 1000
            )
            if not approved:
                with anyio.CancelScope(shield=True):
                    await self._cleanup(
                        self._agent.reconcile_interrupted_history(safe_history_reply)
                    )

    async def _process_turn(self, *, timer: TurnTimer, transcript: str) -> None:
        deadline = self._turn_deadlines[timer.turn_id]
        async with self._turn_lock:
            if self._turn_output_cancelled(timer.turn_id):
                return
            generation_id = _generation_id(timer)
            language_code = _optional_string(timer.dimensions.get("language_code"))
            await self._send_agent_state("thinking")

            async def observe(name: str, timestamp: float, details: dict[str, Any]) -> None:
                timer.mark_server(name, timestamp)
                if name == "t_tts_first_chunk":
                    details = {"request_id": _provider_request_id_hash(details.get("request_id"))}
                    timer.dimensions["tts_request_id"] = details["request_id"]
                level = (
                    logging.INFO
                    if name
                    in {
                        "t_llm_request_start",
                        "t_llm_first_visible_token",
                        "t_llm_first_speakable_chunk",
                        "t_tts_first_chunk",
                    }
                    else logging.DEBUG
                )
                log_voice_event(
                    level,
                    name.removeprefix("t_"),
                    session_id=self._session_id,
                    turn_id=timer.turn_id,
                    **details,
                )
                if self._probe.enabled:
                    await self._send_json(
                        {
                            "type": "probe_observation",
                            "stage": name,
                            "server_monotonic": timestamp,
                            "details": details,
                        }
                    )

            async def observe_tool(span: dict[str, Any]) -> None:
                sources = span.pop("sources", None)
                if sources and not self._turn_output_cancelled(timer.turn_id):
                    await self._send_json(
                        {
                            "type": "search_results",
                            "turn_id": timer.turn_id,
                            "generation_id": generation_id,
                            "call_id": span["call_id"],
                            "results": sources,
                        }
                    )
                timer.record_tool_span(span)
                tool_names = list(dict.fromkeys(item["name"] for item in timer.tool_spans))
                timer.dimensions["tool_used"] = True
                timer.dimensions["tool_names"] = tool_names
                log_voice_event(
                    logging.INFO,
                    "tool_call_completed",
                    session_id=self._session_id,
                    turn_id=timer.turn_id,
                    tool_name=span["name"],
                    call_id=span["call_id"],
                    duration_ms=span["duration_ms"],
                    outcome=span["outcome"],
                    error=span.get("error"),
                )
                if self._probe.enabled:
                    await self._send_json(
                        {
                            "type": "probe_tool_span",
                            "turn_id": timer.turn_id,
                            "span": span,
                        }
                    )

            chunker = (
                PhraseChunker()
                if self._settings.phrase_chunking_enabled
                else PhraseChunker(clause_min_chars=180, first_max_chars=180)
            )

            async def speakable_chunks():
                first_speakable = True
                answer = await self._approved_answer(
                    timer, transcript, observer=observe, tool_observer=observe_tool
                )
                for chunk in chunker.push(answer):
                    if self._turn_output_cancelled(timer.turn_id):
                        return
                    if first_speakable:
                        first_speakable = False
                        deadline.arm("tts_progress")
                        await observe("t_llm_first_speakable_chunk", time.monotonic(), {})
                    await self._send_json(
                        {
                            "type": "agent_text",
                            "turn_id": timer.turn_id,
                            "generation_id": generation_id,
                            "text": chunk,
                        }
                    )
                    self._assistant_text_by_turn.setdefault(timer.turn_id, []).append(chunk)
                    if PhraseChunker._is_speakable(speech := spoken_form(chunk)):
                        yield speech
                for chunk in chunker.flush():
                    if self._turn_output_cancelled(timer.turn_id):
                        return
                    if first_speakable:
                        first_speakable = False
                        deadline.arm("tts_progress")
                        await observe("t_llm_first_speakable_chunk", time.monotonic(), {})
                    await self._send_json(
                        {
                            "type": "agent_text",
                            "turn_id": timer.turn_id,
                            "generation_id": generation_id,
                            "text": chunk,
                        }
                    )
                    self._assistant_text_by_turn.setdefault(timer.turn_id, []).append(chunk)
                    if PhraseChunker._is_speakable(speech := spoken_form(chunk)):
                        yield speech

            target_language = (
                language_code
                if language_code in TTS_LANGUAGES
                else self._settings.tts_language_code
            )

            async def tts_probe(direction: str, message: dict[str, Any]) -> None:
                log_voice_event(
                    logging.DEBUG,
                    "tts_vendor_message",
                    session_id=self._session_id,
                    turn_id=timer.turn_id,
                    direction=direction,
                    message=sanitize_vendor_message(message),
                )
                if self._probe.enabled:
                    await self._send_probe("tts", direction, message)

            tts_codec = self._probe.tts_output_audio_codec or self._settings.tts_output_audio_codec
            tts = SarvamSynthesizer(
                SYNTHESIS[self._settings.tts_provider](
                    TtsConfig(
                        api_key=self._api_key,
                        model=self._settings.tts_model,
                        speaker=self._settings.tts_speaker_for_language(target_language),
                        target_language_code=target_language,
                        sample_rate_hz=self._settings.tts_sample_rate_hz,
                        output_audio_codec=tts_codec,
                        pace=self._settings.tts_pace,
                    ),
                    costs=self._websocket.state.cost_service,
                    scope=CostScope(user_id=self._user_id, session_id=self._session_id),
                    expires_at=self._websocket.state.admission_lease.expires_at,
                ),
                codec=tts_codec,
                sample_rate_hz=self._settings.tts_sample_rate_hz,
                observer=observe,
                probe=tts_probe,
            )
            self._active_tts = tts
            self._active_tts_turn_id = timer.turn_id
            timer.dimensions["tts_connection_id"] = tts.connection_id

            chunk_queue: asyncio.Queue[str | Exception | None] = asyncio.Queue()

            async def produce_chunks() -> None:
                try:
                    async for chunk in speakable_chunks():
                        await chunk_queue.put(chunk)
                except Exception as exc:
                    await chunk_queue.put(exc)
                finally:
                    await chunk_queue.put(None)

            async def queued_chunks():
                while True:
                    item = await chunk_queue.get()
                    if item is None:
                        return
                    if isinstance(item, Exception):
                        raise item
                    yield item

            chunk_producer = asyncio.create_task(
                produce_chunks(), name=f"agent-chunker-{timer.turn_index}"
            )
            cue_task = asyncio.create_task(
                self._dispatch_response_cue(timer, language_code),
                name=f"response-cue-{timer.turn_index}",
            )
            self._supervised_tasks.update({chunk_producer, cue_task})
            first_audio = True
            audio_stream = None
            try:
                audio_stream = tts.synthesize(
                    queued_chunks(),
                    ProviderContext(
                        scope=CostScope(user_id=self._user_id, session_id=self._session_id),
                        deadline=self._websocket.state.admission_lease.expires_at,
                        turn_id=timer.turn_id,
                        generation_id=generation_id,
                    ),
                )
                async for chunk in audio_stream:
                    if not chunk.data:
                        continue
                    audio = normalize_audio_payload(chunk.codec, chunk.data, chunk.sample_rate_hz)
                    deadline.progress("tts_progress")
                    generated_audio_ms = len(audio) / (self._settings.tts_sample_rate_hz * 2) * 1000
                    timer.dimensions["generated_audio_ms"] = (
                        _optional_float(timer.dimensions.get("generated_audio_ms")) or 0.0
                    ) + generated_audio_ms
                    if self._turn_output_cancelled(timer.turn_id):
                        continue
                    if first_audio:
                        first_audio = False
                        if not cue_task.done():
                            cue_task.cancel()
                        self._prepare_response_cue_cancel(
                            timer,
                            reason="answer_started",
                        )
                        await self._send_json(
                            {
                                "type": "audio_start",
                                "turn_id": timer.turn_id,
                                "generation_id": generation_id,
                                "last_speech_capture_seq": timer.last_speech_capture_seq,
                                "last_speech_capture_time_ms": timer.last_speech_capture_time_ms,
                                "speech_anchor_source": timer.dimensions.get(
                                    "speech_anchor_source",
                                    "local_vad_last_speech_frame",
                                ),
                            }
                        )
                        await self._send_agent_state("speaking")
                        self._agent_playing_audio = True
                        self._playback_turn_id = timer.turn_id
                        timer.mark_server("t_audio_sent_server", time.monotonic())
                        log_voice_event(
                            logging.INFO,
                            "audio_sent",
                            session_id=self._session_id,
                            turn_id=timer.turn_id,
                            first_chunk_bytes=len(audio),
                        )
                    await self._send_turn_audio(timer.turn_id, audio, generation_id=generation_id)
                    deadline.complete("first_answer")
                await chunk_producer
                if first_audio:
                    raise SarvamTtsError("Synthesis produced no answer audio")
                deadline.complete("tts_progress")
                deadline.complete("turn_work")
            except asyncio.CancelledError:
                cancellation_time = time.monotonic()
                if "t_llm_request_start" in timer.server_timestamps:
                    timer.mark_server("t_llm_complete", cancellation_time)
                if "t_tts_connection_acquire_start" in timer.server_timestamps:
                    timer.mark_server("t_tts_complete", cancellation_time)
                timer.dimensions["response_cancelled"] = True
                log_voice_event(
                    logging.INFO,
                    "response_stream_cancelled",
                    session_id=self._session_id,
                    turn_id=timer.turn_id,
                    generated_audio_ms=timer.dimensions.get("generated_audio_ms", 0.0),
                )
                raise
            finally:
                if not cue_task.done():
                    cue_task.cancel()
                if not chunk_producer.done():
                    chunk_producer.cancel()
                if audio_stream is not None:
                    await self._cleanup(audio_stream.aclose())
                with contextlib.suppress(asyncio.CancelledError):
                    await self._cleanup(cue_task)
                if first_audio:
                    await self._cancel_response_cue(timer, reason="turn_cancelled")
                with contextlib.suppress(asyncio.CancelledError):
                    await self._cleanup(chunk_producer)
                self._supervised_tasks.difference_update(
                    task for task in (cue_task, chunk_producer) if task.done()
                )
                if self._active_tts is tts:
                    self._active_tts = None
                    self._active_tts_turn_id = None

            playback_event = self._playback_started_events[timer.turn_id]
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(playback_event.wait(), timeout=3.0)
            await self._persist_timer(timer)
            metrics_event = self._metrics_event(timer)
            if self._settings.metrics_hud:
                await self._send_json(metrics_event)
            await self._send_json(
                {
                    "type": "turn_complete",
                    "turn_id": timer.turn_id,
                    "generation_id": generation_id,
                    "latencies_ms": timer.derived_metrics(),
                    "stt_provider_metrics": timer.raw_provider_metrics,
                }
            )
            log_voice_event(
                logging.INFO,
                "turn_metrics",
                session_id=self._session_id,
                turn_id=timer.turn_id,
                waterfall=timer.waterfall(),
                derived=timer.derived_metrics(),
                dimensions=timer.dimensions,
                missing_timestamps=timer.missing_timestamps(),
            )

    async def _handle_playback_started(self, control: dict[str, Any]) -> None:
        turn_id = control.get("turn_id")
        if not isinstance(turn_id, str) or turn_id not in self._timers:
            raise ProtocolError("playback_started has an unknown turn_id")
        timer = self._timers[turn_id]
        if control.get("generation_id") != timer.dimensions.get("generation_id"):
            return
        if self._turn_output_cancelled(turn_id):
            return

        fields = {
            "t_client_audio_received_ms": control.get("audio_received_perf_ms"),
            "t_client_decode_complete_ms": control.get("decode_complete_perf_ms"),
            "t_client_audio_scheduled_ms": control.get("scheduled_perf_ms"),
            "t_client_playback_start_ms": control.get("playback_start_perf_ms"),
        }
        for name, value in fields.items():
            if isinstance(value, int | float):
                timer.mark_client(name, float(value))
        self._playback_started_events[turn_id].set()
        log_voice_event(
            logging.INFO,
            "client_playback_started",
            session_id=self._session_id,
            turn_id=turn_id,
            e2e_voice_to_voice_ms=timer.derived_metrics()["e2e_voice_to_voice_ms"],
        )
        if turn_id in self._persisted_turns:
            await self._persist_timer(timer)

    async def _handle_playback_finished(self, control: dict[str, Any]) -> None:
        turn_id = control.get("turn_id")
        if not isinstance(turn_id, str) or turn_id not in self._timers:
            raise ProtocolError("playback_finished has an unknown turn_id")
        timer = self._timers[turn_id]
        if control.get("generation_id") != timer.dimensions.get("generation_id"):
            return
        if self._turn_output_cancelled(turn_id):
            return

        playback_end = control.get("playback_end_perf_ms")
        if isinstance(playback_end, int | float):
            timer.mark_client("t_client_playback_end_ms", float(playback_end))
        timer.dimensions["played_audio_ms"] = _optional_float(control.get("played_audio_ms"))
        if turn_id in self._persisted_turns:
            await self._persist_timer(timer)
        if self._playback_turn_id == turn_id:
            self._agent_playing_audio = False
            self._playback_turn_id = None
            if not self._barge_buffer:
                await self._send_listening_state()

    def _new_timer(self, endpointing_strategy: str) -> TurnTimer:
        self._turn_counter += 1
        turn_id = f"{self._session_id}:{self._turn_counter}"
        timer = TurnTimer(
            turn_id=turn_id,
            turn_index=self._turn_counter,
            endpointing_strategy=endpointing_strategy,
            dimensions={"endpointing_strategy": endpointing_strategy},
        )
        self._timers[turn_id] = timer
        self._playback_started_events[turn_id] = asyncio.Event()
        return timer

    def _current_turn_id(self) -> str | None:
        timer = self._active_local_timer or self._active_sarvam_timer
        return timer.turn_id if timer else None

    def _metrics_event(self, timer: TurnTimer) -> dict[str, Any]:
        return {
            "type": "turn_metrics",
            "turn_id": timer.turn_id,
            "endpointing_strategy": timer.endpointing_strategy,
            "stages": timer.waterfall(),
            "derived": timer.derived_metrics(),
            "dimensions": timer.dimensions,
            "tool_spans": timer.tool_spans,
            "missing_timestamps": timer.missing_timestamps(),
        }

    async def _persist_timer(self, timer: TurnTimer) -> None:
        derived = timer.derived_metrics()
        dimensions = dict(timer.dimensions)
        dimensions["stt_endpoint_to_final_ms"] = derived["stt_endpoint_to_final_ms"]
        dimensions["llm_visible_to_speakable_ms"] = derived["llm_visible_to_speakable_ms"]
        dimensions["tts_connection_wait_ms"] = derived["tts_connection_wait_ms"]
        dimensions["t_response_cue_sent_server"] = timer.server_timestamps.get(
            "t_response_cue_sent_server"
        )
        dimensions["t_client_response_cue_start_ms"] = timer.client_timestamps_ms.get(
            "t_client_response_cue_start_ms"
        )
        dimensions["response_cue_dispatch_ms"] = derived["response_cue_dispatch_ms"]
        dimensions["feedback_voice_to_voice_ms"] = derived["feedback_voice_to_voice_ms"]
        dimensions["answer_after_feedback_ms"] = derived["answer_after_feedback_ms"]
        values: dict[str, Any] = {
            **{
                name: value
                for name, value in timer.server_timestamps.items()
                if name != "t_response_cue_sent_server"
            },
            **{
                name: value
                for name, value in timer.client_timestamps_ms.items()
                if name != "t_client_response_cue_start_ms"
            },
            "last_speech_capture_seq": timer.last_speech_capture_seq,
            "last_speech_capture_time_ms": timer.last_speech_capture_time_ms,
            "endpoint_decision_ms": derived["endpoint_decision_ms"],
            "stt_flush_to_final_ms": derived["stt_flush_to_final_ms"],
            "stt_eot_ms": derived["stt_eot_ms"],
            "orchestrator_queue_ms": derived["orchestrator_queue_ms"],
            "llm_visible_ttft_ms": derived["llm_visible_ttft_ms"],
            "llm_first_speakable_ms": derived["llm_first_speakable_ms"],
            "tts_ttfb_ms": derived["tts_ttfb_ms"],
            "tts_connection_acquire_ms": derived["tts_connection_acquire_ms"],
            "client_decode_ms": derived["client_decode_ms"],
            "client_schedule_ms": derived["client_schedule_ms"],
            "downstream_to_playback_ms": derived["downstream_to_playback_ms"],
            "e2e_voice_to_voice_ms": derived["e2e_voice_to_voice_ms"],
            "barge_detection_ms": derived["barge_detection_ms"],
            "barge_client_flush_ms": derived["barge_client_flush_ms"],
            "barge_in_stop_ack_ms": derived["barge_in_stop_ack_ms"],
            "interrupted": dimensions.get("interrupted") is True,
            "balance_cutoff": dimensions.get("balance_cutoff") is True,
            "interrupted_audio_generated_ms": dimensions.get("interrupted_audio_generated_ms"),
            "interrupted_audio_played_ms": dimensions.get("interrupted_audio_played_ms"),
            "stt_model": self._settings.stt_model,
            "llm_model": self._settings.llm_model,
            "tts_model": self._settings.tts_model,
            "llm_reasoning_effort": self._settings.llm_reasoning_effort,
            "language_code": dimensions.get("language_code"),
            "language_confidence": dimensions.get("language_confidence"),
            "audio_codec": self._settings.stt_input_audio_codec,
            "input_sample_rate_hz": 16_000,
            "output_sample_rate_hz": self._settings.tts_sample_rate_hz,
            "frame_size_ms": FRAME_SAMPLES * 1000 / 16_000,
            "input_duration_ms": _provider_audio_duration_ms(timer.raw_provider_metrics),
            "tts_connection_state": "cold",
            "stt_request_id": dimensions.get("stt_request_id"),
            "tts_request_id": dimensions.get("tts_request_id"),
            "tool_used": bool(timer.tool_spans),
            "tool_names": list(dict.fromkeys(span["name"] for span in timer.tool_spans)) or None,
            "tool_spans": timer.tool_spans or None,
            "software_version": "0.1.0",
            "configuration_hash": self._configuration_hash(),
            "missing_timestamps": timer.missing_timestamps(),
            "censored": timer.censored,
            "exclusion_reason": (
                "cancelled_while_thinking"
                if dimensions.get("cancelled_while_thinking") is True
                else ("missing_timestamps" if timer.missing_timestamps() else None)
            ),
            "dimensions": dimensions,
            "raw_provider_metrics": timer.raw_provider_metrics,
        }
        # The response worker and browser receiver can persist one turn at once.
        async with self._persist_lock, self._database.session_factory() as session:
            result = await session.execute(
                select(TurnMetric).where(
                    TurnMetric.usage_session_id == self._session_id,
                    TurnMetric.turn_index == timer.turn_index,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = TurnMetric(
                    usage_session_id=self._session_id,
                    turn_index=timer.turn_index,
                )
                session.add(row)
            for name, value in values.items():
                setattr(row, name, value)
            await session.commit()
        self._persisted_turns.add(timer.turn_id)

    async def _start_usage_session(self) -> None:
        async with self._database.session_factory() as session:
            session.add(
                UsageSession(
                    id=self._session_id, user_id=self._user_id, started_at=self._clock.utc_now()
                )
            )
            await session.commit()

    async def _end_usage_session(self, reason: str, *, ended_at=None) -> None:
        async with self._database.session_factory() as session:
            await session.execute(
                update(UsageSession)
                .where(UsageSession.id == self._session_id, UsageSession.ended_at.is_(None))
                .values(ended_at=ended_at or self._clock.utc_now(), end_reason=reason)
            )
            await session.commit()

    async def _send_agent_state(
        self,
        state: str,
        *,
        transmitting: bool | None = None,
        detail: str | None = None,
    ) -> None:
        message: dict[str, Any] = {"type": "agent_state", "state": state}
        if transmitting is not None:
            message["transmitting"] = transmitting
            message["transport_status"] = (
                "transmitting_speech" if transmitting else "silence_not_transmitting"
            )
        if detail is not None:
            message["detail"] = detail
        elif transmitting is False:
            message["detail"] = "silence - not transmitting"
        await self._send_json(message)

    async def _send_listening_state(self) -> None:
        if self._endpointing_strategy == "local_vad":
            await self._send_agent_state("listening", transmitting=False)
        else:
            await self._send_agent_state(
                "listening",
                transmitting=True,
                detail="benchmark mode - forwarding all audio including silence",
            )

    async def _send_probe(
        self,
        service: str,
        direction: str,
        message: dict[str, Any],
    ) -> None:
        sanitized = sanitize_vendor_message(message)
        log_voice_event(
            logging.DEBUG,
            "probe_vendor_message",
            session_id=self._session_id,
            turn_id=self._current_turn_id(),
            service=service,
            direction=direction,
            message=sanitized,
        )
        await self._send_json(
            {
                "type": "probe_vendor_message",
                "service": service,
                "direction": direction,
                "message": sanitized,
            }
        )

    async def _send_json(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            if getattr(self, "_closing", False) and message.get("type") not in {
                "billing",
                "call_ended",
                "error",
            }:
                return
            if message.get("type") in {
                "agent_text",
                "audio_start",
                "response_cue",
                "final_transcript",
            }:
                turn_id = message.get("turn_id")
                timer = self._timers.get(turn_id)
                if (
                    timer is None
                    or self._turn_output_cancelled(turn_id)
                    or message.get("generation_id") != timer.dimensions.get("generation_id")
                ):
                    return
            await self._websocket.send_json(message)

    async def _send_turn_audio(self, turn_id: str, audio: bytes, *, generation_id: str) -> bool:
        async with self._send_lock:
            timer = self._timers.get(turn_id)
            if timer is None:
                return False
            sent = False
            for offset in range(0, len(audio), MAX_AUDIO_PAYLOAD_BYTES):
                if (
                    self._turn_output_cancelled(turn_id)
                    or timer.dimensions.get("generation_id") != generation_id
                ):
                    return sent
                sequence = timer.dimensions.get("audio_sequence", 0)
                frame = encode_output_audio(
                    {
                        "turn_id": turn_id,
                        "generation_id": generation_id,
                        "sequence": sequence,
                        "codec": "pcm_s16le",
                        "sample_rate_hz": self._settings.tts_sample_rate_hz,
                        "kind": "answer",
                    },
                    audio[offset : offset + MAX_AUDIO_PAYLOAD_BYTES],
                )
                timer.dimensions["audio_sequence"] = sequence + 1
                await self._websocket.send_bytes(frame)
                sent = True
            return sent

    def _turn_output_cancelled(self, turn_id: str) -> bool:
        return (
            getattr(self, "_closing", False)
            or turn_id in self._interrupted_turns
            or turn_id in self._cancelled_response_turns
        )

    def _configuration_hash(self) -> str:
        configuration = {
            "endpointing_strategy": self._endpointing_strategy,
            "vad_model": self._gate.vad_name if self._gate else None,
            "vad_end_silence_ms": self._settings.vad_end_silence_ms,
            "vad_pre_roll_ms": self._settings.vad_pre_roll_ms,
            "vad_speech_threshold": self._settings.vad_speech_threshold,
            "stt_model": self._settings.stt_model,
            "llm_model": self._settings.llm_model,
            "tts_model": self._settings.tts_model,
            "phrase_chunking_enabled": self._settings.phrase_chunking_enabled,
            "response_cues_enabled": self._settings.response_cues_enabled,
            "response_cue_delay_ms": self._settings.response_cue_delay_ms,
            "response_cue_cooldown_turns": self._settings.response_cue_cooldown_turns,
            "price_per_minute_paise": self._settings.price_per_minute_paise,
            "billing_tick_seconds": self._settings.billing_tick_seconds,
            "low_balance_warn_paise": self._settings.low_balance_warn_paise,
        }
        encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _provider_request_id_hash(value: Any) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _provider_audio_duration_ms(metrics: dict[str, Any] | None) -> float | None:
    if not metrics:
        return None
    duration = metrics.get("audio_duration")
    return float(duration) * 1000 if isinstance(duration, int | float) else None
