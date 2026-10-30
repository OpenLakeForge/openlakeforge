"""`moved_alias` warns on stderr and leaves stdout, options and exit codes alone (#536)."""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from olf.common.moved import moved_alias


def _app(code: int) -> typer.Typer:
    app = typer.Typer()

    def greet(name: str = typer.Option("world", "--name")) -> None:
        typer.echo(f"hello {name}")
        raise typer.Exit(code=code)

    app.command("greet")(moved_alias(greet, "olf infra greet"))
    app.command("other")(lambda: None)
    return app


def test_alias_keeps_options_stdout_and_exit_code() -> None:
    result = CliRunner().invoke(_app(3), ["greet", "--name", "olf"])
    assert result.exit_code == 3
    assert result.stdout == "hello olf\n"
    assert result.stderr == (
        "WARN: this command moved to `olf infra greet`; the old path will be removed in a later release\n"
    )
