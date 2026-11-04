"""The optional project dependency layer: ``lakehouse_code/requirements.txt`` on top of the platform lock."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from olf.common.deployment.errors import DeploymentError, DeploymentPreconditionError
from olf.common.tooling.toolkit import Toolkit

# The platform lock's `uv pip compile` target; a project's pins must resolve for the same interpreter and platform.
_PYTHON_VERSION = "3.12"
_PLATFORM = "x86_64-manylinux2014"


def platform_constraints(lock_text: str) -> str:
    """The lock's ``name==version`` lines without hashes, comments and continuations.

    The Dockerfile derives the same file with ``sed``, so keep the two in step.
    """
    return "".join(
        f"{re.sub(r' \\$', '', line)}\n" for line in lock_text.splitlines() if re.match(r"[A-Za-z0-9]", line)
    )


def check_project_requirements(requirements: Path, lock: Path, *, tools: Toolkit) -> None:
    """Fail when the project's requirements cannot resolve with the platform pins held fixed."""
    with tempfile.TemporaryDirectory(prefix="project-requirements-") as temporary:
        constraints = Path(temporary) / "constraints.txt"
        constraints.write_text(platform_constraints(lock.read_text()))
        try:
            tools.runner.run(
                [
                    str(tools.resolver.resolve("uv")),
                    "pip",
                    "compile",
                    str(requirements),
                    "--constraint",
                    str(constraints),
                    "--python-version",
                    _PYTHON_VERSION,
                    "--python-platform",
                    _PLATFORM,
                    "--quiet",
                    "--output-file",
                    str(Path(temporary) / "resolved.txt"),
                ],
                cwd=requirements.parent,
                env=None,
                stream_output=True,
            )
        except DeploymentError as exc:
            raise DeploymentPreconditionError(
                f"{requirements} conflicts with the platform dependency pins (images/project-code/requirements.lock), "
                f"which a project cannot change: {exc}"
            ) from exc
