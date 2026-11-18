"""The catalog-image scan logs in to Docker Hub when it can, retries registry errors only, and still fails on findings
and unscannable images.

The step's own script runs against stand-ins for `uv`, `trivy` and `sleep` on PATH, because the job only runs
when the component catalog changes, so a pull request's CI rarely exercises it (#1250).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[4]
_WORKFLOW = _REPO / ".github/workflows/security.yml"
_SCAN = "Scan the images pinned in the component catalog"
_LOGIN = "Log in to Docker Hub"

_RATE_LIMITED = (
    'FATAL\tFatal error\trun error: image scan error: unable to find the specified image in ["remote"]:\n'
    "\t* remote error: GET https://docker.getcollate.io/v2/x/manifests/sha256:4290503: TOOMANYREQUESTS: "
    "You have reached your unauthenticated pull rate limit."
)
_UNAVAILABLE = "remote error: GET https://quay.io/v2/x/manifests/latest: unexpected status code 503 Service Unavailable"
# A digest holding "429" and "503" must not read as a retryable status.
_NOT_FOUND = "remote error: GET https://r.example/v2/x/manifests/sha256:4295030: MANIFEST_UNKNOWN: manifest unknown"

# Each `trivy image` call consumes the next outcome scripted for its image name.
_FAKE_TRIVY = """#!/usr/bin/env bash
set -euo pipefail
command="$1"; shift
if [[ "$command" == convert ]]; then
  while [[ $# -gt 1 ]]; do [[ "$1" == --exit-code ]] && code="$2"; shift; done
  grep -q VULNERABLE "$1" && exit "$code"
  exit 0
fi
while [[ $# -gt 1 ]]; do [[ "$1" == --output ]] && output="$2"; shift; done
name="${1%%:*}"
echo "$name" >>"$FAKE/calls"
outcome="$(head -n1 "$FAKE/outcomes/$name")"
tail -n +2 "$FAKE/outcomes/$name" >"$FAKE/rest" && mv "$FAKE/rest" "$FAKE/outcomes/$name"
case "$outcome" in
  clean) echo '{"Results": []}' >"$output" ;;
  vulnerable) echo '{"Results": ["VULNERABLE"]}' >"$output" ;;
  *) cat "$FAKE/errors/$outcome" >&2; exit 1 ;;
esac
"""

_FAKE_UV = """#!/usr/bin/env bash
case " $* " in
  *" python "*) cat "$FAKE/catalog" ;;
  *" --waived "*) grep -qx "${@: -1}" "$FAKE/waived" ;;
  *) exit 2 ;;
