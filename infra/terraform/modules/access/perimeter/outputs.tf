output "route_group" {
  description = "Protected contract ref to the role floor that gates it (the lowest role its grant reaches)."
  value       = local.route_floor
}

output "group_middlewares" {
  description = "Role floor to the Traefik middlewares, in order, that gate a route in that group."
  value       = { for floor in local.floors : floor => ["olf-strip-identity@file", "olf-auth-${floor}@file"] }
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
    callback_url     = "https://${var.callback_host}/oauth2/callback"
  }
}
