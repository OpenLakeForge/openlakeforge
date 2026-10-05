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


def _drill(
    monkeypatch: pytest.MonkeyPatch, served: list[str | Exception], conditions: str = ""
) -> list[list[str]]:
    commands: list[list[str]] = []
    serials = iter(served)

    def _served_serial(_host: str, _port: int) -> str:
        serial = next(serials)
        if isinstance(serial, Exception):
            raise serial
        return serial

    def _kubectl(_cfg: object, args: list[str], **_kwargs: object) -> str:
        commands.append(args)
        return conditions if args[0] == "get" else ""

    monkeypatch.setattr(_ingress, "kubectl", _kubectl)
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

    patch = commands[-1]
    assert patch[:4] == ["patch", "certificate", "openlakeforge-routes-tls", "-n"]
    assert [c["type"] for c in json.loads(patch[-1])[0]["value"]] == ["Issuing"]
    evidence = json.loads((tmp_path / ".tmp/e2e-evidence/certificate-renewal-lakehouse.json").read_text())
    assert evidence["serial_before"] == "0A"
    assert evidence["serial_after"] == "0B"
    assert evidence["failed_handshakes"] == []


def test_renewal_drill_replaces_a_left_over_issuing_condition(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    existing = [
        {"type": "Ready", "status": "True"},
        {"type": "Issuing", "status": "False", "reason": "Failed"},
    ]
    commands = _drill(monkeypatch, ["0A", "0B"], conditions=json.dumps(existing))

    _ingress.check_certificate_renewal(e2e_cfg(tmp_path))

    written = json.loads(commands[-1][-1])[0]["value"]
    assert [(c["type"], c["status"]) for c in written] == [("Ready", "True"), ("Issuing", "True")]


def test_renewal_drill_fails_on_a_dropped_handshake_even_when_the_serial_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _drill(monkeypatch, ["0A", ssl.SSLError("certificate verify failed"), "0B"])

    with pytest.raises(E2EError, match="1 of 2 TLS handshakes"):
        _ingress.check_certificate_renewal(e2e_cfg(tmp_path))

    assert (tmp_path / ".tmp/e2e-evidence/certificate-renewal-lakehouse.json").is_file()


def test_restart_drill_keeps_its_evidence_when_the_route_does_not_recover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _drill(monkeypatch, [])
    monkeypatch.setattr(_ingress.k8s, "http_wait", lambda _url, **_kwargs: False)

    with pytest.raises(E2EError, match="did not answer"):
        _ingress.check_traefik_restart_recovery(e2e_cfg(tmp_path))

    evidence = json.loads((tmp_path / ".tmp/e2e-evidence/traefik-restart-lakehouse.json").read_text())
    assert evidence["recovered"] is False
