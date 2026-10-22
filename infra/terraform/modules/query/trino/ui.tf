locals {
  # Plain-HTTP route: Trino's OAuth2 UI sign-in needs HTTPS, so the UI signs in
  # as one fixed user instead of showing its login form. The perimeter guards the
  # route; /v1 keeps http-server.authentication.type (the UI type is independent).
  ui_properties = var.web_ui_fixed_user == null ? [] : [
    "web-ui.authentication.type=fixed",
    "web-ui.user=${var.web_ui_fixed_user}",
  ]
}
