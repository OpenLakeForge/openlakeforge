import json
import logging

import pytest
from libs.openlakeforge_logging import log_event


def _emit(caplog: pytest.LogCaptureFixture, **kwargs: object) -> dict:
    logger = logging.getLogger("olf-test")
    with caplog.at_level(logging.DEBUG, logger="olf-test"):
        log_event(logger, **kwargs)  # type: ignore[arg-type]
    return json.loads(caplog.records[-1].getMessage())


def test_log_event_emits_one_json_document_with_the_schema_and_fields(caplog: pytest.LogCaptureFixture) -> None:
    payload = _emit(caplog, message="Loaded Bronze source", source="orders", rows=3)

    assert payload["schema"] == "openlakeforge.log.v1"
    assert (payload["level"], payload["message"], payload["source"], payload["rows"]) == (
        "info",
        "Loaded Bronze source",
        "orders",
        3,
    )
    assert "ts" in payload


def test_log_event_drops_none_fields_and_tags_the_dagster_run(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAGSTER_RUN_ID", "run-1")

    payload = _emit(caplog, message="m", entity=None, uri="s3://b/k")

    assert payload["dagster_run_id"] == "run-1"
    assert "entity" not in payload
    assert payload["uri"] == "s3://b/k"


def test_log_event_uses_the_requested_level_and_falls_back_to_info(caplog: pytest.LogCaptureFixture) -> None:
    assert _emit(caplog, level="warning", message="m")["level"] == "warning"
    assert caplog.records[-1].levelno == logging.WARNING

    _emit(caplog, level="not-a-level", message="m")
    assert caplog.records[-1].levelno == logging.INFO
