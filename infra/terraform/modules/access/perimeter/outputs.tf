output "route_group" {
  description = "Protected contract ref to the role floor that gates it (the lowest role its grant reaches)."
  value       = local.route_floor
}

output "group_middlewares" {
  description = "Role floor to the Traefik middlewares, in order, that gate a route in that group."
  value = merge(
    { for floor in local.floors : floor => ["olf-strip-identity@file", "olf-auth-${floor}@file"] },
    { (local.api_group) = ["olf-strip-identity@file", "olf-auth-${local.api_group}@file"] },
  )
}

output "api_group" {
  description = "Route group of the platform API: the lowest role floor, with the middleware that also hands the session's ID token to the backend as Authorization. In group_middlewares."
  value       = local.api_group
}

output "traefik_dynamic_config" {
  description = "Traefik file-provider document defining every middleware group_middlewares names."
  value       = local.traefik_dynamic_config
}

output "callback_service_name" {
  description = "Service that answers the login callback host: the instance that admits every recognised role."
  value       = "${var.release_name}-${local.last_role}"
}

output "port" {
  description = "Port every oauth2-proxy Service listens on."
  value       = local.port
}

output "contract" {
  description = "shared.access.perimeter contract fields."
  value = {
    implementation   = "perimeter.oauth2_proxy"
    enabled          = true
    protected_routes = sort(var.protected_refs)
    callback_url     = "${var.route_scheme}://${var.callback_host}/oauth2/callback"
  }
}
