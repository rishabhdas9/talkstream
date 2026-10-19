from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.costs.models import CostRecord, Identifier, Money, WorkBound


class RateUnavailable(ValueError):
    pass


class UnitPrice(CostRecord):
    amount_micros: Money
    per_units: int = Field(gt=0, le=10**12)


class PriceAdjustment(CostRecord):
    numerator: int = Field(gt=0, le=10**12)
    denominator: int = Field(gt=0, le=10**12)
    note: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def includes_all_charges(self) -> PriceAdjustment:
        if self.numerator < self.denominator:
            raise ValueError("Rate adjustment cannot discount away fees or taxes")
        return self


class RateCard(CostRecord):
    version: Identifier
    captured_at: AwareDatetime
    source: str = Field(min_length=1, max_length=2000)
    currency: Literal["INR", "USD", "EUR"]
    fx_micro_inr: int = Field(gt=0, le=10**12)
    taxes_and_fees: PriceAdjustment
    unit_prices: dict[Identifier, dict[Identifier, dict[Identifier, UnitPrice]]]
    qualified: bool = False
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def inr_needs_no_conversion(self) -> RateCard:
        if self.currency == "INR" and self.fx_micro_inr != 1_000_000:
            raise ValueError("INR requires an identity currency conversion")
        return self

    def require_current(self, now: datetime) -> None:
        if not self.qualified or not self.captured_at <= now < self.valid_until:
            raise RateUnavailable("A current qualified rate card is required")


def estimate_micro_inr(provider: str, model: str, bound: WorkBound, card: RateCard) -> int:
    if not card.qualified:
        raise RateUnavailable("Rate card is not qualified")
    try:
        prices = card.unit_prices[provider][model]
        # A missing metric must not turn paid work into free work.
        total = sum(
            (
                Fraction(prices[unit].amount_micros * quantity, prices[unit].per_units)
                for unit, quantity in bound.units.items()
            ),
            Fraction(0),
        )
    except KeyError as exc:
        raise RateUnavailable("No qualified rate for provider/model/unit") from exc
    adjusted = (
        total
        * card.fx_micro_inr
        * card.taxes_and_fees.numerator
        / (1_000_000 * card.taxes_and_fees.denominator)
    )
    result = -(-adjusted.numerator // adjusted.denominator)
    if result > 9_000_000_000_000_000:
        raise RateUnavailable("Cost exceeds supported integer storage")
    return result
