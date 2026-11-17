from __future__ import annotations

from olf.infra.e2e import _log_retention as retention

CONF = {
    "version": 1,
    "locations": [
        {"locationPrefix": "/buckets/ops/activations/dev/logs/v2/", "ttl": "30d"},
        {"locationPrefix": "/buckets/ops/platform/logs/v2/", "ttl": "7d"},
        {"locationPrefix": "/buckets/ops/other/"},
    ],
}


def test_rules_map_each_path_to_its_ttl() -> None:
    assert retention.retention_rules(CONF) == {
        "/buckets/ops/activations/dev/logs/v2/": "30d",
        "/buckets/ops/platform/logs/v2/": "7d",
        "/buckets/ops/other/": "",
    }
    assert retention.retention_rules({"version": 1}) == {}


def test_a_prefix_with_another_ttl_or_none_is_missing() -> None:
    rules = retention.retention_rules(CONF)

    missing = retention.missing_rules(rules, "ops", ["activations/dev/logs/v2/", "platform/logs/v2/", "x/"], 30)

    assert missing == ["/buckets/ops/platform/logs/v2/", "/buckets/ops/x/"]


def test_an_entry_ttl_is_found_in_either_spelling() -> None:
    assert retention.entry_ttl_seconds({"Attr": {"Mtime": "t", "TtlSec": 2592000}}) == 2592000
    assert retention.entry_ttl_seconds({"attributes": {"ttl_sec": 60}, "chunks": []}) == 60
    assert retention.entry_ttl_seconds({"Attr": {"Mtime": "t"}, "chunks": [{"ttl": "x"}]}) is None
