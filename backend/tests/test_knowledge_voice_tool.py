from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from test_paid_boundaries import paid_boundary as paid_boundary

from app.costs.models import CostScope
from app.knowledge.schemas import KnowledgeSearchResult
from app.knowledge.voice import KNOWLEDGE_SYSTEM_PROMPT, build_knowledge_voice_extension
from app.voice.tools import ClientToolProxy, ExaSearchClient, build_agent_tools


async def _sender(message) -> None:
    del message


def _base_dependencies(boundary):
    return {
        "exa_client": ExaSearchClient(
            api_key=None,
            timeout_seconds=1,
            costs=boundary.costs,
            scope=boundary.scope,
            expires_at=boundary.costs.clock.utc_now() + timedelta(seconds=120),
        ),
        "todo_proxy": ClientToolProxy(sender=_sender, timeout_seconds=1),
    }


async def test_knowledge_tool_is_absent_when_feature_is_disabled(paid_boundary) -> None:
    dependencies = _base_dependencies(paid_boundary)
    tools = build_agent_tools(**dependencies)
    assert "knowledge_search" not in {tool.name for tool in tools}
    await dependencies["exa_client"].close()


async def test_knowledge_tool_returns_structured_server_citations() -> None:
    class Service:
        async def search_knowledge(self, *, user_id: str, query: str, limit: int, cost_scope):
            assert cost_scope == CostScope(user_id="user-1", session_id="session-1")
            assert user_id == "user-1"
            assert query == "refund timeline"
            assert limit == 4
            return [
                KnowledgeSearchResult(
                    chunk_id="chunk-1",
                    source_id="source-1",
                    source_name="Refund Policy",
                    excerpt="Approved refunds arrive within five working days.",
                    page_number=4,
                    score=0.91,
                )
            ]

    extension = build_knowledge_voice_extension(service=Service(), user_id="user-1")
    knowledge_tool = extension.tools[0]

    payload = json.loads(
        await knowledge_tool.coroutine(
            query="refund timeline",
            runtime=SimpleNamespace(
                tool_call_id="call-1", context=CostScope(user_id="user-1", session_id="session-1")
            ),
        )
    )

    assert payload["ok"] is True
    assert payload["results"][0]["citation"] == {
        "source_id": "source-1",
        "source_name": "Refund Policy",
        "page_number": 4,
    }
    assert payload["results"][0]["excerpt"].startswith("Approved refunds")


def test_voice_prompt_requires_grounded_spoken_citations() -> None:
    policy = KNOWLEDGE_SYSTEM_PROMPT.lower()
    assert "according to" in policy
    assert "never invent" in policy
    assert "no supporting result" in policy


async def test_knowledge_voice_tool_requires_trusted_call_scope():
    calls = []

    class Service:
        async def search_knowledge(self, **kwargs):
            calls.append(kwargs)
            return []

    extension = build_knowledge_voice_extension(service=Service(), user_id="user-1")
    payload = json.loads(
        await extension.tools[0].coroutine(
            query="refund", runtime=SimpleNamespace(tool_call_id="call-1", context=None)
        )
    )
    assert payload["ok"] is False
    assert calls == []


async def test_real_agent_supplies_server_call_scope_to_knowledge_tool(paid_boundary):
    from test_agent_receipts import ScriptedModel, collect

    from app.voice.agent import AgentRunner

    calls = []

    class Service:
        async def search_knowledge(self, **kwargs):
            calls.append(kwargs)
            return []

    boundary = paid_boundary
    extension = build_knowledge_voice_extension(service=Service(), user_id=boundary.scope.user_id)
    subject = AgentRunner(
        api_key="unused",
        base_url="https://unused.invalid/v1",
        model_name="sarvam-105b",
        thread_id="knowledge-scope",
        tools=list(extension.tools),
        costs=boundary.costs,
        scope=boundary.scope,
        expires_at=boundary.costs.clock.utc_now() + timedelta(seconds=120),
        chat_model=ScriptedModel(
            responses=["unused"],
            script=[
                [{"id": "search-1", "name": "knowledge_search", "args": {"query": "refund"}}],
                "No supporting document found.",
            ],
        ),
    )
    await collect(subject, transcript="Search my documents for refund rules")
    assert len(calls) == 1
    assert calls[0]["cost_scope"] == boundary.scope
