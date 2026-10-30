import re
from pathlib import Path

ROOT = Path(__file__).parents[3]


def test_catalog_declares_release_and_lockfiles() -> None:
    catalog = (ROOT / "release/component-catalog.yaml").read_text()
    assert "kind: ComponentCatalog" in catalog
    assert re.search(r"version: \d+\.\d+\.\d+-alpha\.\d+", catalog)
    assert (ROOT / "images/project-code/requirements.lock").stat().st_size > 0


def test_container_and_action_pin_patterns_accept_immutable_inputs() -> None:
    assert re.search(r"@sha256:[0-9a-f]{64}", "python:3.12-slim@sha256:" + "a" * 64)
    assert re.fullmatch(r"[0-9a-f]{40}", "a" * 40)


def test_unpinned_inputs_are_rejected_by_pin_patterns() -> None:
    assert not re.search(r"@sha256:[0-9a-f]{64}", "python:3.12-slim")
    assert not re.fullmatch(r"[0-9a-f]{40}", "actions/checkout@v4")
