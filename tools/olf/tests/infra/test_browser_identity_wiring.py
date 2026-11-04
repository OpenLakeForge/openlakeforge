"""How the local root wires a browser session's token to the platform API (#799).

The modules' own behaviour is in their `terraform test` files; the root cannot be planned here, so its wiring
is pinned in source, as test_perimeter_args.py does.
"""

from __future__ import annotations

import re
from pathlib import Path

TERRAFORM = Path(__file__).resolve().parents[4] / "infra/terraform"
LOCAL = (TERRAFORM / "environments/local/main.tf").read_text()
CONTRACTS = (TERRAFORM / "environments/local/contracts.tf").read_text()


def test_only_the_platform_api_route_uses_the_api_group() -> None:
    assert CONTRACTS.count("module.perimeter[0].api_group") == 1
    route = CONTRACTS[CONTRACTS.index('"platform-api" = {') :]
    assert "group        = module.perimeter[0].api_group" in route.split("}", 1)[0]


def test_the_local_root_trusts_the_perimeter_for_olf_cli() -> None:
    assert re.search(
        r'trusted_peers\s+= \{ for name in local.identity_public_clients : name => \["perimeter"\] \}', LOCAL
    )


def test_the_login_audience_is_requested_only_with_dex() -> None:
    assert 'login_audience    = local.identity_dex ? local.identity_oidc.clients["olf-cli"].client_id : null' in LOCAL


def test_the_apis_accepted_audience_stays_olf_cli_alone() -> None:
    assert 'audiences         = [local.identity_oidc.clients["olf-cli"].client_id]' in LOCAL
    assert "perimeter.client_id]" not in LOCAL.split('module "platform_api"')[1].split('module "traefik"')[0]


def test_person_query_follows_dex_with_people_catalogs_and_the_origin_follows_the_route_scheme() -> None:
    assert "person_query  = local.query_api_enabled" in LOCAL
    assert 'query_api_enabled = local.identity_dex && local.trino_workload_authentication == "password"' in CONTRACTS
    assert 'public_origin = "${local.route_scheme}://${local.base_domain}"' in LOCAL
    assert 'route_scheme = local.route_tls ? "https" : "http"' in CONTRACTS