esac
"""

_FAKE_SLEEP = """#!/usr/bin/env bash
echo "$1" >>"$FAKE/sleeps"
"""


@dataclass(frozen=True)
class _Run:
    returncode: int
    output: str
    calls: list[str]
    sleeps: list[int]


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(_WORKFLOW.read_text())
    return workflow


def _catalog_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _workflow()["jobs"]["catalog-images"]["steps"]
    return steps


def _step_index(name: str) -> int:
    (index,) = [index for index, step in enumerate(_catalog_steps()) if step.get("name") == name]
    return index


def _scan_script() -> str:
    return str(_catalog_steps()[_step_index(_SCAN)]["run"])


def _run(tmp_path: Path, outcomes: dict[str, list[str]], waived: tuple[str, ...] = ()) -> _Run:
    fake = tmp_path / "fake"
    bin_dir = tmp_path / "bin"
    for directory in (fake / "outcomes", fake / "errors", bin_dir, tmp_path / "work"):
        directory.mkdir(parents=True)
    for name, script in (("trivy", _FAKE_TRIVY), ("uv", _FAKE_UV), ("sleep", _FAKE_SLEEP)):
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)
    for name, text in (("rate_limited", _RATE_LIMITED), ("unavailable", _UNAVAILABLE), ("not_found", _NOT_FOUND)):
        (fake / "errors" / name).write_text(text + "\n")
    (fake / "catalog").write_text("".join(f"{name} {name}:1.0@sha256:abc\n" for name in outcomes))
    (fake / "waived").write_text("".join(f"images:{name}\n" for name in waived))
    for name, sequence in outcomes.items():
        (fake / "outcomes" / name).write_text("".join(f"{outcome}\n" for outcome in sequence))

    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE": str(fake)}
    env["GITHUB_EVENT_NAME"] = "push"
    result = subprocess.run(
        ["bash", "-c", _scan_script()],
        cwd=tmp_path / "work",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    def lines(path: Path) -> list[str]:
        return path.read_text().split() if path.exists() else []

    return _Run(
        returncode=result.returncode,
        output=result.stdout + result.stderr,
        calls=lines(fake / "calls"),
        sleeps=[int(value) for value in lines(fake / "sleeps")],
    )


def test_a_rate_limited_image_is_retried_with_growing_backoff_until_it_scans(tmp_path: Path) -> None:
    run = _run(tmp_path, {"ingestion": ["rate_limited", "unavailable", "rate_limited", "clean"], "trino": ["clean"]})

    assert run.returncode == 0, run.output
    assert run.calls == ["ingestion"] * 4 + ["trino"]
    assert [30 <= run.sleeps[0] < 45, 60 <= run.sleeps[1] < 75, 120 <= run.sleeps[2] < 135] == [True] * 3


def test_a_finding_still_fails_the_job_after_a_retried_scan(tmp_path: Path) -> None:
    run = _run(tmp_path, {"ingestion": ["rate_limited", "vulnerable"]})

    assert run.returncode == 1
    assert run.calls == ["ingestion"] * 2


def test_a_finding_in_a_waived_image_does_not_fail_the_job(tmp_path: Path) -> None:
    run = _run(tmp_path, {"ingestion": ["vulnerable"]}, waived=("ingestion",))

    assert run.returncode == 0, run.output


@pytest.mark.parametrize(("waived", "returncode"), [((), 1), (("ingestion",), 0)])
def test_an_image_still_rate_limited_after_four_attempts_fails_unless_waived(
    tmp_path: Path, waived: tuple[str, ...], returncode: int
) -> None:
    run = _run(tmp_path, {"ingestion": ["rate_limited"] * 4, "trino": ["clean"]}, waived=waived)

    assert run.returncode == returncode
    assert run.calls == ["ingestion"] * 4 + ["trino"]
    assert len(run.sleeps) == 3
    assert "could not scan ingestion" in run.output


def test_a_non_registry_error_is_not_retried(tmp_path: Path) -> None:
    run = _run(tmp_path, {"ingestion": ["not_found"], "trino": ["clean"]})

    assert run.returncode == 1
    assert run.calls == ["ingestion", "trino"]
    assert run.sleeps == []


def test_docker_hub_login_runs_before_the_scan_with_the_org_credentials_and_only_when_the_token_is_set() -> None:
    login = _catalog_steps()[_step_index(_LOGIN)]

    assert _step_index(_LOGIN) < _step_index(_SCAN)
    assert re.fullmatch(r"docker/login-action@[0-9a-f]{40}", login["uses"])
    # No registry: Docker Hub, the only registry these credentials belong to.
    assert login["with"] == {
        "username": "${{ vars.DOCKERHUB_USERNAME }}",
        "password": "${{ secrets.DOCKERHUB_TOKEN }}",
    }
    # Secrets cannot appear in `if`, so the token is tested through the step's env; fork pull requests
    # and callers that do not pass it skip the login and scan anonymously.
    assert login["env"] == {"DOCKERHUB_TOKEN": "${{ secrets.DOCKERHUB_TOKEN }}"}
    assert login["if"] == "env.DOCKERHUB_TOKEN != ''"


def test_the_docker_hub_token_never_reaches_trivy_as_a_registry_agnostic_credential() -> None:
    # Trivy sends TRIVY_USERNAME/TRIVY_PASSWORD to every registry it pulls from (ghcr.io,
    # docker.getcollate.io, ...); the docker config the login writes is keyed by registry instead.
    job = _workflow()["jobs"]["catalog-images"]
    scan = _catalog_steps()[_step_index(_SCAN)]
    trivy_env = {name for step in job["steps"] for name in step.get("env", {}) if name.startswith("TRIVY_")}

    assert "DOCKERHUB_TOKEN" not in yaml.safe_dump(scan)
    assert "env" not in job
    assert trivy_env == set()


def test_the_release_workflow_passes_the_token_to_the_scans_it_calls() -> None:
    # YAML 1.1 reads the bare `on:` key as boolean true.
    declared = _workflow()[True]["workflow_call"]["secrets"]["DOCKERHUB_TOKEN"]
    release = yaml.safe_load((_REPO / ".github/workflows/release.yml").read_text())

    assert declared == {"required": False}
    assert release["jobs"]["security"]["secrets"] == {"DOCKERHUB_TOKEN": "${{ secrets.DOCKERHUB_TOKEN }}"}
