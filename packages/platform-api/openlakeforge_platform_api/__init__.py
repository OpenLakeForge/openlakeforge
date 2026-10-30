"""The platform API (ADR 0021): one backend for the portal, `olf` and agents.

It validates every token itself and checks each operation against the
permission matrix in `permissions.toml`; a perimeter header is never proof of
identity on its own.
"""
