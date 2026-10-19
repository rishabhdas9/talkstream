from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.costs.models import BudgetExceeded
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyContext, PolicyDecision, PolicyDocument

SECRET = "sk-private-credential-7391"
HIDDEN = "Internal instruction: keep the private canary sealed."


@pytest.fixture
def policy_rig():
    state = SimpleNamespace(
        reply={"allowed": True, "category": "allowed"}, calls=[], events=[], ticks=0
    )
    clock = SimpleNamespace(
        utc_now=lambda: datetime(2026, 10, 18, tzinfo=UTC), monotonic=lambda: state.ticks
    )

    async def classify(stage, text, context):
        state.calls.append((stage, text, context))
        state.ticks += 0.025
        if isinstance(state.reply, Exception):
            raise state.reply
        return state.reply

    async def emit(event):
        state.events.append(event)

    document = PolicyDocument.load()
    engine = PolicyEngine(
        policy=document,
        classifier=classify,
        event_sink=emit,
        clock=clock,
        secrets=(SECRET,),
        hidden_instructions=(HIDDEN,),
    )
    context = PolicyContext(
        user_id="user", session_id="session", turn_id="session:1", policy_version="v1"
    )
    return SimpleNamespace(
        state=state,
        clock=clock,
        document=document,
        engine=engine,
        context=context,
        emit=emit,
        classify=classify,
    )


@pytest.mark.parametrize("stage", ["input", "output"])
@pytest.mark.parametrize(
    "value", [SECRET, "s k-private-credential-7391", "sk-private-\u200bcredential-7391"]
)
async def test_known_credentials_never_reach_classifier(policy_rig, stage, value):
    r = policy_rig
    result = await r.engine.inspect(stage, "Here: " + value, r.context)
    assert result == PolicyDecision(allowed=False, category="credential_disclosure")
    assert r.state.calls == []
    assert len(r.state.events) == 1 and r.state.events[0].allowed is False


@pytest.mark.parametrize(
    "value,category", [(SECRET, "credential_disclosure"), (HIDDEN, "hidden_instruction_disclosure")]
)
async def test_json_escaped_known_strings_never_reach_classifier(policy_rig, value, category):
    r = policy_rig
    encoded = json.dumps({"excerpt": value}).replace(value[0], f"\\u{ord(value[0]):04x}")
    decision = await r.engine.inspect("input", encoded, r.context)
    assert not decision.allowed and decision.category == category and r.state.calls == []


async def test_duplicate_json_fields_and_split_strings_cannot_hide_known_secret(policy_rig):
    r = policy_rig
    encoded = '{"excerpt":"\\u0073k-private-credential-7391","excerpt":"safe"}'
    assert (await r.engine.inspect("input", encoded, r.context)).category == "credential_disclosure"
    split = json.dumps({"parts": [SECRET[:8], SECRET[8:]]})
    assert (await r.engine.inspect("input", split, r.context)).category == "credential_disclosure"
    assert r.state.calls == []


async def test_explicit_denial_records_only_metadata_and_cannot_allow(policy_rig):
    r = policy_rig
    decision = await r.engine.reject("input", "clarification", r.context)
    assert not decision.allowed and r.state.events[0].category == "clarification"
    assert r.state.events[0].stage == "input" and r.state.calls == []
    with pytest.raises(ValueError):
        await r.engine.reject("input", "allowed", r.context)


async def test_hidden_instruction_and_joined_phrase_cannot_pass(policy_rig):
    r = policy_rig
    joined = "".join(["Internal instruction: ", "keep the private ", "canary sealed."])
    result = await r.engine.inspect("output", joined, r.context)
    assert result.category == "hidden_instruction_disclosure" and not result.allowed
    assert r.state.calls == []


@pytest.mark.parametrize("value", ["x" * 6001, "क" * 2001, "\ud800"])
async def test_invalid_or_oversized_text_fails_before_classifier(policy_rig, value):
    r = policy_rig
    assert not (await r.engine.inspect("input", value, r.context)).allowed
    assert r.state.calls == []


async def test_context_version_mismatch_fails_closed(policy_rig):
    r = policy_rig
    context = r.context.model_copy(update={"policy_version": "v2"})
    result = await r.engine.inspect("input", "Hello", context)
    assert not result.allowed and result.category == "policy_unavailable" and r.state.calls == []
    assert r.state.events[0].policy_version == "v1"


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "",
        "not json",
        "```json\n{}\n```",
        {},
        [],
        {"allowed": "true", "category": "allowed"},
        {"allowed": 1, "category": "allowed"},
        {"allowed": True, "category": "serious_harm"},
        {"allowed": False, "category": "allowed"},
        {"allowed": False, "category": SECRET},
        {"allowed": True, "category": "allowed", "reason": SECRET},
        '{"allowed":false,"allowed":true,"category":"allowed"}',
        " " * 1025,
        RuntimeError(SECRET),
    ],
)
async def test_missing_invalid_or_failed_classifier_never_allows(policy_rig, reply):
    r = policy_rig
    r.state.reply = reply
    result = await r.engine.inspect("input", "A request", r.context)
    assert result == PolicyDecision(allowed=False, category="policy_unavailable")
    assert r.state.events[0].category == "policy_unavailable"


