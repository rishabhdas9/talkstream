from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Protocol

import httpx
from fastapi import APIRouter, FastAPI

from app.config import Settings
from app.db import Database
from app.voice.extensions import VoiceExtensionFactory


class AppAddon(Protocol):
    router: APIRouter
    voice_extension_factory: VoiceExtensionFactory

    def install(self, app: FastAPI) -> None: ...

    def lifespan(
        self,
        app: FastAPI,
        http_client: httpx.AsyncClient,
    ) -> AsyncIterator[None]: ...


class AddonRegistry:
    def __init__(
        self,
        *,
        capabilities: dict[str, bool],
        addons: list[AppAddon],
    ) -> None:
        self.capabilities = capabilities
        self._addons = addons

    def install(self, app: FastAPI) -> None:
        app.state.voice_extension_factories = tuple(
            addon.voice_extension_factory for addon in self._addons
        )
        for addon in self._addons:
            app.include_router(addon.router)
            addon.install(app)

    @asynccontextmanager
    async def lifespan(
        self,
        app: FastAPI,
        http_client: httpx.AsyncClient,
    ) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for addon in self._addons:
                await stack.enter_async_context(addon.lifespan(app, http_client))
            yield


def build_addon_registry(settings: Settings, database: Database) -> AddonRegistry:
    capabilities = {"knowledge_rag": settings.knowledge_rag_enabled}
    addons: list[AppAddon] = []
    if settings.knowledge_rag_enabled:
        from app.knowledge.addon import KnowledgeAddon

        addons.append(KnowledgeAddon(settings=settings, database=database))
    return AddonRegistry(capabilities=capabilities, addons=addons)
