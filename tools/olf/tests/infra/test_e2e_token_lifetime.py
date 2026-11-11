import pytest

from olf.infra.e2e._token_lifetime import MIN_LIFETIME_SECONDS, shortened_lifetime_seconds


@pytest.mark.parametrize("baseline", [0, 5, 20])
def test_a_fast_baseline_still_gets_a_lifetime_a_dbt_start_up_fits_in(baseline: float) -> None:
    assert shortened_lifetime_seconds(baseline) == MIN_LIFETIME_SECONDS


def test_a_slow_baseline_is_outlasted_by_a_run_of_the_same_length() -> None:
    assert shortened_lifetime_seconds(120) < 120
