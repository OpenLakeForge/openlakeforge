"""Every oauth2-proxy instance of the perimeter keeps one CSRF cookie per login attempt (#437)."""

from __future__ import annotations

from pathlib import Path

MAIN = Path(__file__).resolve().parents[3] / "infra/terraform/modules/access/perimeter/main.tf"


def test_parallel_sign_ins_get_their_own_csrf_cookie() -> None:
    # One container args list serves every instance (for_each over the floors).
    source = MAIN.read_text()
    assert source.count('"--code-challenge-method=S256"') == 1
    assert source.count('"--cookie-csrf-per-request=true"') == 1
    assert source.count('"--cookie-csrf-expire=5m"') == 1