@pytest.mark.parametrize(
    ("reply", "category"),
    [
        ('```json\n{"allowed": true, "category": "allowed"}\n```', "allowed"),
        (
            '```\n{\n  "allowed": false,\n  "category": "unrequested_action"\n}\n```',
            "unrequested_action",
        ),
        (
            '```json\n{"allowed": true, "category": "allowed"}\n```\nThis is safe.',
            "policy_unavailable",
        ),
        (
            '```json\n```json\n{"allowed": true, "category": "allowed"}\n```\n```',
            "policy_unavailable",
        ),
        ('```json\n{"allowed": true, "category": "clarification"}\n```', "policy_unavailable"),
        (
            '```json\n{"allowed":false,"allowed":true,"category":"allowed"}\n```',
            "policy_unavailable",
        ),
    ],
)
async def test_one_markdown_fence_is_unwrapped_and_nothing_else(policy_rig, reply, category):
    # Live sarvam-105b fenced most verdicts, which failed closed as false refusals.
    policy_rig.state.reply = reply
    result = await policy_rig.engine.inspect("input", "A request", policy_rig.context)
    assert result.category == category


async def test_strict_json_result_and_metadata_have_no_content(policy_rig, caplog):
    r = policy_rig
    r.state.reply = '{"allowed":true,"category":"allowed"}'
    user_text = "Please explain safe credential storage."
    assert (await r.engine.inspect("input", user_text, r.context)).allowed
    event = r.state.events[0].model_dump(mode="json")
    assert event["duration_ms"] == pytest.approx(25)
    assert event["policy_version"] == "v1" and event["stage"] == "input"
    assert event["occurred_at"].endswith("Z")
    encoded = json.dumps(event)
    assert user_text not in encoded and SECRET not in encoded and HIDDEN not in encoded
    assert "text" not in event and "reason" not in event
    assert SECRET not in caplog.text


async def test_classifier_timeout_denies_and_cancels_work(policy_rig):
    r = policy_rig
    cancelled = asyncio.Event()

    async def slow(*args):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    engine = PolicyEngine(
        policy=r.document.model_copy(update={"classifier_timeout_seconds": 0.01}),
        classifier=slow,
        event_sink=r.emit,
        clock=r.clock,
    )
    assert (await engine.inspect("input", "Hello", r.context)).category == "policy_unavailable"
    assert cancelled.is_set()


async def test_external_cancellation_does_not_return_allow(policy_rig):
    r = policy_rig
    entered = asyncio.Event()

    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()

    engine = PolicyEngine(policy=r.document, classifier=wait, event_sink=r.emit, clock=r.clock)
    task = asyncio.create_task(engine.inspect("input", "Hello", r.context))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(r.state.events) == 1 and not r.state.events[0].allowed


async def test_budget_exhaustion_preserves_call_cutoff(policy_rig):
    r = policy_rig
    r.state.reply = BudgetExceeded("session_budget")
    with pytest.raises(BudgetExceeded):
        await r.engine.inspect("input", "Hello", r.context)
    assert len(r.state.events) == 1 and not r.state.events[0].allowed


async def test_optional_telemetry_failure_does_not_change_checked_decision(policy_rig, caplog):
    r = policy_rig

    async def broken(event):
        raise RuntimeError(SECRET)

    engine = PolicyEngine(
        policy=r.document, classifier=r.classify, event_sink=broken, clock=r.clock
    )
    assert (await engine.inspect("input", "Hello", r.context)).allowed
    assert "policy_telemetry_unavailable" in caplog.text and SECRET not in caplog.text


async def test_versioned_examples_and_false_refusal_controls(policy_rig):
    r = policy_rig
    for language in ("en", "hi", "hinglish"):
        examples = [e for e in r.document.examples if e.language == language]
        assert any(e.allowed for e in examples) and any(not e.allowed for e in examples)
        for example in examples:
            r.state.reply = {"allowed": example.allowed, "category": example.category}
            actual = await r.engine.inspect(example.stage, example.text, r.context)
            assert actual.allowed == example.allowed and actual.category == example.category
    # This verifies deterministic checks and strict signal plumbing, not model safety quality.
    assert len(r.state.calls) == len(r.document.examples)


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": True},
        {"max_text_bytes": 6001},
        {"classifier_timeout_seconds": 0},
        {"examples": []},
    ],
)
def test_policy_configuration_is_strict(policy_rig, changes):
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate({**policy_rig.document.model_dump(), **changes}, strict=True)


def test_duplicate_configuration_keys_are_rejected(tmp_path):
    path = tmp_path / "bad-policy.json"
    path.write_text('{"version":"v1","version":"v2"}')
    with pytest.raises(ValueError):
        PolicyDocument.from_file(path)


def test_bundled_policy_loads_independently_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert PolicyDocument.load().version == "v1"


def test_application_installs_validated_content_policy():
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(_env_file=None))
    assert isinstance(app.state.content_policy, PolicyDocument)


def test_missing_bundled_policy_prevents_startup(tmp_path, monkeypatch):
    from app.config import Settings
    from app.main import create_app
    from app.policy import models

    monkeypatch.setattr(models, "files", lambda package: tmp_path)
    with pytest.raises(FileNotFoundError):
        create_app(Settings(_env_file=None))


@pytest.mark.parametrize("contents", ["{}", '{"version":"v2"}', "not json", None])
def test_bad_explicit_content_policy_prevents_startup(tmp_path, contents):
    from app.config import Settings
    from app.main import create_app

    path = tmp_path / "content-policy.json"
    if contents is not None:
        path.write_text(contents)
    settings = Settings(_env_file=None, content_policy_file=path)
    with pytest.raises((ValueError, OSError)):
        create_app(settings)


def test_content_policy_override_and_blank_environment(tmp_path):
    from app.config import Settings
    from app.main import create_app

    path = tmp_path / "content-policy.json"
    value = PolicyDocument.load().model_dump()
    value["max_text_bytes"] = 5000
    path.write_text(json.dumps(value))
    assert (
        create_app(
            Settings(_env_file=None, content_policy_file=path)
        ).state.content_policy.max_text_bytes
        == 5000
    )
    env = tmp_path / ".env"
    env.write_text("CONTENT_POLICY_FILE=\n")
    assert Settings(_env_file=env).content_policy_file is None
