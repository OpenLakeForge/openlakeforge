"""Security manager for the OIDC sign-in; its settings come from olf_oidc.py."""

from __future__ import annotations

import time

from flask import session
from flask_appbuilder import expose
from flask_appbuilder.security.views import AuthOAuthView
from olf_oidc import DATA_ROLE, NO_ACCESS_ROLE, SESSION_SECONDS, VIEWER_ROLE
from superset.security import SupersetSecurityManager


class OlfAuthOAuthView(AuthOAuthView):
    @expose("/login/")
    @expose("/login/<provider>")
    def login(self, provider=None):
        # One provider: skip the login page and go straight to the issuer.
        # Every FAB callback failure (refused user, denied consent, bad state)
        # flashes with category "warning" and returns here; the page must show
        # it, not loop. Flask-Login's "Please log in" is category "message"
        # and must still redirect.
        refused = any(c == "warning" for c, _ in session.get("_flashes", []))
        if provider is None and not refused:
            provider = self.appbuilder.sm.oauth_providers[0]["name"]
        return super().login(provider)


class OlfSecurityManager(SupersetSecurityManager):
    # Superset's own /login/ view would shadow the OAuth one.
    register_superset_auth_view = False
    authoauthview = OlfAuthOAuthView

    def sync_role_definitions(self) -> None:
        super().sync_role_definitions()
        # Superset has no read-only dashboard role. Public's permission set
        # under another name cannot be confused with the anonymous role.
        self.set_role(VIEWER_ROLE, self._is_public_pvm, self._get_all_pvms())
        for name in (NO_ACCESS_ROLE, DATA_ROLE):
            self.add_role(name)

    def get_oauth_user_info(self, provider, response=None):
        claims = self.oauth_remotes[provider].userinfo()
        values = claims.get(self.appbuilder.app.config["OLF_ROLE_CLAIM"], [])
        # Prefixed so an issuer user can never resolve to the local `admin`
        # account, whose roles a login would rewrite.
        return {
            "username": f"oidc-{claims.get('preferred_username') or claims['sub']}",
            "email": claims.get("email", ""),
            "first_name": claims.get("name", ""),
            "last_name": "",
            "role_keys": [values] if isinstance(values, str) else list(values),
        }

    def auth_user_oauth(self, userinfo):
        # FAB adds the registration role to every user, so without this check
        # a user with no recognised role would still sign in.
        mapping = self.appbuilder.app.config["AUTH_ROLES_MAPPING"]
        if not any(key in mapping for key in userinfo.get("role_keys", [])):
            return None
        user = super().auth_user_oauth(userinfo)
        if user:
            session["olf_login_at"] = time.time()
        return user


def enforce_session_bound(app) -> None:
    # Re-signing the cookie on any session write (CSRF token, flash) slides
    # Flask's own lifetime, so the bound is checked against the login time
    # stored inside the signed cookie.
    @app.before_request
    def _expire() -> None:
        started = session.get("olf_login_at")
        if started is not None and time.time() - started > SESSION_SECONDS:
            session.clear()
