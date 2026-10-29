"""Validators for contract and profile values that need no deployment state.

Shared by the Deployment Profile and the provider contracts, so neither has to
import the other to reuse them.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


class ProviderContractError(ValueError):
    """Raised when a provider contract is malformed or cannot serve a stage."""


def _string(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderContractError(f"{where} must be a non-empty string")
    return value


_URI_INVALID = re.compile(r"[^A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]|%(?![0-9A-Fa-f]{2})")


def _oidc_issuer_url(value: object, *, where: str, allow_http: bool = False) -> str:
    """The one authority for OIDC issuer URLs (the schemas only check `^https://`):
    https (http only in the plain-HTTP `*.localhost` mode, ADR 0014), a host,
    a valid port, no userinfo, query, fragment, whitespace or control
    characters. `_absolute_http_uri` covers host, port and userinfo."""
    uri = _absolute_http_uri(value, where=where)
    parts = urlsplit(uri)
    if (
        parts.scheme not in (("https", "http") if allow_http else ("https",))
        or not parts.hostname
        or "?" in uri
        or "#" in uri
    ):
        scheme = "an http or https" if allow_http else "an https"
        raise ProviderContractError(f"{where} must be {scheme} URL with a host and no query or fragment")
    if "[" in parts.path or "]" in parts.path:
        raise ProviderContractError(f"{where} must not contain '[' or ']' outside the host")
    if re.search(r"[\s\x00-\x1f\x7f]", uri):
        raise ProviderContractError(f"{where} must not contain whitespace or control characters")
    if _URI_INVALID.search(uri):
        raise ProviderContractError(f"{where} must only contain RFC 3986 URI characters and valid percent escapes")
    return uri


def _absolute_http_uri(value: object, *, where: str) -> str:
    """A URI with a real scheme and authority, fit to compare by origin.

    Floe's Iceberg REST client accepts either http or https, unlike the
    Trino query endpoint (_http_host_port_uri), so both schemes are valid
    here. Without this, two relative strings (urlsplit gives an empty
    scheme/netloc for e.g. "polaris") would compare as trivially
    "same origin" in _same_origin, and a non-string value would raise
    AttributeError/TypeError inside urlsplit's caller instead of
    ProviderContractError.
    """
    uri = _string(value, where=where)
    try:
        parts = urlsplit(uri)
    except ValueError as exc:  # e.g. an unbalanced IPv6 bracket in the authority
        raise ProviderContractError(f"{where} is not a valid URI: {exc}") from exc
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ProviderContractError(f"{where} must be an absolute http:// or https:// URI")
    if parts.username is not None or parts.password is not None:
        # AGENTS.md: credentials never appear in the contract, only Secret
        # references - a URI with embedded userinfo would otherwise be
        # exported by build_contract_env() and written into Floe's
        # generated EnvironmentProfile.
        raise ProviderContractError(f"{where} must not embed credentials in its authority")
    try:
        port = parts.port
    except ValueError as exc:
        raise ProviderContractError(f"{where} must have a valid TCP port") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ProviderContractError(f"{where} must have a valid TCP port")
    return uri
