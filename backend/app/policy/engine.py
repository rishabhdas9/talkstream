from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC
from typing import Any

import anyio

from app.costs.models import BudgetExceeded
from app.policy.models import (
    PolicyCategory,
    PolicyContext,
    PolicyDecision,
    PolicyDocument,
    PolicyEvent,
    PolicyStage,
    parse_policy_json,
)
from app.runtime import Clock
from app.voice.lifecycle import bounded_cleanup

# Sarvam models often wrap the JSON verdict in one Markdown fence; nothing else is unwrapped.
_FENCED_JSON = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n[ \t]*```\Z", re.DOTALL)


def _unfenced(raw: str) -> str:
    match = _FENCED_JSON.match(raw.strip())
    return match.group(1) if match else raw


PolicyClassifier = Callable[[PolicyStage, str, PolicyContext], Awaitable[Any]]
PolicyEventSink = Callable[[PolicyEvent], Awaitable[None]]
_logger = logging.getLogger(__name__)


def policy_reply(decision: PolicyDecision, language_code: str | None) -> str:
    replies = {
        "clarification": (
            "Please clarify what you want me to do.",
            "कृपया साफ़ बताइए कि आप क्या करवाना चाहते हैं।",
        ),
        "imminent_harm": (
            "If anyone is in immediate danger, contact local emergency services "
            "or a trusted person now.",
            "अगर किसी को तुरंत खतरा है, तो अभी स्थानीय आपात सेवा या किसी भरोसेमंद व्यक्ति से संपर्क करें।",
        ),
        "policy_unavailable": (
            "I couldn't safely check that response. Please try again.",
            "इस जवाब की सुरक्षित जाँच नहीं हो सकी। कृपया फिर कोशिश करें।",
        ),
    }
    english, hindi = replies.get(
        decision.category,
        (
            "I can't help with that request. Please ask something else.",
            "मैं इस अनुरोध में मदद नहीं कर सकता। कृपया कुछ और पूछिए।",
        ),
    )
    return hindi if language_code == "hi-IN" else english


def _normalized(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value)
        if not char.isspace() and unicodedata.category(char) != "Cf"
    )


def _inspection_text(value: str) -> str:
    parts = [value]
    try:
        # Retain duplicate fields too: untrusted references may encode or split a credential.
        pending = [json.loads(value, object_pairs_hook=lambda pairs: [v for p in pairs for v in p])]
    except ValueError:
        return _normalized(value)
    while pending:
        item = pending.pop()
        if type(item) is list:
            pending.extend(reversed(item))
        elif type(item) is str:
            parts.append(item)
    return _normalized("\n".join(parts))


class PolicyEngine:
    def __init__(
        self,
        *,
        policy: PolicyDocument,
        classifier: PolicyClassifier,
        event_sink: PolicyEventSink,
        clock: Clock,
        secrets: Collection[str] = (),
        hidden_instructions: Collection[str] = (),
    ):
        self.policy = PolicyDocument.model_validate(policy.model_dump(), strict=True)
        self._classifier, self._event_sink, self._clock = classifier, event_sink, clock
        self._secrets = tuple(filter(None, (_normalized(value) for value in secrets)))
        self._hidden = tuple(
            filter(None, (_normalized(value).casefold() for value in hidden_instructions))
        )

    async def inspect(
        self, stage: PolicyStage, text: str, context: PolicyContext
    ) -> PolicyDecision:
        start = self._clock.monotonic()
        decision = PolicyDecision(allowed=False, category="policy_unavailable")
        try:
            if stage not in ("input", "output") or context.policy_version != self.policy.version:
                raise ValueError("Unsupported policy context")
            if type(text) is not str:
                raise TypeError("Policy text must be a string")
            if (
                len(text) > self.policy.max_text_bytes
                or len(text.encode("utf-8")) > self.policy.max_text_bytes
            ):
                decision = PolicyDecision(allowed=False, category="text_limit")
            elif not text.strip():
                decision = PolicyDecision(
                    allowed=False,
                    category="clarification" if stage == "input" else "policy_unavailable",
                )
            else:
                normalized = _inspection_text(text)
                if any(value in normalized for value in self._secrets):
                    decision = PolicyDecision(allowed=False, category="credential_disclosure")
                elif any(value in normalized.casefold() for value in self._hidden):
                    decision = PolicyDecision(
                        allowed=False, category="hidden_instruction_disclosure"
                    )
                else:
                    async with asyncio.timeout(self.policy.classifier_timeout_seconds):
                        raw = await self._classifier(stage, text, context)
                    if type(raw) is str:
                        if len(raw) > 1024 or len(raw.encode("utf-8")) > 1024:
                            raise ValueError("Classifier response exceeds the limit")
                        raw = parse_policy_json(_unfenced(raw))
                    if type(raw) is not dict:
                        raise ValueError("Missing classifier decision")
                    decision = PolicyDecision.model_validate(raw, strict=True)
        except BudgetExceeded:
            raise
        except Exception:
            # Classifier failures and invalid decisions cannot relax an enforcement check.
            decision = PolicyDecision(allowed=False, category="policy_unavailable")
        finally:
            await self._record(stage, decision, context, start)
        return decision

    async def reject(
        self, stage: PolicyStage, category: PolicyCategory, context: PolicyContext
    ) -> PolicyDecision:
        decision = PolicyDecision(
            allowed=False,
            category=(
                category if context.policy_version == self.policy.version else "policy_unavailable"
            ),
        )
        await self._record(stage, decision, context, self._clock.monotonic())
        return decision

    async def _record(self, stage, decision, context, start):
        try:
            event = PolicyEvent(
                **decision.model_dump(),
                context=context,
                policy_version=self.policy.version,
                stage=stage,
                duration_ms=max(0.0, (self._clock.monotonic() - start) * 1000),
                occurred_at=self._clock.utc_now().astimezone(UTC),
            )
            with anyio.CancelScope(shield=True):
                await bounded_cleanup(self._event_sink(event), timeout_seconds=0.05)
        except Exception:
            # Event delivery is optional telemetry, separate from the enforcement result.
            _logger.warning("policy_telemetry_unavailable")
