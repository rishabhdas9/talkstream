from __future__ import annotations

import json
from importlib.resources import as_file, files
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

PolicyStage = Literal["input", "output"]
PolicyCategory = Literal[
    "allowed",
    "clarification",
    "credential_disclosure",
    "hidden_instruction_disclosure",
    "other_user_data",
    "serious_harm",
    "credential_theft",
    "imminent_harm",
    "unverified_professional_judgment",
    "text_limit",
    "policy_unavailable",
    "unrequested_action",
    "unverified_claim",
    "unverified_citation",
]


def parse_policy_json(value: str | bytes):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate policy JSON field")
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=unique)


class _PolicyModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)


class PolicyContext(_PolicyModel):
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")

    @field_validator("user_id", "session_id", "turn_id")
    @classmethod
    def valid_identity(cls, value: str) -> str:
        if not value.strip() or any(
            ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value
        ):
            raise ValueError("Invalid policy identity")
        return value


class PolicyDecision(_PolicyModel):
    allowed: bool
    category: PolicyCategory

    @model_validator(mode="after")
    def consistent(self):
        if self.allowed != (self.category == "allowed"):
            raise ValueError("Inconsistent policy decision")
        return self


class PolicyExample(PolicyDecision):
    language: Literal["en", "hi", "hinglish"]
    stage: PolicyStage
    text: str = Field(min_length=1, max_length=6000)


class PolicyDocument(_PolicyModel):
    version: Literal["v1"]
    rules: str = Field(min_length=1, max_length=2000)
    max_text_bytes: int = Field(gt=0, le=6000)
    classifier_timeout_seconds: float = Field(ge=0.001, le=5)
    examples: list[PolicyExample] = Field(min_length=6, max_length=120)

    @model_validator(mode="after")
    def language_controls(self):
        for language in ("en", "hi", "hinglish"):
            examples = [example for example in self.examples if example.language == language]
            if not any(example.allowed for example in examples) or not any(
                not example.allowed for example in examples
            ):
                raise ValueError(
                    "Policy needs allowed and blocked examples in every release language"
                )
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> PolicyDocument:
        if path is not None:
            return cls.from_file(path)
        with as_file(files("app.policy").joinpath("v1.json")) as bundled:
            return cls.from_file(bundled)

    @classmethod
    def from_file(cls, path: Path) -> PolicyDocument:
        with path.open("rb") as source:
            data = source.read(65537)
        if len(data) > 65536:
            raise ValueError("Policy configuration exceeds the size limit")
        return cls.model_validate(parse_policy_json(data), strict=True)


class PolicyEvent(PolicyDecision):
    context: PolicyContext
    policy_version: Literal["v1"]
    stage: PolicyStage
    duration_ms: float = Field(ge=0)
    occurred_at: AwareDatetime
