import pytest

from olf.infra.e2e._token_lifetime import MIN_LIFETIME_SECONDS, generation_count, shortened_lifetime_seconds


@pytest.mark.parametrize("baseline", [0, 5, 20])
def test_a_fast_baseline_still_gets_a_lifetime_a_dbt_start_up_fits_in(baseline: float) -> None:
    assert shortened_lifetime_seconds(baseline) == MIN_LIFETIME_SECONDS


def test_a_slow_baseline_is_outlasted_by_a_run_of_the_same_length() -> None:
    assert shortened_lifetime_seconds(120) < 120


def test_generations_follow_the_ref_graph(tmp_path) -> None:
    (tmp_path / "a.sql").write_text("select 1")
    (tmp_path / "b.sql").write_text("select * from {{ ref('a') }}")
    (tmp_path / "c.sql").write_text('select * from {{ ref("b") }} join {{ ref("a") }}')
    (tmp_path / "d.sql").write_text("select * from {{ source('silver', 'x') }}")

    assert generation_count(tmp_path) == 3


def test_mint_times_are_the_logged_mints_in_seconds() -> None:
    from olf.infra.e2e._token_lifetime import TOKEN_MINTED_MESSAGE, mint_times

    events = [
        {"message": TOKEN_MINTED_MESSAGE, "timestamp": "2000"},
        {"message": "something else", "timestamp": "1000"},
        {"message": TOKEN_MINTED_MESSAGE, "timestamp": "1500"},
    ]

    assert mint_times(events) == [1.5, 2.0]


def test_flat_models_are_one_generation(tmp_path) -> None:
    (tmp_path / "a.sql").write_text("select 1")
    (tmp_path / "b.sql").write_text("select 2")

    assert generation_count(tmp_path) == 1
