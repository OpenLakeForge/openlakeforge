output "cluster_issuer_name" {
  description = "ClusterIssuer that signs route certificates from the local CA, available once it is applied."
  value       = local.issuer_name
  depends_on  = [helm_release.local_ca]
}
