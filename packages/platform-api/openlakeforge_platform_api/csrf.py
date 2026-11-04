"""Cross-site write protection (ADR 0021): the perimeter's session cookie can authorize a browser request.

No token and no session store. A browser always sends `Origin` on a cross-origin
request and on a same-origin unsafe one, and `Sec-Fetch-Site` on every request
it makes; a client that sends neither is not a browser (`olf`) and cannot be
driven by a victim's cookie. No CORS headers are served, so no other origin may
read a response or pass a preflight. Residual: a browser too old to send either
header looks like the CLI.
"""

from __future__ import annotations

from fastapi import Request

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
JSON = "application/json"
# Operations whose body is not JSON, by the type they take. Every type here is outside what a cross-site <form>
# can send (urlencoded, multipart, text/plain), so it needs a preflight that no origin passes.
BODY_TYPES = {"import_report": "application/zip"}


def violation(request: Request, operation: str, public_origin: str | None) -> str | None:
    """Why `request` is refused as a cross-site or forged write, or None when it may proceed."""
    if request.method not in UNSAFE_METHODS:
        return None
    origin = request.headers.get("Origin")
    if origin is not None and origin != public_origin:
        return "cross-origin write refused"
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        return "cross-site write refused"
    has_body = request.headers.get("Content-Length", "0") != "0" or "Transfer-Encoding" in request.headers
    media_type = request.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if has_body and media_type != BODY_TYPES.get(operation, JSON):
        return f"a request body must be {BODY_TYPES.get(operation, JSON)}"
    return None
