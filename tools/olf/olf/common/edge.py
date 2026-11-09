"""Pinning the public name of a `tls: external` deployment to a local address.

Under `spec.access.tls: external` the base domain is the customer's DNS. Only the
e2e fixture, whose edge listens on the runner, needs a name to resolve elsewhere,
so it states the mapping as `<domain>=<address>` in `OLF_EDGE_RESOLVE`. A real
deployment never sets it and its clients resolve the name as usual.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

EDGE_RESOLVE_ENV = "OLF_EDGE_RESOLVE"


def pinned_address(host: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The address `host` is pinned to, or None when it is the base domain's or a name under it that is not."""
    spec = (os.environ if environ is None else environ).get(EDGE_RESOLVE_ENV, "")
    domain, _, address = spec.partition("=")
    name = host.lower().rstrip(".")
    if domain and address and (name == domain or name.endswith(f".{domain}")):
        return address
    return None
