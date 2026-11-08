from __future__ import annotations

import pytest

from olf.common.edge import EDGE_RESOLVE_ENV, pinned_address


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("example.test", "127.0.0.1"),
        ("auth.example.test", "127.0.0.1"),
        ("AUTH.Example.Test.", "127.0.0.1"),
        ("notexample.test", None),
        ("example.org", None),
    ],
)
def test_only_the_base_domain_and_its_subdomains_are_pinned(host: str, expected: str | None) -> None:
    assert pinned_address(host, {EDGE_RESOLVE_ENV: "example.test=127.0.0.1"}) == expected


@pytest.mark.parametrize("spec", ["", "example.test", "=127.0.0.1"])
def test_nothing_is_pinned_without_a_domain_and_an_address(spec: str) -> None:
    assert pinned_address("example.test", {EDGE_RESOLVE_ENV: spec}) is None
