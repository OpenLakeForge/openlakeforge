"""Logging helpers matching the shell script output conventions."""

from __future__ import annotations

import sys


def step(message: str) -> None:
    """Announce a step, matching the shell `==> ...` convention."""
    print(f"==> {message}", flush=True)


def info(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"WARN: {message}", file=sys.stderr, flush=True)


def error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr, flush=True)


def fail(message: str) -> int:
    """Log error and return exit code 1."""
    error(message)
    return 1
