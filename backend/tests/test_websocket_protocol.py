from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth.tokens import encode_access_token
from app.config import Settings
from app.main import create_app
from app.voice.protocol import validate_client_message

USER_ID = "00000000-0000-0000-0000-000000000001"
EMAIL = "person@example.com"


def build_app(tmp_path: Path, *, balance_paise: int, session_factory=None, configure_rates=True):
    database_path = tmp_path / "ws.db"
    rate_file = tmp_path / "rates.json"
    now = datetime.now(UTC)
    rate_file.write_text(
        json.dumps(
            {
                "version": "synthetic-ws-fixture",
                "captured_at": now.isoformat(),
                "valid_until": (now + timedelta(days=1)).isoformat(),
                "qualified": True,
                "source": "https://example.com/fixture",
                "currency": "INR",
                "fx_micro_inr": 1_000_000,
                "taxes_and_fees": {"numerator": 1, "denominator": 1, "note": "Synthetic test only"},
                "unit_prices": {
                    "sarvam": {
                        "saaras:v3": {
                            "audio_ms": {"amount_micros": 1_000_000, "per_units": 120_000}
                        }
                    }
                },
            }
        )
    )
    settings = Settings(
        _env_file=None,  # A developer's live backend/.env must never reach tests.
        jwt_secret="test-secret-that-is-at-least-thirty-two-characters",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        sarvam_api_key=None,
        exa_api_key=None,
        resend_api_key=None,
        min_voice_balance_paise=1,
        daily_allowance_paise=balance_paise,  # the daily wallet is funded by its allowance
        rate_card_file=rate_file if configure_rates else None,
    )
    app = create_app(settings)
    asyncio.run(app.state.database.create_all())
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users (id, email, created_at, is_verified) VALUES (?, ?, ?, 1)",
            (USER_ID, EMAIL, "2026-01-01 00:00:00"),
        )
    if session_factory is not None:
        app.state.voice_session_factory = session_factory
    token = encode_access_token(
        user_id=USER_ID,
        email=EMAIL,
        secret=settings.jwt_secret_value,
        algorithm=settings.jwt_algorithm,
        ttl_seconds=60,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    return app, token


class InterruptScriptedSession:
    def __init__(self, *, websocket: WebSocket, **kwargs) -> None:
        del kwargs
        self.websocket = websocket

    async def run(self) -> None:
        await self.websocket.send_json(
            {
                "type": "ready",
                "session_id": "session-1",
                "balance_paise": 500,
                "price_per_minute_paise": 200,
                "tts_sample_rate_hz": 24000,
            }
        )
        await self.websocket.send_json({"type": "interrupt", "turn_id": "turn-1"})
        acknowledgement = validate_client_message(await self.websocket.receive_json())
        assert acknowledgement["type"] == "interrupt_ack"
        await self.websocket.send_json(
            {
                "type": "interrupt_resolved",
                "turn_id": "turn-1",
                "barge_in_stop_ack_ms": 20.0,
            }
        )
        await self.websocket.close(code=1000)


class ExhaustionScriptedSession:
    def __init__(self, *, websocket: WebSocket, **kwargs) -> None:
        del kwargs
        self.websocket = websocket

    async def run(self) -> None:
        await self.websocket.send_json(
            {
                "type": "billing",
                "seconds": 2,
                "cost_paise": 5,
                "balance_paise": 0,
                "low_balance": True,
                "final": True,
                "terminated_reason": "balance_exhausted",
            }
        )
        await self.websocket.send_json({"type": "call_ended", "reason": "balance_exhausted"})
        await self.websocket.close(code=4403, reason="Balance exhausted")


def ticket_url(client, token):
    response = client.post("/auth/voice-ticket", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return "/ws/voice?ticket=" + response.json()["ticket"]


ORIGIN = {"Origin": "http://localhost:5173"}


def test_ws_rejects_below_minimum_balance_with_4402(tmp_path: Path) -> None:
    app, token = build_app(tmp_path, balance_paise=0)

    with (
        TestClient(app) as client,
        client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as websocket,
    ):
        error = websocket.receive_json()
        assert error["code"] == "insufficient_balance"
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()

    assert closed.value.code == 4402


def test_ws_never_accepts_a_jwt_in_the_query_and_logs_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, token = build_app(tmp_path, balance_paise=500)

    monkeypatch.setattr(logging.getLogger("talkstream"), "propagate", True)
    with (
        caplog.at_level(logging.WARNING, logger="talkstream.auth"),
        TestClient(app) as client,
        client.websocket_connect(f"/ws/voice?token={token}", headers=ORIGIN) as websocket,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        websocket.receive_text()

    assert closed.value.code == 4401
    payloads = [json.loads(record.message) for record in caplog.records]
    assert {
        "event": "auth_rejected",
        "surface": "voice_websocket",
        "reason": "missing_ticket",
        "close_code": 4401,
    } in payloads
    assert token not in caplog.text


def test_interrupt_and_interrupt_ack_sequence(tmp_path: Path) -> None:
    app, token = build_app(
        tmp_path,
        balance_paise=500,
        session_factory=InterruptScriptedSession,
    )

    with (
        TestClient(app) as client,
        client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as websocket,
    ):
        assert websocket.receive_json()["type"] == "ready"
        interrupt = websocket.receive_json()
        assert interrupt == {"type": "interrupt", "turn_id": "turn-1"}
        websocket.send_json(
            {
                "type": "interrupt_ack",
                "turn_id": "turn-1",
                "interrupt_received_perf_ms": 10.0,
                "queue_cleared_perf_ms": 11.0,
                "audio_queue_cleared": True,
            }
        )
        assert websocket.receive_json()["type"] == "interrupt_resolved"
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()

    assert closed.value.code == 1000


def test_mid_call_exhaustion_closes_with_4403(tmp_path: Path) -> None:
    app, token = build_app(
        tmp_path,
        balance_paise=5,
        session_factory=ExhaustionScriptedSession,
    )

    with (
        TestClient(app) as client,
        client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as websocket,
    ):
        billing = websocket.receive_json()
        assert billing["terminated_reason"] == "balance_exhausted"
        assert billing["balance_paise"] == 0
        assert websocket.receive_json() == {
            "type": "call_ended",
            "reason": "balance_exhausted",
        }
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()

    assert closed.value.code == 4403


@pytest.mark.parametrize("origin", [None, "https://attacker.example", "null"])
def test_ws_rejects_origin_before_consuming_ticket(tmp_path, origin):
    app, token = build_app(tmp_path, balance_paise=0)
    with TestClient(app) as client:
        url = ticket_url(client, token)
        with client.websocket_connect(
            url, headers={} if origin is None else {"Origin": origin}
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401
            assert closed.value.reason == "Invalid origin"
        # A hostile handshake cannot spend the legitimate user's one-use ticket.
        with client.websocket_connect(url, headers=ORIGIN) as ws:
            assert ws.receive_json()["code"] == "insufficient_balance"
        with client.websocket_connect(url, headers=ORIGIN) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401


def test_ws_passes_ticket_identity_to_the_session(tmp_path):
    class Session:
        def __init__(self, *, websocket, user_id, **kwargs):
            self.websocket = websocket
            self.user = user_id

        async def run(self):
            grant = self.websocket.state.voice_grant
            assert self.user == grant.user_id == USER_ID
            await self.websocket.send_json({"session_id": grant.session_id})
            await self.websocket.close()

    app, token = build_app(tmp_path, balance_paise=500, session_factory=Session)
    with TestClient(app) as client:
        url = ticket_url(client, token)
        with client.websocket_connect(url, headers=ORIGIN) as ws:
            assert ws.receive_json()["session_id"]


def test_socket_query_secrets_are_redacted():
    from app.logging import RedactTokenQueryFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s",
        ("/ws/voice?ticket=secret-one&token=secret-two&TICKET=secret-three",),
        None,
    )
    RedactTokenQueryFilter().filter(record)
    assert "secret-" not in record.getMessage()


def test_real_voice_session_uses_ticket_session_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.auth.tickets import TicketGrant
    from app.voice import session as implementation
    from app.voice.protocol import ProbeOptions

    app, _ = build_app(tmp_path, balance_paise=500)
    from pydantic import SecretStr

    app.state.settings.sarvam_api_key = SecretStr("unused-offline-key")
    for name in ("ExaSearchClient", "AgentRunner"):
        monkeypatch.setattr(implementation, name, lambda *args, **kwargs: SimpleNamespace())
    legacy = implementation.RECOGNITION["sarvam_legacy"]
    fake = (lambda *args, **kwargs: SimpleNamespace(), legacy[1])
    monkeypatch.setitem(implementation.RECOGNITION, "sarvam_legacy", fake)
    websocket = WebSocket({"type": "websocket"}, receive=None, send=None)
    websocket.state.voice_grant = TicketGrant(
        USER_ID, "reserved-session", datetime.now(UTC) + timedelta(minutes=1)
    )
    from app.voice.admission import AdmissionLease

    websocket.state.admission_lease = AdmissionLease(
        "reserved-session", websocket.state.voice_grant.access_expires_at, "fixture-envelope"
    )
    websocket.state.cost_service = app.state.cost_service
    websocket.state.call_admission = app.state.call_admission
    voice = implementation.VoiceSession(
        websocket=websocket,
        settings=app.state.settings,
        probe_options=ProbeOptions(),
        database=app.state.database,
        user_id=USER_ID,
        wallet=app.state.wallet_service,
        starting_balance_paise=500,
        connected_at=0,
    )
    assert voice._session_id == "reserved-session"


class HoldingSession:
    constructed = 0

    def __init__(self, *, websocket, **kwargs):
        type(self).constructed += 1
        self.websocket = websocket

    async def run(self):
        await self.websocket.send_json({"type": "test_ready"})
        with contextlib.suppress(WebSocketDisconnect):
            await self.websocket.receive_json()


def test_missing_rates_prevent_session_construction(tmp_path):
    HoldingSession.constructed = 0
    app, token = build_app(
        tmp_path, balance_paise=500, session_factory=HoldingSession, configure_rates=False
    )
    with (
        TestClient(app) as client,
        client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as ws,
    ):
        assert ws.receive_json()["code"] == "provider_budget_unavailable"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4405


assert HoldingSession.constructed == 0


def test_user_slot_limit_precedes_session_construction(tmp_path):
    HoldingSession.constructed = 0
    app, token = build_app(tmp_path, balance_paise=500, session_factory=HoldingSession)
    with (
        TestClient(app) as client,
        client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as first,
    ):
        assert first.receive_json()["type"] == "test_ready"
        with client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as second:
            assert second.receive_json()["code"] == "call_limit_exceeded"
            with pytest.raises(WebSocketDisconnect) as closed:
                second.receive_json()
            assert closed.value.code == 4404
        assert HoldingSession.constructed == 1
        first.send_json({"type": "end_call"})


def test_failed_session_constructor_releases_lease_and_unused_envelope(tmp_path):
    def fail(**kwargs):
        raise RuntimeError("private initialization detail")

    app, token = build_app(tmp_path, balance_paise=500, session_factory=fail)
    with TestClient(app) as client:
        with client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as ws:
            assert ws.receive_json()["message"] == "Voice session failed"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1011

        async def released():
            for _ in range(100):
                if await app.state.call_admission.active_count() == 0:
                    return
                await asyncio.sleep(0.01)
            pytest.fail("Initialization left an active call lease")

        client.portal.call(released)
        with sqlite3.connect(tmp_path / "ws.db") as conn:
            assert conn.execute("SELECT SUM(held_micro_inr) FROM cost_periods").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM usage_sessions").fetchone()[0] == 0


def test_expired_ticket_cannot_consume_a_slot_or_construct_session(tmp_path):
    HoldingSession.constructed = 0
    app, token = build_app(tmp_path, balance_paise=500, session_factory=HoldingSession)
    from types import SimpleNamespace

    now = datetime.now(UTC)
    app.state.clock = SimpleNamespace(utc_now=lambda: now, monotonic=lambda: 0)
    with TestClient(app) as client:
        url = ticket_url(client, token)
        now += timedelta(seconds=31)
        with client.websocket_connect(url, headers=ORIGIN) as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4401
        assert HoldingSession.constructed == 0
        assert client.portal.call(app.state.call_admission.active_count) == 0


def test_global_slots_precede_all_session_constructors(tmp_path):
    HoldingSession.constructed = 0
    app, first_token = build_app(tmp_path, balance_paise=500, session_factory=HoldingSession)
    settings = app.state.settings
    other_tokens = []
    for user in ("second", "third"):
        with sqlite3.connect(tmp_path / "ws.db") as conn:
            conn.execute(
                "INSERT INTO users(id,email,created_at,is_verified) VALUES (?,?,?,1)",
                (user, f"{user}@example.com", "2026-01-01 00:00:00"),
            )
            conn.execute(
                "INSERT INTO wallet_transactions(id,user_id,amount_paise,kind,created_at) "
                "VALUES (?,?,500,'topup',?)",
                (f"topup-{user}", user, "2026-01-01 00:00:00"),
            )
        other_tokens.append(
            encode_access_token(
                user_id=user,
                email=f"{user}@example.com",
                secret=settings.jwt_secret_value,
                algorithm=settings.jwt_algorithm,
                ttl_seconds=60,
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
            )
        )
    with TestClient(app) as client:
        with client.websocket_connect(ticket_url(client, first_token), headers=ORIGIN) as first:
            assert first.receive_json()["type"] == "test_ready"
            with client.websocket_connect(
                ticket_url(client, other_tokens[0]), headers=ORIGIN
            ) as second:
                assert second.receive_json()["type"] == "test_ready"
                with client.websocket_connect(
                    ticket_url(client, other_tokens[1]), headers=ORIGIN
                ) as third:
                    assert third.receive_json()["code"] == "call_limit_exceeded"
                    with pytest.raises(WebSocketDisconnect) as closed:
                        third.receive_json()
                    assert closed.value.code == 4404
                assert HoldingSession.constructed == 2
                second.send_json({"type": "end_call"})
            first.send_json({"type": "end_call"})
        assert client.portal.call(app.state.call_admission.active_count) == 0


def test_route_stops_the_session_at_the_admitted_deadline(tmp_path):
    from app.runtime import RuntimePolicy

    class SlowSession:
        def __init__(self, *, websocket, **kwargs):
            self.websocket = websocket

        async def run(self):
            await self.websocket.send_json({"type": "test_ready"})
            await asyncio.sleep(1.2)
            await self.websocket.send_json({"type": "unexpected_late_work"})

    app, token = build_app(tmp_path, balance_paise=500, session_factory=SlowSession)
    policy = RuntimePolicy(session_seconds=1)
    app.state.runtime_policy = policy
    app.state.cost_service.policy = policy
    app.state.call_admission.policy = policy
    with (
        TestClient(app) as client,
        client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as ws,
    ):
        assert ws.receive_json()["type"] == "test_ready"
        assert ws.receive_json()["code"] == "session_expired"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4406


def test_provider_budget_exhaustion_explains_cutoff_and_releases_slot(tmp_path):
    from app.costs.models import BudgetExceeded

    class ExhaustedSession:
        def __init__(self, **kwargs):
            pass

        async def run(self):
            raise BudgetExceeded("private provider accounting detail")

    app, token = build_app(tmp_path, balance_paise=500, session_factory=ExhaustedSession)
    with TestClient(app) as client:
        with client.websocket_connect(ticket_url(client, token), headers=ORIGIN) as ws:
            result = ws.receive_json()
            assert result["code"] == "provider_budget_exhausted"
            assert "allowance" in result["message"]
            assert "private" not in result["message"]
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 4405
        assert client.portal.call(app.state.call_admission.active_count) == 0
