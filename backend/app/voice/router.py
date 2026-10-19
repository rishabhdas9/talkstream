from __future__ import annotations

import asyncio
import contextlib
import time

import anyio
from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketState

from app.auth.logging import log_auth_rejection
from app.auth.tickets import TicketService
from app.config import Settings
from app.costs.models import BudgetExceeded
from app.costs.rates import RateUnavailable
from app.voice.admission import CallLimitExceeded
from app.voice.lifecycle import bounded_cleanup
from app.voice.protocol import ProbeOptions
from app.voice.session import VoiceSession
from app.wallet.service import WalletService

router = APIRouter()

STT_CODECS = {"wav", "pcm_s16le", "pcm_l16", "pcm_raw"}
STT_ENCODINGS = {"audio/wav", "audio/pcm", "pcm_s16le", "pcm_l16", "pcm_raw"}
TTS_CODECS = {"pcm", "linear16", "wav", "mp3"}


@router.websocket("/ws/voice")
async def voice_socket(websocket: WebSocket) -> None:
    settings: Settings = websocket.app.state.settings
    if websocket.headers.get("origin") not in settings.cors_origin_list:
        log_auth_rejection(surface="voice_websocket", reason="invalid_origin", close_code=4401)
        await websocket.accept()
        await websocket.close(code=4401, reason="Invalid origin")
        return
    ticket = websocket.query_params.get("ticket")
    state = websocket.app.state
    grant = (
        await TicketService(state.database, state.clock, state.runtime_policy).consume(ticket)
        if ticket
        else None
    )
    if grant is None:
        log_auth_rejection(
            surface="voice_websocket",
            reason="invalid_ticket" if ticket else "missing_ticket",
            close_code=4401,
        )
        await websocket.accept()
        await websocket.close(code=4401, reason="Invalid or expired voice ticket")
        return
    websocket.state.voice_grant = grant

    wallet: WalletService = websocket.app.state.wallet_service
    starting_balance = await wallet.balance(grant.user_id)
    if starting_balance < settings.min_voice_balance_paise:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "code": "insufficient_balance",
                "message": (
                    f"A balance of at least {settings.min_voice_balance_paise} paise is required."
                ),
                "balance_paise": starting_balance,
            }
        )
        await websocket.close(code=4402, reason="Insufficient balance - recharge required")
        return

    admission = state.call_admission
    try:
        if admission is None:
            raise RateUnavailable("Voice spending configuration is unavailable")
        lease = await admission.admit(
            grant.user_id, grant.session_id, access_expires_at=grant.access_expires_at
        )
    except (CallLimitExceeded, BudgetExceeded, RateUnavailable) as exc:
        limited = isinstance(exc, CallLimitExceeded)
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "code": "call_limit_exceeded" if limited else "provider_budget_unavailable",
                "message": "Call limit reached. Try again later."
                if limited
                else "Voice is temporarily unavailable within its spending limits.",
            }
        )
        await websocket.close(code=4404 if limited else 4405)
        return
    websocket.state.admission_lease = lease
    websocket.state.cost_service = state.cost_service
    websocket.state.call_admission = admission
    probe_options = _probe_options(websocket, settings)
    connected_at = time.monotonic()
    session = None
    try:
        await websocket.accept()
        session_factory = getattr(websocket.app.state, "voice_session_factory", VoiceSession)
        extension_factories = getattr(
            websocket.app.state,
            "voice_extension_factories",
            (),
        )
        session = session_factory(
            websocket=websocket,
            settings=settings,
            probe_options=probe_options,
            database=websocket.app.state.database,
            user_id=grant.user_id,
            wallet=wallet,
            starting_balance_paise=starting_balance,
            connected_at=connected_at,
            agent_extensions=[factory(grant.user_id) for factory in extension_factories],
        )
        seconds = max(0, (lease.expires_at - state.clock.utc_now()).total_seconds())
        async with asyncio.timeout(seconds):
            await session.run()
    except (BudgetExceeded, RateUnavailable):
        await _terminal_error(
            websocket,
            {
                "type": "error",
                "code": "provider_budget_exhausted",
                "message": "Call stopped: the AI spending allowance is unavailable.",
            },
            code=4405,
            reason="Provider allowance unavailable",
        )
    except TimeoutError:
        await _terminal_error(
            websocket,
            {"type": "error", "code": "session_expired", "message": "Call time limit reached."},
            code=4406,
            reason="Call time limit reached",
        )
    except Exception:
        await _terminal_error(
            websocket,
            {
                "type": "error",
                "code": "voice_session_error",
                "message": "Voice session failed",
            },
            code=1011,
        )
    finally:
        # ASGI cancellation must not skip shared finalization.
        with anyio.CancelScope(shield=True):
            try:
                lifecycle = getattr(session, "lifecycle", None)
                if lifecycle is not None:
                    await lifecycle.finish("route_ended")
                else:
                    await bounded_cleanup(admission.release(lease.session_id))
            except Exception:
                # Unknown cleanup retains its lease and spend until safe recovery.
                pass
            finally:
                if websocket.application_state is WebSocketState.CONNECTED:
                    with contextlib.suppress(Exception):
                        await bounded_cleanup(websocket.close())


async def _terminal_error(websocket: WebSocket, message: dict, *, code: int, reason: str = ""):
    async def notify():
        await websocket.send_json(message)
        await websocket.close(code=code, reason=reason)

    with contextlib.suppress(Exception):
        await bounded_cleanup(notify(), timeout_seconds=0.2)


def _probe_options(websocket: WebSocket, settings: Settings) -> ProbeOptions:
    enabled = (
        settings.app_env == "development"
        and websocket.query_params.get("probe", "false").lower() == "true"
    )
    if not enabled:
        return ProbeOptions()
    return ProbeOptions(
        enabled=True,
        stt_input_audio_codec=_allowed_query(websocket, "stt_codec", STT_CODECS),
        stt_audio_encoding=_allowed_query(websocket, "stt_encoding", STT_ENCODINGS),
        tts_output_audio_codec=_allowed_query(websocket, "tts_codec", TTS_CODECS),
        endpointing_strategy=_allowed_query(
            websocket, "endpointing_strategy", {"local_vad", "sarvam"}
        ),
    )


def _allowed_query(websocket: WebSocket, name: str, allowed: set[str]) -> str | None:
    value = websocket.query_params.get(name)
    return value if value in allowed else None
