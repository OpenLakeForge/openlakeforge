from __future__ import annotations

from pathlib import Path

from olf.common.commands.checks import _resources

_BARE = """\
resource "kubernetes_job_v1" "x" {
  spec {
    template {
      spec {
        container {
          name  = "bootstrap"
          image = "busybox"
        }
      }
    }
  }
}
"""

_SIZED = _BARE.replace(
    '          image = "busybox"\n',
    '          image = "busybox"\n\n          resources {\n            requests = {\n              cpu    = "10m"\n'
    '              memory = "32Mi"\n            }\n            limits = {\n              memory = "64Mi"\n'
    "            }\n          }\n",
)


def _module(tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "infra/terraform/modules/demo/main.tf"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return _resources.resource_errors(tmp_path)


def test_container_without_resources_is_rejected(tmp_path: Path) -> None:
    errors = _module(tmp_path, _BARE)

    assert len(errors) == 1
    assert "'bootstrap' lacks resources" in errors[0]


def test_container_with_requests_and_memory_limit_passes(tmp_path: Path) -> None:
    assert _module(tmp_path, _SIZED) == []


def test_container_without_memory_limit_or_memory_request_is_rejected(tmp_path: Path) -> None:
    unsized = _SIZED.replace('              memory = "64Mi"\n', '              cpu = "1"\n')
    unsized = unsized.replace('              memory = "32Mi"\n', "")

    assert "requests.memory, limits.memory" in _module(tmp_path, unsized)[0]


def test_init_container_is_held_to_the_rule(tmp_path: Path) -> None:
    assert len(_module(tmp_path, _BARE.replace("container {", "init_container {"))) == 1


def test_resources_expression_is_accepted(tmp_path: Path) -> None:
    sized = _BARE.replace('image = "busybox"', 'image = "busybox"\n          resources = var.sizing')

    assert _module(tmp_path, sized) == []


def test_repository_containers_are_all_sized() -> None:
    root = Path(__file__).resolve().parents[4]
    found = sum(
        line.strip() in ("container {", "init_container {")
        for path in (root / "infra/terraform/modules").rglob("*.tf")
        for line in path.read_text().splitlines()
    )

    assert found > 20  # the scan sees the repository's containers, so an empty result is not a miss
    assert _resources.resource_errors(root) == []
