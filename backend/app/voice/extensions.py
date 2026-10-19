from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.tools import BaseTool


@dataclass(frozen=True)
class VoiceAgentExtension:
    tools: tuple[BaseTool, ...]
    system_prompt: str


VoiceExtensionFactory = Callable[[str], VoiceAgentExtension]
