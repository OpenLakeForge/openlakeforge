"""The log archive's retention is applied by the object store, not only reported (ADR 0022, decision 6, #22).

SeaweedFS 4.48 accepts and returns a lifecycle rule put through the S3 API but creates no TTL for it, so the local
adapter sets a filer path TTL (`fs.configure -ttl`, modules/storage/seaweedfs). Three facts are checked on the
running store:

1. the filer's configuration holds a rule of the profile's `retention_days` for each v2 prefix of the stage and of
   the platform;
2. an object the collector wrote under such a prefix carries that TTL;
3. the mechanism expires: a scratch prefix with a one-minute rule loses an object it was given, and a sibling object
   outside the prefix stays. The real rules count days and cannot be waited out; this shows the filer honours a path
   TTL on objects written through the S3 gateway, which is how the collector writes.

Local only: it reads the filer inside its pod and needs the SeaweedFS port-forward for the S3 writes.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from olf.common import log
from olf.infra.e2e import _log_collector as collector
from olf.infra.e2e._shell import E2EConfig, E2EError, kubectl

FILER_POD = "seaweedfs-filer-0"
FILER_URL = "http://127.0.0.1:8888"
MASTER_ADDRESS = "seaweedfs-master:9333"
SCRATCH_TTL = "1m"
# The filer drops an expired entry on lookup; this allows its clock and the rule's pickup, not a background sweep.
EXPIRY_TIMEOUT_SECONDS = 240.0
EXPIRY_POLL_SECONDS = 15.0


def seaweedfs_ttl(days: int) -> tuple[str, int]:
    """The TTL the seaweedfs module renders for `days`, and the days it keeps.

    `fs.configure -ttl` in SeaweedFS 4.48 takes a count of 1 to 255, so above 255 days the module uses the shortest
    TTL of whole weeks, months (30 days) or years (365 days) that is at least `days`, the larger unit on a tie
    (`retention_ttl` in modules/storage/seaweedfs/main.tf; its tftest pins the same values).
    """
    if days <= 255:
        return f"{days}d", days
    candidates = [
        (math.ceil(days / unit) * unit, order, f"{math.ceil(days / unit)}{suffix}")
        for order, (suffix, unit) in enumerate((("y", 365), ("M", 30), ("w", 7)))
        if math.ceil(days / unit) <= 255
    ]
    kept, _, ttl = min(candidates)
    return ttl, kept


def retention_rules(filer_conf: Mapping[str, Any]) -> dict[str, str]:
    """Location prefix to TTL string of every path rule in the filer's configuration."""
    return {rule["locationPrefix"]: rule.get("ttl", "") for rule in filer_conf.get("locations", [])}


def missing_rules(rules: Mapping[str, str], bucket: str, prefixes: Iterable[str], ttl: str) -> list[str]:
    """The prefixes with no rule of exactly `ttl`, as the filer path each would be under."""
    return [f"/buckets/{bucket}/{prefix}" for prefix in prefixes if rules.get(f"/buckets/{bucket}/{prefix}") != ttl]


def entry_ttl_seconds(entry: Mapping[str, Any]) -> int | None:
    """The TTL of a filer entry document, wherever the build puts it (`Attr.TtlSec` or `attributes.ttlSec`)."""
    stack: list[Any] = [entry]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key.lower() in ("ttlsec", "ttl_sec") and isinstance(value, int):
                    return value
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _filer(cfg: E2EConfig, args: list[str]) -> str:
    return kubectl(cfg, ["exec", "-n", cfg.platform_namespace, FILER_POD, "--", *args], capture=True)


def _filer_get(cfg: E2EConfig, path: str) -> str:
    return _filer(cfg, ["wget", "-qO-", f"{FILER_URL}{path}"])


def _shell(cfg: E2EConfig, command: str) -> str:
    """One `weed shell` command against this cluster, run in the filer's pod where the binary is."""
    return kubectl(
        cfg,
        [
            "exec",
            "-i",
            "-n",
            cfg.platform_namespace,
            FILER_POD,
            "--",
            "sh",
            "-c",
            f"echo '{command}' | /usr/bin/weed shell -master={MASTER_ADDRESS} -filer=127.0.0.1:8888",
        ],
        capture=True,
    )


