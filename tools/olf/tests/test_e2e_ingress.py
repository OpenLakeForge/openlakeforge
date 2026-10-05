from __future__ import annotations

import contextlib
import json
import ssl
from pathlib import Path

import pytest
from conftest import e2e_cfg

from olf.e2e import _ingress
from olf.e2e._shell import E2EError

_URL = "https://dagster.dev.olf.localhost"


def _drill(monkeypatch: pytest.MonkeyPatch, served: list[str | Exception]) -> list[list[str]]:
    commands: list[list[str]] = []
    serials = iter(served)

    def _served_serial(_host: str, _port: int) -> str:
        serial = next(serials)
        if isinstance(serial, Exception):
            raise serial
        return serial

    monkeypatch.setattr(_ingress, "kubectl", lambda _cfg, args, **_kwargs: commands.append(args) or "")
    monkeypatch.setattr(_ingress, "_route", lambda _cfg: contextlib.nullcontext(_URL))
    monkeypatch.setattr(_ingress, "_served_serial", _served_serial)
    monkeypatch.setattr(_ingress.time, "sleep", lambda _seconds: None)
    monkeypatch.delenv("OLF_PORT_FORWARD", raising=False)
    return commands


def test_drills_leave_the_cluster_alone_when_port_forwarding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commands = _drill(monkeypatch, [])
    monkeypatch.setenv("OLF_PORT_FORWARD", "1")

    _ingress.check_traefik_restart_recovery(e2e_cfg(tmp_path))
    _ingress.check_certificate_renewal(e2e_cfg(tmp_path))

    assert commands == []


def test_renewal_drill_waits_for_a_new_serial_and_keeps_the_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _drill(monkeypatch, ["0A", "0A", "0B"])

    _ingress.check_certificate_renewal(e2e_cfg(tmp_path))

    patch = commands[0]
    assert patch[:4] == ["patch", "certificate", "openlakeforge-routes-tls", "-n"]
    assert json.loads(patch[-1])[0]["value"]["type"] == "Issuing"
    evidence = json.loads((tmp_path / ".tmp/e2e-evidence/certificate-renewal-lakehouse.json").read_text())
    assert evidence["serial_before"] == "0A"
    assert evidence["serial_after"] == "0B"
    assert evidence["failed_handshakes"] == []


def test_renewal_drill_fails_on_a_dropped_handshake_even_when_the_serial_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _drill(monkeypatch, ["0A", ssl.SSLError("certificate verify failed"), "0B"])

    with pytest.raises(E2EError, match="1 of 2 TLS handshakes"):
        _ingress.check_certificate_renewal(e2e_cfg(tmp_path))
