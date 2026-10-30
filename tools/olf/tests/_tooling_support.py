"""Test-only helpers for the `olf.deployment`/`olf.tooling` test modules.

Deliberately not `conftest.py`: this only serves the new deployment-substrate
tests and must not interact with the shared domain fixtures other test files
depend on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from olf.tooling.process import Command, CommandResult, ProcessRunner

FAKE_SCRIPT_TEMPLATE = """#!/usr/bin/env python3
import json
import os
import sys

payload = {{
    "argv": sys.argv[1:],
    "env": {{key: os.environ.get(key) for key in {env_keys!r}}},
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
}}
sys.stderr.write({stderr!r})
print(json.dumps(payload))
sys.exit({exit_code})
"""


def write_fake_executable(
    path: Path,
    *,
    env_keys: list[str] | None = None,
    stderr: str = "",
    exit_code: int = 0,
) -> Path:
    """Write a tiny Python executable that reports its argv/env/stdin as JSON."""
    script = FAKE_SCRIPT_TEMPLATE.format(env_keys=env_keys or [], stderr=stderr, exit_code=exit_code)
    path.write_text(script)
    path.chmod(0o755)
    return path


def write_sleeping_executable(path: Path, *, seconds: float) -> Path:
    script = f"#!/usr/bin/env python3\nimport time\ntime.sleep({seconds})\n"
    path.write_text(script)
    path.chmod(0o755)
    return path


@dataclass
class RecordedCall:
    argv: list[str]
    kwargs: dict[str, Any]


class RecordingRunner(ProcessRunner):
    """A `ProcessRunner` stand-in that records calls instead of executing anything.

    Used by tool-adapter tests, which assert exact argv construction rather
    than exercising a real subprocess.
    """

    def __init__(self, result: CommandResult | None = None) -> None:
        super().__init__()
        self.calls: list[RecordedCall] = []
        self._result = result

    def run(  # type: ignore[override]
        self,
        command: Command | list[str],
        **kwargs: Any,
    ) -> CommandResult:
        argv = list(command.argv) if isinstance(command, Command) else [str(part) for part in command]
        self.calls.append(RecordedCall(argv=argv, kwargs=kwargs))
        if self._result is not None:
            return self._result
        return CommandResult(argv=tuple(argv), returncode=0, stdout="", stderr="", duration_seconds=0.0)

    @property
    def last_call(self) -> RecordedCall:
        return self.calls[-1]
