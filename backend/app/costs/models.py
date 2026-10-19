from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
Units = Annotated[int, Field(ge=0, le=10**12)]
Money = Annotated[int, Field(ge=0, le=9_000_000_000_000_000)]


class CostRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


class CostScope(CostRecord):
    user_id: Identifier
    session_id: Identifier | None = None
    job_id: Identifier | None = None

    @model_validator(mode="after")
    def require_one_owner(self) -> CostScope:
        if (self.session_id is None) == (self.job_id is None):
            raise ValueError("Exactly one session or non-call job is required")
        return self


class WorkBound(CostRecord):
    units: dict[Identifier, Units] = Field(min_length=1, max_length=16)


class Usage(WorkBound):
    provider_request_id: Identifier | None = None


class Reservation(CostRecord):
    id: Identifier
    maximum_micro_inr: Money


class BudgetExceeded(ValueError):
    pass


class CostAccountingError(BudgetExceeded):
    """Unreconciled provider evidence blocks all new paid work."""
