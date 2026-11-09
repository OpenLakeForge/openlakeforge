"""Per-user Trino access from the signed-in user's Dex token (ADR 0019 section 9).

Inert until a database is imported with `impersonate_user` and a Trino URL:
without a bearer in the connection the mutator changes nothing. Superset and
the trino client are imported inside `install()` so tests import this file
without either.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Dex tokens expire on their own clock; refreshing a minute early keeps a query
# from starting with a token that dies mid-request.
EXPIRY_MARGIN_SECONDS = 60


def db_connection_mutator(uri, params, username, security_manager, source):
    """Send the user's bearer to Trino as the `token` extra credential.

    Stock `impersonate_user` puts the token in an `Authorization` header only.
    Trino 480 with keyless authentication reads the token from the extra
    credential, and the session user must be the email claim.
    """
    if uri.get_backend_name() != "trino":
        return uri, params
    args = params.get("connect_args") or {}
    session = args.get("http_session")
    scheme, _, token = (session.headers.get("Authorization", "") if session else "").partition(" ")
    if scheme != "Bearer" or not token:
        # No bearer: leave the connection alone and let Trino's 401 start the
        # OAuth2 flow. Nothing here may invent an identity.
        return uri, params

    args["extra_credential"] = [("token", token)]
    user = security_manager.find_user(username=username) if username else None
    if identity := getattr(user, "email", None) or username:
        args["user"] = identity
    # trino/dbapi.py builds its own session, and applies `verify`, only when
    # none is supplied; the supplied one keeps requests' default.
    if "verify" in args:
        session.verify = args["verify"]
    # A URI password makes trino/sqlalchemy/dialect.py replace the bearer with
    # basic authentication, silently.
    if uri.password:
        uri = uri.set(password=None)
    return uri, params


def store_login_token(user_id: int, token: dict) -> None:
    """Seed the user's Trino OAuth2 row from the sign-in token response.

    The user then opens a dashboard without a second consent. One database
    only: Dex rotates refresh tokens, so two rows sharing one would invalidate
    each other. A stack with no per-user database yet stores nothing.
    """
    from superset import db
    from superset.models.core import Database, DatabaseUserOAuth2Tokens

    database = next(
        (d for d in db.session.query(Database).filter(Database.impersonate_user.is_(True)).order_by(Database.id)
         if d.backend == "trino"),
        None,
    )  # fmt: skip
    if database is None:
        return
    row = db.session.query(DatabaseUserOAuth2Tokens).filter_by(user_id=user_id, database_id=database.id).one_or_none()
    row = row or DatabaseUserOAuth2Tokens(user_id=user_id, database_id=database.id)
    row.access_token = token["access_token"]
    row.access_token_expiration = datetime.now() + timedelta(
        seconds=max(token.get("expires_in", 0) - EXPIRY_MARGIN_SECONDS, 0)
    )
    row.refresh_token = token.get("refresh_token") or row.refresh_token
    db.session.add(row)
    db.session.commit()


def install(app=None) -> None:
    """Fix the stock refresh for a refresh token Dex refuses.

    Superset 6.1.0 deletes the stored token only when the engine spec raises
    its `oauth2_exception`; `get_oauth2_fresh_token` raises `HTTPError` on Dex's
    400 `invalid_grant`, so the dead token stays and every query errors instead
    of asking the user to sign in again.
    """
    from requests import HTTPError
    from superset.db_engine_specs.trino import TrinoEngineSpec
    from trino.exceptions import TrinoAuthError

    fresh = TrinoEngineSpec.get_oauth2_fresh_token.__func__

    def get_oauth2_fresh_token(cls, config, refresh_token):
        try:
            token = fresh(cls, config, refresh_token)
        except HTTPError as ex:
            if ex.response is not None and ex.response.status_code in (400, 401):
                raise TrinoAuthError("error 401: the identity provider refused the refresh token") from ex
            raise
        if "expires_in" in token:
            token = {**token, "expires_in": max(token["expires_in"] - EXPIRY_MARGIN_SECONDS, 0)}
        return token

    TrinoEngineSpec.get_oauth2_fresh_token = classmethod(get_oauth2_fresh_token)
