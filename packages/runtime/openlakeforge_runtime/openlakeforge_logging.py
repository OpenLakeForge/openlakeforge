from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any


def log_event(logger, *, level: str = "info", message: str, **fields: Any) -> None:
    payload = {
        "schema": "openlakeforge.log.v1",
        "ts": datetime.now(UTC).isoformat(),
        "level": level,
        "message": message,
    }
    run_id = os.environ.get("DAGSTER_RUN_ID")
    if run_id:
        payload["dagster_run_id"] = run_id
    payload.update({key: value for key, value in fields.items() if value is not None})

    log_method = getattr(logger, level, logger.info)
    log_method(json.dumps(payload, sort_keys=True, default=str))
