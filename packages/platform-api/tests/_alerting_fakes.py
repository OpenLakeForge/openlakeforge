"""An in-memory ops bucket for the alert evaluator's tests."""

from __future__ import annotations

import io
import json
from typing import Any

from botocore.exceptions import ClientError, EndpointConnectionError


class FakeBucket:
    """get_object and put_object over a dict, as boto3 answers them; `down` raises like an unreachable store."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.down = False

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._check()
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        self._check()
        self.objects[Key] = Body

    def list_objects_v2(  # noqa: N803
        self, *, Bucket: str, Prefix: str = "", ContinuationToken: str = ""
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _check(self) -> None:
        if self.down:
            raise EndpointConnectionError(endpoint_url="http://store.invalid")

    def under(self, prefix: str) -> dict[str, dict[str, Any]]:
        return {key: json.loads(body) for key, body in sorted(self.objects.items()) if key.startswith(prefix)}


def settings_mapping() -> dict[str, Any]:
    return {
        "stages": ["dev"],
        "dagster_graphql_urls": {"dev": "http://dagster-dev/graphql"},
        "state_base_uri": "s3://ops/alerts/",
        "receivers": [{"name": "ops", "url_env": "OPS_URL"}],
    }
