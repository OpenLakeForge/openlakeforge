"""Secret redaction for service error text that a caller is shown, such as a failed Dagster step's message.

Best effort by shape, not a guarantee: a secret with no recognisable shape passes through. That is why the callers
also bound what they serve (the first line of a message, a fixed length) and never fetch a stack trace at all.
"""

from __future__ import annotations

import re

REDACTED = "<redacted>"

# `scheme://user:password@host`: the userinfo goes, the scheme and host stay so the message still names the service.
_USERINFO = re.compile(r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")
# A URL's whole query: a presigned URL carries its credential and signature there, under names no list can foresee.
_QUERY = re.compile(r"(?P<url>\b[A-Za-z][A-Za-z0-9+.-]*://[^\s?#]*)\?[^\s#]*")
_SCHEME_TOKEN = re.compile(r"(?i)\b(?P<scheme>bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
# A quoted value is escape-aware, so a JSON-serialized secret holding `\"` is consumed whole, and an unclosed
# quote (a message cut short) runs to the end rather than leaving its tail.
_KEY_VALUE = re.compile(
    r"(?i)(?P<key>\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credential|signature)[\w.-]*[\"']?)(?P<sep>\s*[=:]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"?|'(?:\\.|[^'\\])*'?|[^\s,;&\"'}]+)"
)
_LOOKALIKES = (
    # AWS access key ids, by their documented prefixes.
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA|APKA)[A-Z0-9]{16}\b"),
    # A JWT: three base64url segments, the first a JSON object.
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),
    # A long base64 run of upper, lower and digits, the shape of a secret access key. A hex digest or an identifier
    # with underscores does not match, so step keys and run ids survive.
    re.compile(
        r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[0-9])"
        r"[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/])"
    ),
)


def redact(text: str) -> str:
    """`text` with URL userinfo and queries, Bearer and Basic credentials, `key=value` secrets and access-key
    lookalikes replaced by REDACTED."""
    text = _USERINFO.sub(rf"\g<scheme>{REDACTED}@", text)
    text = _QUERY.sub(rf"\g<url>?{REDACTED}", text)
    text = _SCHEME_TOKEN.sub(rf"\g<scheme> {REDACTED}", text)
    text = _KEY_VALUE.sub(rf"\g<key>\g<sep>{REDACTED}", text)
    for pattern in _LOOKALIKES:
        text = pattern.sub(REDACTED, text)
    return text
