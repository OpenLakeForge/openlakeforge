from __future__ import annotations

from datetime import date
from pathlib import Path

from olf.commands.checks import _security

TODAY = date(2026, 11, 1)


def _errors(tmp_path: Path, text: str) -> list[str]:
    (tmp_path / ".trivyignore.yaml").write_text(text)
    return _security.suppression_errors(tmp_path, TODAY)


def test_accepts_owned_unexpired_suppressions(tmp_path: Path) -> None:
    text = "vulnerabilities:\n  - id: CVE-1\n    owner: malon64\n    expired_at: 2026-12-01\n"
    assert _errors(tmp_path, text) == []


def test_accepts_an_empty_file(tmp_path: Path) -> None:
    assert _errors(tmp_path, "# nothing suppressed\n") == []


def test_rejects_missing_owner_expired_and_distant_dates(tmp_path: Path) -> None:
    text = (
        "misconfigurations:\n"
        "  - {id: A, expired_at: 2026-12-01}\n"
        "  - {id: B, owner: x, expired_at: 2026-10-31}\n"
        "  - {id: C, owner: x, expired_at: 2027-06-01}\n"
        "  - {id: D, owner: x}\n"
    )
    errors = _errors(tmp_path, text)
    assert "A: owner is required" in errors[0]
    assert "B: expired on 2026-10-31" in errors[1]
    assert "C: expires 2027-06-01, more than 90 days out" in errors[2]
    assert "D: expired_at must be a YYYY-MM-DD date" in errors[3]


def test_image_waiver_applies_until_it_expires(tmp_path: Path) -> None:
    _errors(
        tmp_path,
        "images:\n  - {name: a, owner: x, expired_at: 2026-11-01}\n  - {name: b, owner: x, expired_at: 2026-10-31}\n",
    )
    assert _security.is_image_waived(tmp_path, "a", TODAY)
    assert not _security.is_image_waived(tmp_path, "b", TODAY)
    assert not _security.is_image_waived(tmp_path, "c", TODAY)
