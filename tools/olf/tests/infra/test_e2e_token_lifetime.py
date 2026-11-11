import pytest

from olf.infra.e2e._token_lifetime import MIN_LIFETIME_SECONDS, generation_count, shortened_lifetime_seconds


def test_a_fast_build_still_gets_a_lifetime_a_dbt_start_up_fits_in() -> None:
    # Two 5 s generations: 1.5 x 5 s is under the floor.
    assert shortened_lifetime_seconds([0, 5], 10) == MIN_LIFETIME_SECONDS


def test_the_lifetime_covers_the_slowest_generation_with_headroom() -> None:
    # The example's customer_health on a CI runner: mints at 0 s and 18 s, last commit at 23 s.
    lifetime = shortened_lifetime_seconds([1000, 1018], 1023)
    assert lifetime is not None
    assert lifetime > 18


def test_no_lifetime_for_a_single_generation() -> None:
    # One process is the whole build: no pause between generations can stretch it.
    assert shortened_lifetime_seconds([1000], 1200) is None


def test_a_ref_in_a_comment_is_not_a_dependency(tmp_path) -> None:
    (tmp_path / "a.sql").write_text(
        "-- usage: {{ ref('a') }} and {{ ref('b') }}\n/* ref('b') */ select 1 {# ref('b') #}"
    )
    (tmp_path / "b.sql").write_text("select * from {{ ref('a') }}")

    assert generation_count(tmp_path) == 2


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


@pytest.mark.parametrize(
    ("implementation", "expected"),
    [
        ("catalog.iceberg_rest.polaris", True),
        ("catalog.iceberg_rest.polaris_on_aks", True),
        ("catalog.aws_glue", False),
        (None, False),
    ],
)
def test_the_check_runs_on_every_polaris_catalog_and_skips_glue(implementation: object, expected: bool) -> None:
    from olf.infra.e2e._token_lifetime import catalog_is_polaris

    assert catalog_is_polaris(implementation) is expected


def test_the_proof_uses_a_product_whose_gold_has_two_generations(tmp_path) -> None:
    from dataclasses import replace

    from conftest import E2E_INVENTORY, E2E_REPO_ROOT, e2e_cfg

    from olf.infra.e2e._token_lifetime import generation_count, proof_product

    cfg = replace(e2e_cfg(tmp_path), repo_root=E2E_REPO_ROOT)
    product = proof_product(cfg)

    # The example's default product builds Gold in one generation, so the proof needs another product.
    assert product is not None
    assert generation_count(cfg.repo_root / product.dbt_project_dir / "models") >= 2
    assert product.id in {p.id for p in E2E_INVENTORY.products}
