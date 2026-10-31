from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from libs import duckdb_extensions  # noqa: E402


def test_floe_merge_extensions_include_the_one_iceberg_loads_itself() -> None:
    assert set(duckdb_extensions.EXTENSIONS) == {"httpfs", "aws", "iceberg", "avro"}


def test_verify_fails_naming_the_extension_rather_than_downloading_it(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="install the required extension .aws."):
        duckdb_extensions.verify(str(tmp_path))

    assert not any(tmp_path.rglob("*.duckdb_extension"))


@pytest.mark.parametrize("argv", [[], ["load"], ["verify", "a", "b"]])
def test_the_command_rejects_unknown_usage(argv: list[str]) -> None:
    with pytest.raises(SystemExit, match="usage"):
        duckdb_extensions.main(argv)
