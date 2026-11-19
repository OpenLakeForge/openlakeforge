"""`--json` for every `olf check` target: one document on stdout, the targets' own output on stderr.

The targets report by printing and by raising `typer.Exit`, so the document is
built around them rather than threaded through each one: a target's output is
teed to stderr as it is written, and its summary or error is lifted from what
it printed. That keeps the human output byte-identical and the JSON shape
owned in one place.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, TextIO

import typer

JSON_OPTION: Any = typer.Option(
    False, "--json", help="Print a JSON report on stdout; the checks' own output goes to stderr."
)

_ERROR_PREFIX = "ERROR: "


class _Tee(io.TextIOBase):
    def __init__(self, sink: TextIO) -> None:
        self._sink = sink
        self.captured = io.StringIO()

    def write(self, text: str) -> int:
        self._sink.write(text)
        self.captured.write(text)
        return len(text)

    def flush(self) -> None:
        self._sink.flush()


def _message(stdout: str, stderr: str, *, failed: bool) -> str:
    lines = stderr.splitlines()
    error_at = next((i for i, line in enumerate(lines) if line.startswith(_ERROR_PREFIX)), None)
    if failed and error_at is not None:
        return "\n".join(line.removeprefix(_ERROR_PREFIX) for line in lines[error_at:]).strip()
    if failed:
        # contracts and components report every result on stdout and exit 1 with no ERROR line.
        return stdout.strip()
    return next((line for line in reversed(stdout.splitlines()) if line.strip()), "").strip()


def run_target(check_id: str, target: Callable[[], None]) -> tuple[dict[str, Any], int]:
    sink = sys.stderr
    out, err = _Tee(sink), _Tee(sink)
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            target()
        except typer.Exit as exc:
            code = exc.exit_code
    item = {
        "id": check_id,
        "status": "fail" if code else "pass",
        "message": _message(out.captured.getvalue(), err.captured.getvalue(), failed=bool(code)),
        "remedy": None,
    }
    return item, code


def run_targets(command: str, targets: Sequence[tuple[str, Callable[[], None]]]) -> None:
    """Run `targets` in order, stopping at the first failure as the text mode does, and print the document."""
    checks: list[dict[str, Any]] = []
    code = 0
    for check_id, target in targets:
        if code:
            checks.append(
                {"id": check_id, "status": "skipped", "message": "not run: an earlier check failed", "remedy": None}
            )
            continue
        item, code = run_target(check_id, target)
        checks.append(item)
    document = {"command": command, "ok": code == 0, "checks": checks}
    typer.echo(json.dumps(document, indent=2))
    if code:
        raise typer.Exit(code=code)


def with_json(check_id: str, target: Callable[..., None]) -> Callable[..., None]:
    """Return `target` as a command that also takes `--json`."""

    def command(*args: Any, json_output: bool = False, **kwargs: Any) -> None:
        if not json_output:
            target(*args, **kwargs)
            return
        run_targets(f"check {check_id}", [(check_id, lambda: target(*args, **kwargs))])

    signature = inspect.signature(target, eval_str=True)
    json_param = inspect.Parameter("json_output", inspect.Parameter.KEYWORD_ONLY, default=JSON_OPTION, annotation=bool)
    command.__signature__ = signature.replace(  # type: ignore[attr-defined]  # typer reads the command's options from it
        parameters=[*signature.parameters.values(), json_param]
    )
    command.__name__ = target.__name__
    command.__doc__ = target.__doc__
    return command
