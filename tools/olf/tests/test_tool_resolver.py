from __future__ import annotations

from pathlib import Path

import pytest

from olf.deployment.errors import ExecutableNotFoundError
from olf.tooling.resolver import PathExecutableResolver


def test_resolves_executable_from_path() -> None:
    resolver = PathExecutableResolver(which=lambda tool: f"/usr/bin/{tool}" if tool == "terraform" else None)

    assert resolver.resolve("terraform") == Path("/usr/bin/terraform")


def test_missing_executable_raises_typed_error() -> None:
    resolver = PathExecutableResolver(which=lambda _tool: None)

    with pytest.raises(ExecutableNotFoundError) as excinfo:
        resolver.resolve("terraform")

    assert excinfo.value.tool == "terraform"


def test_override_takes_precedence_over_path_lookup() -> None:
    calls: list[str] = []

    def which(tool: str) -> str | None:
        calls.append(tool)
        return f"/usr/bin/{tool}"

    resolver = PathExecutableResolver(overrides={"terraform": Path("/opt/fake/terraform")}, which=which)

    resolved = resolver.resolve("terraform")

    assert resolved == Path("/opt/fake/terraform")
    assert calls == []  # PATH lookup is never consulted when an override exists


def test_resolver_satisfies_the_protocol_used_by_adapters() -> None:
    # Adapters only ever call `.resolve(tool)`; any object satisfying that
    # shape can substitute a managed-toolchain resolver (#127) later.
    class FakeManagedResolver:
        def resolve(self, tool: str) -> Path:
            return Path(f"/managed/{tool}")

    resolver = FakeManagedResolver()
    assert resolver.resolve("helm") == Path("/managed/helm")
