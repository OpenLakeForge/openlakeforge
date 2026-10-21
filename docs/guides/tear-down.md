# Tear down a local deployment

How to remove a local OpenLakeForge deployment completely, how to remove only
the platform and keep the Kubernetes cluster, and how to clear what `olf`
leaves on your machine.

## Remove everything

```bash
olf destroy --provider local
```

It runs two steps, in order:

1. **Platform.** Terraform destroys the platform services, then `olf` deletes
   every namespace the deployment owns, including a stage you disabled in the
   profile.
2. **Foundation.** Terraform deletes the kind cluster `openlakeforge-local`.

The cluster holds all state: the SeaweedFS buckets, the Iceberg catalog, the
PostgreSQL databases and Dagster's run history. Removing it deletes your
Bronze, Silver and Gold data and cannot be undone. Your project files,
`openlakeforge.yaml` and `lakehouse_code/`, are not touched, so the next
`olf deploy --provider local` rebuilds the same platform from them.

With the HTTPS stack, the local CA lives in the cluster, so a rebuilt cluster
gets a new CA. Run `olf access trust` again after the next deploy.

## Remove only the platform

Keep the kind cluster and delete the platform services and namespaces:

```bash
olf destroy --provider local --phase platform
```

Install them again, then your code, when you want them back:

```bash
olf deploy --provider local --phase platform
olf deploy --provider local --phase artifacts
```

## Remove only the cluster

```bash
olf destroy --provider local --phase foundation
```

This refuses to run while any namespace the deployment owns still exists, and
names those namespaces. Run `--phase platform` first. `--force` deletes the
cluster even with platform resources in it; use it only when you intend to lose
them.

## Clear what `olf` keeps on your machine

`olf destroy` does not remove the managed Terraform, Helm, kubectl and kind
binaries under `~/.openlakeforge` (`$OLF_HOME` if you set it). To delete the
toolchain versions:

```bash
olf toolchain clean --all
```

`--version <v>` removes one distribution version, and `--keep-current` removes
every version but the current one. Neither command touches tools you installed
yourself.

## Other providers

The same command tears down a cloud deployment, and removes billable
resources:

```bash
olf destroy --provider aws
olf destroy --provider azure
```

With `--phase`, destroy the platform before the foundation there as well.

## Check it worked

```bash
docker ps --filter name=openlakeforge-local
```

After a full teardown it lists no containers. After `--phase platform` the
cluster's node containers are still listed.
