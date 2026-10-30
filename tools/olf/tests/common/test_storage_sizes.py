from __future__ import annotations

from pathlib import Path

import pytest

from olf.common.profile import Size
from olf.common.storage_sizes import load_object_store_volumes, quantity_bytes

REPO_ROOT = Path(__file__).parents[4]


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [("1Gi", 2**30), ("20Gi", 20 * 2**30), ("2Ti", 2 * 2**40), ("500Mi", 500 * 2**20), ("5G", 5 * 10**9), ("42", 42)],
)
def test_quantities_read_the_way_kubernetes_reads_them(quantity: str, expected: int) -> None:
    assert quantity_bytes(quantity) == expected


@pytest.mark.parametrize("quantity", ["", "Gi", "1.5Gi", "-1Gi", "20 GB", "5m"])
def test_quantities_that_are_not_whole_storage_sizes_are_rejected(quantity: str) -> None:
    with pytest.raises(ValueError, match="storage quantity"):
        quantity_bytes(quantity)


def test_small_is_the_footprint_a_profile_without_storage_always_had() -> None:
    small = load_object_store_volumes(REPO_ROOT)[Size.SMALL]

    assert (small.master, small.filer, small.volume, small.max_volumes) == ("1Gi", "5Gi", "20Gi", 80)


def test_every_size_has_a_row_and_each_row_is_larger_than_the_one_before() -> None:
    rows = load_object_store_volumes(REPO_ROOT)
    assert set(rows) == set(Size)

    ordered = [rows[size] for size in (Size.SMALL, Size.MEDIUM, Size.LARGE)]
    for smaller, larger in zip(ordered, ordered[1:], strict=False):
        assert quantity_bytes(smaller.master) < quantity_bytes(larger.master)
        assert quantity_bytes(smaller.filer) < quantity_bytes(larger.filer)
        assert quantity_bytes(smaller.volume) < quantity_bytes(larger.volume)
        assert smaller.max_volumes < larger.max_volumes


@pytest.mark.parametrize("size", list(Size))
def test_the_volume_server_has_a_slot_for_every_gib_it_can_hold(size: Size) -> None:
    """A slot fills to 1000 MB, so fewer slots than GiB leaves part of the PVC unusable."""
    row = load_object_store_volumes(REPO_ROOT)[size]

    assert row.max_volumes >= quantity_bytes(row.volume) // 2**30


def test_a_malformed_table_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "release").mkdir()
    (tmp_path / "release/storage-sizes.yaml").write_text("sizes: {small: {object_store: {master: 1Gi}}}")

    with pytest.raises(ValueError, match="release/storage-sizes.yaml is malformed"):
        load_object_store_volumes(tmp_path)
