output "ingress_class_name" {
  description = "IngressClass Traefik serves, available once the release is ready."
  # The chart names its IngressClass after the release.
  value = helm_release.traefik.name
}
