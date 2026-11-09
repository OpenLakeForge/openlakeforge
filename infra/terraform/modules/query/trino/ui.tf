locals {
  # Trino has no authentication of its own (ADR 0019 section 9), so the Web UI
  # signs in as one fixed user instead of showing a login form. The perimeter
  # guards the route; what a query reads is still Polaris's decision.
  ui_properties = var.web_ui_fixed_user == null ? [] : [
    "web-ui.authentication.type=fixed",
    "web-ui.user=${var.web_ui_fixed_user}",
  ]
}
