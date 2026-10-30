"""`olf check security`: every scan suppression has an owner and a bounded expiry."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import typer
import yaml

from olf.common.commands.checks._shared import _root
from olf.common.log import fail

# Trivy reads the vulnerabilities/misconfigurations/secrets sections natively and drops an
# entry once its expired_at passes. `images` is ours: a waived image is still scanned and
# reported, but its findings stop failing the scan. `codeql` entries name an alert as
# "<rule id>:<path>", because CodeQL has no suppression file of its own.
SUPPRESSIONS = ".trivyignore.yaml"
# The remediation window: a suppression buys time to fix, not a permanent waiver.
MAX_SUPPRESSION_DAYS = 90


def _load(root: Path) -> dict[str, list[dict[str, object]]]:
    return yaml.safe_load((root / SUPPRESSIONS).read_text()) or {}


def suppression_errors(root: Path, today: date) -> list[str]:
    """Return a message per suppression that is unowned, expired, or dated too far out."""
    errors: list[str] = []
    for section, entries in _load(root).items():
        for entry in entries or []:
            label = f"{SUPPRESSIONS} {section} {entry.get('id') or entry.get('name')}"
            expires = entry.get("expired_at")
            if not str(entry.get("owner", "")).strip():
                errors.append(f"{label}: owner is required")
            if not isinstance(expires, date):
                errors.append(f"{label}: expired_at must be a YYYY-MM-DD date")
            elif expires < today:
                errors.append(f"{label}: expired on {expires}; fix the finding or renew with a new date")
            elif expires > today + timedelta(days=MAX_SUPPRESSION_DAYS):
                errors.append(f"{label}: expires {expires}, more than {MAX_SUPPRESSION_DAYS} days out")
    return errors


def is_waived(root: Path, section: str, name: str, today: date) -> bool:
    """Whether an unexpired waiver in this section names the finding or image."""
    for entry in _load(root).get(section) or []:
        expires = entry.get("expired_at")
        if entry.get("name") == name and isinstance(expires, date) and expires >= today:
            return True
    return False


def security(
    repo_root: str = typer.Option("", "--repo-root", help="Checkout root to validate."),
    waived: str = typer.Option("", "--waived", help="SECTION:NAME. Exit 0 only if that entry has an unexpired waiver."),
) -> None:
    """Validate the security-scan suppression file."""
    root = _root(repo_root)
    if waived:
        section, _, name = waived.partition(":")
        raise typer.Exit(code=0 if is_waived(root, section, name, date.today()) else 1)
    errors = suppression_errors(root, date.today())
    if errors:
        raise typer.Exit(code=fail("\n".join(errors)))
    typer.echo("Security suppressions are owned and unexpired.")