def _archive_contract(cfg: E2EConfig) -> tuple[str, int, bool]:
    contracts = collector.load_provider_contracts_or_raise(cfg)["shared"]
    archive = contracts["observability"]["logs"]["archive"]
    return contracts["ops_storage"]["bucket_name"], int(archive["retention_days"]), bool(archive["retention_supported"])


def _stage_prefixes(cfg: E2EConfig) -> list[str]:
    stage = cfg.namespace.removeprefix("olf-")
    return [f"activations/{stage}/logs/v2/", collector.PLATFORM_V2_PREFIX]


def _newest_v2_key(client: Any, bucket: str, cfg: E2EConfig) -> str:
    prefix = f"activations/{cfg.namespace.removeprefix('olf-')}/logs/v2/namespace={cfg.namespace}/"
    newest: tuple[Any, str] | None = None
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            if newest is None or item["LastModified"] > newest[0]:
                newest = (item["LastModified"], item["Key"])
    if newest is None:
        raise E2EError(f"no v2 archive object under {prefix}: the collector drills run before this check")
    return newest[1]


def check_archive_retention(cfg: E2EConfig) -> None:
    """The profile's retention is in the filer as TTL rules, on the collector's objects, and a path TTL expires."""
    log.step("Checking the log archive's retention is applied by the object store...")
    bucket, days, supported = _archive_contract(cfg)
    if not supported:
        raise E2EError("the provider contract reports archive retention as unsupported, so none was applied")
    prefixes = _stage_prefixes(cfg)
    ttl_rule, kept_days = seaweedfs_ttl(days)
    rules = retention_rules(json.loads(_filer_get(cfg, "/etc/seaweedfs/filer.conf")))
    if missing := missing_rules(rules, bucket, prefixes, ttl_rule):
        raise E2EError(f"the filer has no {ttl_rule} TTL rule for {missing}; it has {rules}")
    with collector._s3(cfg) as (client, _bucket):  # noqa: SLF001
        key = _newest_v2_key(client, bucket, cfg)
        entry = json.loads(_filer_get(cfg, f"/buckets/{bucket}/{key}?metadata=true"))
        ttl = entry_ttl_seconds(entry)
        log.info(f"{key} carries ttl_sec={ttl}")
        if ttl != kept_days * 86400:
            raise E2EError(
                f"{key} has TTL {ttl}s, not {kept_days * 86400}s ({ttl_rule}); its entry: {json.dumps(entry)[:600]}"
            )
        _check_a_path_ttl_expires(cfg, client, bucket)


def _check_a_path_ttl_expires(cfg: E2EConfig, client: Any, bucket: str) -> None:
    scratch = f"olf-e2e-retention-{uuid.uuid4().hex[:8]}/"
    sibling = f"{scratch.rstrip('/')}-kept/probe.txt"
    path = f"/buckets/{bucket}/{scratch}"
    _shell(cfg, f"fs.configure -locationPrefix={path} -ttl={SCRATCH_TTL} -apply")
    try:
        client.put_object(Bucket=bucket, Key=f"{scratch}probe.txt", Body=b"expires")
        client.put_object(Bucket=bucket, Key=sibling, Body=b"stays")
        deadline = time.monotonic() + EXPIRY_TIMEOUT_SECONDS
        while _listed(client, bucket, scratch):
            if time.monotonic() >= deadline:
                raise E2EError(
                    f"an object under a {SCRATCH_TTL} path TTL was still listed after {EXPIRY_TIMEOUT_SECONDS:.0f}s"
                )
            time.sleep(EXPIRY_POLL_SECONDS)
        if not _listed(client, bucket, sibling):
            raise E2EError("the path TTL removed an object outside its prefix")
        log.info(f"A {SCRATCH_TTL} path TTL expired its object and left its sibling.")
    finally:
        _shell(cfg, f"fs.configure -locationPrefix={path} -delete -apply")
        client.delete_object(Bucket=bucket, Key=sibling)
        client.delete_object(Bucket=bucket, Key=f"{scratch}probe.txt")


def _listed(client: Any, bucket: str, prefix: str) -> bool:
    return bool(client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents"))
