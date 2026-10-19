from __future__ import annotations

import pytest

from app.wallet.billing import (
    final_billable_seconds,
    full_elapsed_seconds,
    prorated_cost_paise,
)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, 0), (1, 3), (2, 7), (15, 50), (30, 100), (60, 200), (61, 203)],
)
def test_proration_uses_cumulative_half_up_rounding(seconds: int, expected: int) -> None:
    assert (
        prorated_cost_paise(
            billable_seconds=seconds,
            price_per_minute_paise=200,
        )
        == expected
    )


def test_sixty_seconds_equals_exact_minute_price() -> None:
    assert prorated_cost_paise(billable_seconds=60, price_per_minute_paise=199) == 199


def test_periodic_ticks_only_bill_full_seconds() -> None:
    assert full_elapsed_seconds(connected_at=10.0, now=10.999) == 0
    assert full_elapsed_seconds(connected_at=10.0, now=11.0) == 1


def test_final_partial_second_rounds_up() -> None:
    assert final_billable_seconds(connected_at=10.0, disconnected_at=10.001) == 1
    assert final_billable_seconds(connected_at=10.0, disconnected_at=11.001) == 2


def test_billing_clocks_reject_time_reversal() -> None:
    with pytest.raises(ValueError):
        full_elapsed_seconds(connected_at=2.0, now=1.0)
    with pytest.raises(ValueError):
        final_billable_seconds(connected_at=2.0, disconnected_at=1.0)
