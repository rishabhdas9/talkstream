from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.addons import build_addon_registry
from app.analytics.router import router as analytics_router
from app.auth.email import ConsoleOtpSender, ResendOtpSender
from app.auth.router import router as auth_router
from app.config import Settings
from app.costs.rates import RateCard
from app.costs.service import CostService
from app.db import Database
from app.logging import configure_application_logging, install_token_log_redaction
from app.migrations import apply_migrations
from app.operations.router import router as operations_router
from app.policy.models import PolicyDocument
from app.retention import purge_expired, purge_periodically
from app.runtime import LocalUnlimitedPolicy, RuntimePolicy, SystemClock
from app.voice.admission import CallAdmission
from app.voice.lifecycle import database_owner, recover_abandoned_sessions
from app.voice.providers.factory import RECOGNITION
from app.voice.router import router as voice_router
from app.wallet.router import router as wallet_router
from app.wallet.service import WalletService


def create_app(settings: Settings | None = None) -> FastAPI:
    install_token_log_redaction()
    resolved_settings = settings or Settings()
    configure_application_logging(resolved_settings.log_level)
    content_policy = PolicyDocument.load(resolved_settings.content_policy_file)
    policy = (
        RuntimePolicy.model_validate_json(resolved_settings.policy_file.read_text())
        if resolved_settings.policy_file is not None
        else RuntimePolicy(profile=resolved_settings.runtime_profile)
    )
    if policy.profile != resolved_settings.runtime_profile:
        raise ValueError("Runtime policy profile does not match selected deployment profile")
    if resolved_settings.no_limits_enabled:
        policy = LocalUnlimitedPolicy.from_policy(policy)
    rates = (
        RateCard.model_validate_json(resolved_settings.rate_card_file.read_text())
        if resolved_settings.rate_card_file is not None
        else None
    )
    database = Database(
        resolved_settings.database_url,
        echo=resolved_settings.database_echo,
    )
    wallet_service = WalletService(
        database,
        # ₹1,00,000 a day locally, so the mock wallet never ends a call.
        daily_allowance_paise=10_000_000
        if resolved_settings.no_limits_enabled
        else resolved_settings.daily_allowance_paise,
        daily_topups=resolved_settings.daily_topups,
    )
    addon_registry = build_addon_registry(resolved_settings, database)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with database_owner(database):
            purge_task = None
            try:
                await apply_migrations(database)
                await recover_abandoned_sessions(database, app.state.clock, app.state.cost_service)
                await purge_expired(database, app.state.clock.utc_now(), policy)
                purge_task = asyncio.create_task(
                    purge_periodically(database, app.state.clock, policy)
                )
                async with httpx.AsyncClient(timeout=10.0) as http_client:
                    resend_key = resolved_settings.resend_api_key
                    # Local dev login never sends real email, even with a Resend key in .env.
                    if resend_key is None or resolved_settings.dev_login_enabled:
                        app.state.email_sender = ConsoleOtpSender()
                    else:
                        app.state.email_sender = ResendOtpSender(
                            api_key=resend_key.get_secret_value(),
                            sender=resolved_settings.resend_from,
                            client=http_client,
                        )
                    async with addon_registry.lifespan(app, http_client):
                        yield
            finally:
                if purge_task is not None:
                    purge_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await purge_task
                await database.dispose()

    app = FastAPI(
        title="Talkstream API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.clock = SystemClock()
    app.state.runtime_policy = policy
    app.state.content_policy = content_policy
    # Local mode keeps the per-call and global caps; the per-user day cap returns with hosting.
    app.state.cost_service = CostService(
        database,
        app.state.clock,
        policy,
        rates,
        user_day_limit=not resolved_settings.dev_login_enabled,
    )
    app.state.call_admission = (
        CallAdmission(
            database,
            app.state.clock,
            policy,
            app.state.cost_service,
            stt_provider=RECOGNITION[resolved_settings.stt_transport][0].provider,
            stt_model=resolved_settings.stt_model,
            # Local login shares one "user"; the daily count returns with hosting.
            daily_call_limit=not resolved_settings.dev_login_enabled,
        )
        if rates is not None
        else None
    )
    app.state.database = database
    app.state.wallet_service = wallet_service
    addon_registry.install(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth_router)
    app.include_router(wallet_router)
    app.include_router(voice_router)
    app.include_router(analytics_router)
    app.include_router(operations_router)

    @app.get("/capabilities", tags=["system"])
    async def capabilities() -> dict[str, object]:
        return {
            "features": addon_registry.capabilities,
            "dev_login": resolved_settings.dev_login_enabled,
            "speech": {
                "transport": resolved_settings.stt_transport,
                "model": resolved_settings.stt_model,
                "configured_language": resolved_settings.stt_language_code,
                "language_detection": resolved_settings.stt_language_code in {"unknown", "auto"},
                "experimental": resolved_settings.stt_transport != "sarvam_legacy",
            },
        }

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
