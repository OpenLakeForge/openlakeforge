from __future__ import annotations

import pytest

from olf.toolchain.platform import Platform
from olf.toolchain.spec import ToolchainCatalogError, build_spec, load_specs

_PLATFORM = Platform(os="linux", arch="amd64")
_DIGEST = "sha256:" + "a" * 64


def _entry(version: str = "1.2.3") -> dict:
    return {"version": version, "platforms": {"linux-amd64": _DIGEST}}


@pytest.mark.parametrize(
    ("tool", "expected_url", "archive", "member"),
    [
        (
            "terraform",
            "https://releases.hashicorp.com/terraform/1.2.3/terraform_1.2.3_linux_amd64.zip",
            "zip",
            "terraform",
        ),
        ("helm", "https://get.helm.sh/helm-v1.2.3-linux-amd64.tar.gz", "tar.gz", "linux-amd64/helm"),
        ("kubectl", "https://dl.k8s.io/release/v1.2.3/bin/linux/amd64/kubectl", "raw", None),
        (
            "kind",
            "https://github.com/kubernetes-sigs/kind/releases/download/v1.2.3/kind-linux-amd64",
            "raw",
            None,
        ),
    ],
)
def test_build_spec_constructs_the_correct_url_and_archive_layout(
    tool: str, expected_url: str, archive: str, member: str | None
) -> None:
    spec = build_spec(tool, _entry(), platform=_PLATFORM)
    assert spec.url == expected_url
    assert spec.archive == archive
    assert spec.member == member
    assert spec.sha256 == _DIGEST
    assert spec.version == "1.2.3"


def test_build_spec_rejects_unmanaged_tool() -> None:
    with pytest.raises(ToolchainCatalogError):
        build_spec("docker", _entry(), platform=_PLATFORM)


def test_build_spec_requires_a_version() -> None:
    with pytest.raises(ToolchainCatalogError):
        build_spec("terraform", {"platforms": {"linux-amd64": _DIGEST}}, platform=_PLATFORM)


def test_build_spec_requires_the_platform_key() -> None:
    with pytest.raises(ToolchainCatalogError):
        build_spec("terraform", {"version": "1.2.3", "platforms": {"darwin-arm64": _DIGEST}}, platform=_PLATFORM)


def test_build_spec_rejects_a_malformed_digest() -> None:
    with pytest.raises(ToolchainCatalogError):
        build_spec("terraform", {"version": "1.2.3", "platforms": {"linux-amd64": "not-a-digest"}}, platform=_PLATFORM)


def test_load_specs_builds_every_managed_tool() -> None:
    catalog = {
        "components": {
            "toolchain": {
                "terraform": _entry("1.0.0"),
                "helm": _entry("2.0.0"),
                "kubectl": _entry("3.0.0"),
                "kind": _entry("4.0.0"),
            }
        }
    }
    specs = load_specs(catalog, platform=_PLATFORM)
    assert set(specs) == {"terraform", "helm", "kubectl", "kind"}
    assert specs["terraform"].version == "1.0.0"


def test_load_specs_rejects_a_catalog_missing_the_toolchain_block() -> None:
    with pytest.raises(ToolchainCatalogError):
        load_specs({"components": {}}, platform=_PLATFORM)


@pytest.mark.parametrize("malformed_components", [["a", "list"], "a scalar string", 42])
def test_load_specs_rejects_a_non_mapping_components_block(malformed_components: object) -> None:
    with pytest.raises(ToolchainCatalogError):
        load_specs({"components": malformed_components}, platform=_PLATFORM)


def test_load_specs_rejects_a_catalog_missing_one_tool() -> None:
    catalog = {
        "components": {
            "toolchain": {
                "terraform": _entry(),
                "helm": _entry(),
                "kubectl": _entry(),
            }
        }
    }
    with pytest.raises(ToolchainCatalogError, match="kind"):
        load_specs(catalog, platform=_PLATFORM)
